from __future__ import annotations

import gzip
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from live_betting.models import Market, OddsSnapshot
from live_betting.monitor import _collect_odds_response
from live_betting.sanitize import (
    PUBLIC_STREAM_EVIDENCE_KEY,
    sanitize_public_url,
    sanitize_raybet_payload,
    stored_public_stream_url,
    verified_public_stream_url,
)
from live_betting.storage import LiveBettingStore


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def test_payload_redaction_removes_sensitive_keys_and_url_query() -> None:
    payload = {
        "result": {
            "id": "42",
            "live_url": (
                "https://stream.test/live.m3u8?"
                "auth_key=SECRET_TOKEN_VALUE&expires=999"
            ),
            "nested": {
                "Authorization": "Bearer SECRET_TOKEN_VALUE",
                "safe": "text?token=SECRET_TOKEN_VALUE&x=1",
            },
        }
    }

    sanitized = sanitize_raybet_payload(payload)

    encoded = json.dumps(sanitized, ensure_ascii=False, sort_keys=True)
    assert "SECRET_TOKEN_VALUE" not in encoded
    assert sanitized["result"]["live_url"] == "https://stream.test/live.m3u8"
    assert "Authorization" not in sanitized["result"]["nested"]
    assert sanitized["result"]["nested"]["safe"] == "text?x=1"


def test_public_url_drops_userinfo_query_and_fragment() -> None:
    assert (
        sanitize_public_url(
            "https://user:pass@stream.test/live.m3u8?auth_key=secret#fragment"
        )
        == "https://stream.test/live.m3u8"
    )


def test_direct_storage_and_raw_archive_never_keep_signed_url_material() -> None:
    payload = {
        "id": "42",
        "game_id": 151,
        "tournament_name": "Security Cup",
        "live_url": "https://stream.test/live.m3u8?auth_key=SECRET_TOKEN_VALUE",
        "team": [
            {"pos": 1, "team_id": 1, "team_name": "One"},
            {"pos": 2, "team_id": 2, "team_name": "Two"},
        ],
    }

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with LiveBettingStore(root / "live.db") as store:
            store.init_schema()
            store.upsert_raybet_match(payload, NOW)
            stored = store.connection.execute(
                "SELECT live_url, raw_json FROM raybet_matches WHERE raybet_match_id='42'"
            ).fetchone()
            assert stored["live_url"] is None
            assert "SECRET_TOKEN_VALUE" not in stored["raw_json"]
            assert PUBLIC_STREAM_EVIDENCE_KEY not in stored["raw_json"]

            snapshot = OddsSnapshot(
                "42",
                "odds-1",
                "group-1",
                NOW,
                2.0,
                "1",
                Market("winner", "map_1", "team_one", None, "team_one", True),
                None,
                {"stream_url": "https://stream.test/live.m3u8?token=SECRET_TOKEN_VALUE"},
            )
            store.insert_odds(snapshot)
            odds_raw = store.connection.execute(
                "SELECT raw_json FROM odds_snapshots WHERE odds_id='odds-1'"
            ).fetchone()["raw_json"]
            assert "SECRET_TOKEN_VALUE" not in odds_raw

            sanitized_payload = sanitize_raybet_payload(payload)
            receipt = store.archive_response_payload(
                sanitized_payload,
                observed_at=NOW,
                match_id="42",
            )

        with gzip.open(receipt.path, "rt", encoding="utf-8") as handle:
            archived = handle.read()
        assert "SECRET_TOKEN_VALUE" not in archived


def test_unsigned_allowlisted_stream_requires_writer_provenance() -> None:
    public_url = "https://qplay.ehome.gg/live/42.m3u8"
    assert verified_public_stream_url(public_url) == public_url
    assert verified_public_stream_url(f"{public_url}?token=secret") is None
    assert verified_public_stream_url("javascript:alert(1)") is None
    assert verified_public_stream_url("https://foreign.example/live/42.m3u8") is None

    with tempfile.TemporaryDirectory() as directory:
        with LiveBettingStore(Path(directory) / "live.db") as store:
            store.init_schema()
            store.upsert_raybet_match(
                {"id": "42", "game_id": 151, "live_url": public_url},
                NOW,
                public_live_url=public_url,
            )
            row = store.connection.execute(
                "SELECT live_url, raw_json FROM raybet_matches WHERE raybet_match_id='42'"
            ).fetchone()

    assert stored_public_stream_url(row["live_url"], row["raw_json"]) == public_url


def test_direct_collector_records_only_originally_unsigned_public_stream() -> None:
    public_url = "https://qplay.ehome.gg/live/42.m3u8"

    class Client:
        def __init__(self, live_url: str) -> None:
            self.live_url = live_url

        def match_odds(self, match_id: str) -> dict[str, object]:
            return {
                "result": {
                    "id": match_id,
                    "game_id": 151,
                    "live_url": self.live_url,
                    "team": [],
                    "odds": [],
                }
            }

    with tempfile.TemporaryDirectory() as directory:
        with LiveBettingStore(Path(directory) / "live.db") as store:
            store.init_schema()
            _collect_odds_response(
                store,
                Client(public_url),
                match_id="42",
                response_kind="live_odds",
                list_row={"id": "42"},
            )
            row = store.connection.execute(
                "SELECT live_url, raw_json FROM raybet_matches WHERE raybet_match_id='42'"
            ).fetchone()
            assert stored_public_stream_url(row["live_url"], row["raw_json"]) == public_url

            _collect_odds_response(
                store,
                Client(f"{public_url}?auth_key=EPHEMERAL_TOKEN"),
                match_id="42",
                response_kind="live_odds",
                list_row={"id": "42"},
            )
            row = store.connection.execute(
                "SELECT live_url, raw_json FROM raybet_matches WHERE raybet_match_id='42'"
            ).fetchone()
            assert row["live_url"] is None
            assert "EPHEMERAL_TOKEN" not in row["raw_json"]
