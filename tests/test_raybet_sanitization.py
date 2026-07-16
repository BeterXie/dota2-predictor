from __future__ import annotations

import gzip
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from live_betting.models import Market, OddsSnapshot
from live_betting.monitor import _write_raw
from live_betting.sanitize import sanitize_raybet_payload, sanitize_public_url
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
            assert stored["live_url"] == "https://stream.test/live.m3u8"
            assert "SECRET_TOKEN_VALUE" not in stored["raw_json"]

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

        archive_path = _write_raw(root / "raw", "42", payload, NOW)
        with gzip.open(archive_path, "rt", encoding="utf-8") as handle:
            archived = handle.read()
        assert "SECRET_TOKEN_VALUE" not in archived
