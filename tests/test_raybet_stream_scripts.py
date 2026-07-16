from __future__ import annotations

import hashlib
import json
from io import StringIO
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from scripts.supervise_raybet_streams import (
    DEFAULT_OBSERVATION_DIR as SUPERVISOR_OBSERVATION_DIR,
    active_matches,
    reap_children,
    record_supervisor_health,
    supervisor_health,
    watcher_command,
)
from scripts.invalidate_vision_observations import freeze_draft_map, invalidate
from scripts.watch_raybet_stream import (
    ALLOWED_STREAM_HOSTS,
    DEFAULT_OBSERVATION_DIR as WATCHER_OBSERVATION_DIR,
    ROOT,
    _meaningful,
    _should_persist_frame,
    _validate_stream_url,
    _write_evidence_frame,
    completion_check_due,
    current_frame_clock_fields,
    match_is_complete,
    match_source,
    resolve_source,
)
from contracts.live_observation import LiveObservation
from vision.map_state import ConfirmedClock
from live_betting.storage import LiveBettingStore
from live_betting.vision import VisionObservation


STREAM_URL = "https://play.ehome.gg/live.m3u8"


def test_visual_supervisor_cli_constructs_parser() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "supervise_raybet_streams.py"),
            "--help",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--database" in result.stdout


def test_visual_supervisor_records_worker_heartbeat(tmp_path: Path) -> None:
    database = tmp_path / "vision-health.db"
    with LiveBettingStore(database) as store:
        store.init_schema()
    record_supervisor_health(database, "healthy", active_matches=2)
    with LiveBettingStore(database) as store:
        row = store.connection.execute(
            """SELECT status, details_json FROM service_health
                 WHERE component='vision_worker'"""
        ).fetchone()
    assert row["status"] == "healthy"
    assert json.loads(row["details_json"])["active_watchers"] == 2


def _source_database(
    tmp_path: Path,
    raw: dict,
    rows: list[tuple],
    *,
    status: str = "2",
    updated_at: str = "2026-07-14T01:00:00+00:00",
) -> Path:
    database = tmp_path / "live.db"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE raybet_matches (
                raybet_match_id TEXT PRIMARY KEY,
                live_url TEXT,
                raw_json TEXT,
                best_of INTEGER,
                status TEXT,
                updated_at TEXT
            );
            CREATE TABLE odds_snapshots (
                id INTEGER PRIMARY KEY,
                odds_id TEXT,
                raybet_match_id TEXT,
                market_type TEXT,
                status TEXT,
                period TEXT,
                received_at TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO raybet_matches VALUES (?, ?, ?, ?, ?, ?)",
            (
                "42",
                STREAM_URL,
                json.dumps(raw),
                3,
                status,
                updated_at,
            ),
        )
        connection.executemany(
            "INSERT INTO odds_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)", rows
        )
        connection.commit()
    finally:
        connection.close()
    return database


def _raybet_payload(*, settled_maps: dict[int, str] | None = None) -> dict:
    settled_maps = settled_maps or {}
    odds = []
    for map_number in range(1, 4):
        winner = settled_maps.get(map_number)
        status = 5 if winner is not None else (2 if map_number == 1 else 1)
        for team_id, side in ((101, "team_one"), (202, "team_two")):
            odds.append(
                {
                    "odds_group_id": 1000 + map_number,
                    "odds_id": 2000 + map_number * 10 + team_id,
                    "match_stage": f"r{map_number}",
                    "group_short_name": "Winner",
                    "tag": "win",
                    "team_id": team_id,
                    "status": status,
                    "win": int(side == winner) if winner is not None else -1,
                }
            )
    return {
        "id": 42,
        "game_id": 151,
        "team": [
            {"pos": 1, "team_id": 101, "score": {}},
            {"pos": 2, "team_id": 202, "score": {}},
        ],
        "odds": odds,
    }


def test_match_source_prefers_manual_current_index(tmp_path: Path) -> None:
    raw = {
        "team": [
            {"score": {"manualControlData": {"currentIndex": 3}}},
            {"score": {"manualControlData": {"currentIndex": 3}}},
        ]
    }
    database = _source_database(
        tmp_path,
        raw,
        [
            (1, "series-a", "42", "winner", "1", "series", "2026-07-14T01:00:00+00:00"),
            (2, "map-1-a", "42", "winner", "1", "map_1", "2026-07-14T01:00:00+00:00"),
            (3, "map-2-a", "42", "winner", "1", "map_2", "2026-07-14T01:00:00+00:00"),
        ],
    )
    assert match_source(database, "42") == (STREAM_URL, 3)


def test_match_source_refreshes_signed_url_without_reading_it_from_sqlite(
    tmp_path: Path,
) -> None:
    raw = {
        "team": [
            {"score": {"manualControlData": {"currentIndex": 1}}},
            {"score": {"manualControlData": {"currentIndex": 1}}},
        ]
    }
    database = _source_database(tmp_path, raw, [])
    signed = "https://qplay.ehome.gg/live.m3u8?auth_key=EPHEMERAL_TOKEN"
    response = {
        "result": {
            "id": 42,
            "game_id": 151,
            "live_url": signed,
            "team": [
                {"score": {"manualControlData": {"currentIndex": 1}}},
                {"score": {"manualControlData": {"currentIndex": 1}}},
            ],
        }
    }
    with patch("scripts.watch_raybet_stream.RayBetClient") as client_type:
        client = client_type.return_value.__enter__.return_value
        client.match_odds.return_value = response
        assert match_source(database, "42", refresh_url=True) == (signed, 1)
        client.match_odds.assert_called_once_with("42")

    connection = sqlite3.connect(database)
    try:
        stored = connection.execute(
            "SELECT live_url, raw_json FROM raybet_matches WHERE raybet_match_id='42'"
        ).fetchone()
    finally:
        connection.close()
    assert stored[0] == STREAM_URL
    assert "EPHEMERAL_TOKEN" not in stored[1]


def test_match_source_uses_settled_maps_not_open_future_markets(
    tmp_path: Path,
) -> None:
    database = _source_database(
        tmp_path,
        _raybet_payload(settled_maps={1: "team_one"}),
        [],
    )

    assert match_source(database, "42") == (STREAM_URL, 2)


def test_match_source_does_not_mistake_future_open_market_for_current_map(
    tmp_path: Path,
) -> None:
    database = _source_database(tmp_path, _raybet_payload(), [])

    assert match_source(database, "42") == (STREAM_URL, 1)


def test_match_source_fails_closed_when_current_map_is_ambiguous(
    tmp_path: Path,
) -> None:
    database = _source_database(
        tmp_path,
        {},
        [
            (1, "map-1-a", "42", "winner", "1", "map_1", "2026-07-14T01:00:00+00:00"),
            (2, "map-2-a", "42", "winner", "1", "map_2", "2026-07-14T01:00:00+00:00"),
            (3, "series", "42", "winner", "1", "series", "2026-07-14T01:00:00+00:00"),
        ],
    )

    with pytest.raises(ValueError, match="cannot determine a unique current map"):
        match_source(database, "42")


def test_explicit_map_override_bypasses_ambiguous_market_inference(
    tmp_path: Path,
) -> None:
    database = _source_database(
        tmp_path,
        {},
        [
            (1, "map-1-a", "42", "winner", "1", "map_1", "2026-07-14T01:00:00+00:00"),
            (2, "map-2-a", "42", "winner", "1", "map_2", "2026-07-14T01:00:00+00:00"),
        ],
    )

    assert match_source(database, "42", map_override=2) == (
        STREAM_URL,
        2,
    )


def test_direct_url_requires_explicit_map_number() -> None:
    with pytest.raises(ValueError, match="--map-number is required"):
        resolve_source(
            url=STREAM_URL,
            database=None,
            match_id="42",
            map_number=None,
        )
    assert resolve_source(
        url=STREAM_URL,
        database=None,
        match_id="42",
        map_number=2,
    ) == (STREAM_URL, 2)


@pytest.mark.parametrize("host", sorted(ALLOWED_STREAM_HOSTS))
@pytest.mark.parametrize(
    ("scheme", "port"),
    (("https", ""), ("https", ":443"), ("http", ":80")),
)
def test_stream_url_allowlist_accepts_public_hosts_and_signed_queries(
    host: str, scheme: str, port: str
) -> None:
    url = f"{scheme}://{host}{port}/live.m3u8?auth_key=EPHEMERAL_TOKEN"

    assert _validate_stream_url(url) == url


@pytest.mark.parametrize(
    "bad_url",
    (
        "https://localhost/live.m3u8",
        "https://127.0.0.1/live.m3u8",
        "https://10.0.0.7/live.m3u8",
        "https://play.ehome.gg:8443/live.m3u8",
        "https://user:pass@play.ehome.gg/live.m3u8",
        "https://play.ehome.gg/live.m3u8#fragment",
        "https://not-allowlisted.example/live.m3u8",
    ),
)
def test_stream_url_allowlist_rejects_private_and_ambiguous_urls(
    bad_url: str,
) -> None:
    with pytest.raises(ValueError, match="invalid stream URL"):
        _validate_stream_url(bad_url)


@pytest.mark.parametrize(
    "bad_url",
    (
        "https://localhost/live.m3u8",
        "https://play.ehome.gg:8443/live.m3u8",
        "https://user:pass@play.ehome.gg/live.m3u8",
    ),
)
def test_stream_url_allowlist_applies_to_sqlite_and_explicit_sources(
    tmp_path: Path, bad_url: str
) -> None:
    database = _source_database(tmp_path, {}, [])
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE raybet_matches SET live_url=? WHERE raybet_match_id='42'",
            (bad_url,),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ValueError, match="invalid stored live URL"):
        match_source(database, "42")
    with pytest.raises(ValueError, match="invalid explicit stream URL"):
        resolve_source(
            url=bad_url,
            database=None,
            match_id="42",
            map_number=1,
        )


def test_stream_url_allowlist_applies_to_fresh_source(tmp_path: Path) -> None:
    database = _source_database(tmp_path, {}, [])
    response = {
        "result": {
            "id": 42,
            "game_id": 151,
            "live_url": "http://127.0.0.1:8000/private.m3u8",
        }
    }
    with patch("scripts.watch_raybet_stream.RayBetClient") as client_type:
        client = client_type.return_value.__enter__.return_value
        client.match_odds.return_value = response
        with pytest.raises(ValueError, match="invalid fresh live URL"):
            match_source(database, "42", refresh_url=True)
        client.match_odds.assert_called_once_with("42")


def test_supervisor_does_not_override_inferred_map_number(tmp_path: Path) -> None:
    command = watcher_command(
        tmp_path / "live.db",
        "42",
        tmp_path / "observations",
        tmp_path / "evidence",
    )
    assert "--map-number" not in command
    assert "--refresh-url" in command
    assert str(tmp_path / "observations" / "42.jsonl") in command


def test_supervisor_selects_only_fresh_live_provider_rows(tmp_path: Path) -> None:
    database = tmp_path / "live.db"
    now = datetime(2026, 7, 15, 14, 2, tzinfo=timezone.utc)
    with LiveBettingStore(database) as store:
        store.init_schema()
        rows = [
            ("live", "2", now - timedelta(seconds=10), "https://stream/live.m3u8"),
            ("prematch", "1", now - timedelta(seconds=10), "https://stream/pre.m3u8"),
            ("stale", "2", now - timedelta(minutes=2), "https://stream/stale.m3u8"),
            ("no-video", "2", now - timedelta(seconds=10), None),
        ]
        for match_id, status, updated_at, live_url in rows:
            store.connection.execute(
                """INSERT INTO raybet_matches
                   (raybet_match_id, status, live_url, raw_json, updated_at)
                   VALUES (?, ?, ?, '{}', ?)""",
                (match_id, status, live_url, updated_at.isoformat()),
            )
        store.connection.commit()

    assert active_matches(database, now=now) == ["live"]


def test_watcher_stops_only_when_live_provider_row_is_stale_or_not_live(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 15, 14, 2, tzinfo=timezone.utc)
    database = _source_database(
        tmp_path,
        _raybet_payload(),
        [],
        updated_at=(now - timedelta(seconds=10)).isoformat(),
    )
    assert not match_is_complete(database, "42", now=now)

    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE raybet_matches SET updated_at=? WHERE raybet_match_id='42'",
            ((now - timedelta(minutes=2)).isoformat(),),
        )
        connection.commit()
    finally:
        connection.close()
    assert match_is_complete(database, "42", now=now)


def test_completion_checks_use_sample_count_not_decoder_sequence() -> None:
    assert not completion_check_due(1)
    assert completion_check_due(15)
    assert completion_check_due(30)


def test_unconfirmed_frame_never_refreshes_previous_clock_value() -> None:
    previous = ConfirmedClock(1, 1392, False, 0.94)
    assert current_frame_clock_fields(previous) == (1392, False, 0.94)
    assert current_frame_clock_fields(None) == (None, None, 0.0)


def test_confirmation_and_pause_changes_are_persisted_as_barriers() -> None:
    def row(clock: int | None, paused: bool | None) -> LiveObservation:
        return LiveObservation(
            raybet_match_id="42",
            map_number=1,
            captured_at_utc=datetime.now(timezone.utc),
            game_clock_seconds=clock,
            is_paused=paused,
            radiant_hero_ids=[1, 2, 3, 4, 5],
            dire_hero_ids=[6, 7, 8, 9, 10],
            clock_confidence=0.95 if clock is not None else 0.0,
            draft_confidence=0.95,
            source_frame_ref="frame",
            screen_state="game",
        )

    confirmed = row(600, False)
    assert _meaningful(confirmed, row(None, None))
    assert _meaningful(confirmed, row(600, True))


def test_every_confirmed_observation_requires_persisted_frame_evidence() -> None:
    def row(clock: int) -> LiveObservation:
        return LiveObservation(
            raybet_match_id="42",
            map_number=1,
            captured_at_utc=datetime.now(timezone.utc),
            game_clock_seconds=clock,
            is_paused=False,
            radiant_hero_ids=[1, 2, 3, 4, 5],
            dire_hero_ids=[6, 7, 8, 9, 10],
            radiant_team_side="team_one",
            clock_confidence=0.95,
            draft_confidence=0.95,
            source_frame_ref="stream:hash:1",
            screen_state="game",
        )

    assert _should_persist_frame(
        row(600),
        row(605),
        captured_at=10.0,
        last_evidence_at=9.0,
        evidence_interval=30.0,
    )


def test_evidence_reference_is_returned_only_after_successful_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "frame.jpg"
    image = np.full((20, 30, 3), 127, dtype=np.uint8)

    reference = _write_evidence_frame(path, image)

    assert reference == str(path.resolve())
    assert path.stat().st_size > 0


def test_failed_evidence_write_cannot_publish_a_frame_reference(
    tmp_path: Path,
) -> None:
    path = tmp_path / "frame.jpg"

    def partial_write(temporary: str, *_: object) -> bool:
        Path(temporary).write_bytes(b"partial")
        return False

    with patch("scripts.watch_raybet_stream.cv2.imwrite", side_effect=partial_write):
        with pytest.raises(OSError, match="failed to write evidence frame"):
            _write_evidence_frame(path, object())
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_vision_invalidation_is_audited_and_online_backed_up(
    tmp_path: Path,
) -> None:
    database = tmp_path / "live.db"
    backup = tmp_path / "backups" / "before.db"
    first = "2026-07-11T17:07:38.100279+00:00"
    second = "2026-07-11T17:07:54.139449+00:00"
    with LiveBettingStore(database) as store:
        store.init_schema()
        for captured_at, frame in ((first, "first.jpg"), (second, "second.jpg")):
            store.connection.execute(
                """INSERT INTO vision_observations VALUES
                   ('42', 1, ?, 1392, 0, '[1,2,3,4,5]',
                    '[6,7,8,9,10]', NULL, 0.94, 0.96, ?, 'game', 1)""",
                (captured_at, frame),
            )
        store.connection.commit()

    count = invalidate(
        database,
        match_id="42",
        map_number=1,
        clock_seconds=1392,
        after=datetime.fromisoformat(first).astimezone(timezone.utc).isoformat(),
        reason="stale_clock_republished_with_new_capture_time",
        backup=backup,
    )
    assert count == 1
    assert backup.exists()
    with LiveBettingStore(database) as store:
        assert (
            store.connection.execute(
                "SELECT confirmed FROM vision_observations WHERE source_frame_ref='first.jpg'"
            ).fetchone()[0]
            == 1
        )
        assert (
            store.connection.execute(
                "SELECT confirmed FROM vision_observations WHERE source_frame_ref='second.jpg'"
            ).fetchone()[0]
            == 0
        )
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM vision_observation_invalidations"
            ).fetchone()[0]
            == 1
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store.connection.execute(
                "UPDATE vision_observation_invalidations SET reason='changed'"
            )
    verification = sqlite3.connect(backup)
    try:
        assert (
            verification.execute(
                "SELECT confirmed FROM vision_observations WHERE source_frame_ref='second.jpg'"
            ).fetchone()[0]
            == 1
        )
    finally:
        verification.close()


def test_freeze_draft_map_rejects_unconfirmed_frame(tmp_path: Path) -> None:
    database = tmp_path / "live.db"
    with LiveBettingStore(database) as store:
        store.init_schema()
        store.connection.execute(
            """INSERT INTO vision_observations
               (raybet_match_id, map_number, captured_at, game_clock_seconds,
                is_paused, radiant_hero_ids, dire_hero_ids, radiant_team_side,
                clock_confidence, draft_confidence, source_frame_ref,
                screen_state, confirmed)
               VALUES ('42', 1, '2026-07-11T17:07:38+00:00', 600, 0,
                       '[1,2,3,4,5]', '[6,7,8,9,10]', 'team_one',
                       0.94, 0.96, 'unconfirmed.jpg', 'game', 0)"""
        )
        store.connection.commit()

    with pytest.raises(ValueError, match="no trusted complete draft"):
        freeze_draft_map(database, match_id="42", map_number=1, reason="audit")


def test_freeze_draft_map_rejects_invalidated_frame(tmp_path: Path) -> None:
    database = tmp_path / "live.db"
    captured_at = "2026-07-11T17:07:38+00:00"
    with LiveBettingStore(database) as store:
        store.init_schema()
        store.connection.execute(
            """INSERT INTO vision_observations
               (raybet_match_id, map_number, captured_at, game_clock_seconds,
                is_paused, radiant_hero_ids, dire_hero_ids, radiant_team_side,
                clock_confidence, draft_confidence, source_frame_ref,
                screen_state, confirmed)
               VALUES ('42', 1, ?, 600, 0, '[1,2,3,4,5]',
                       '[6,7,8,9,10]', 'team_one', 0.94, 0.96,
                       'invalidated.jpg', 'game', 1)""",
            (captured_at,),
        )
        store.connection.execute(
            """INSERT INTO vision_observation_invalidations
               (raybet_match_id, captured_at, source_frame_ref,
                invalidated_at, reason)
               VALUES ('42', ?, 'invalidated.jpg', ?, 'bad frame')""",
            (captured_at, captured_at),
        )
        store.connection.commit()

    with pytest.raises(ValueError, match="no trusted complete draft"):
        freeze_draft_map(database, match_id="42", map_number=1, reason="audit")


def test_freeze_draft_map_uses_trusted_frame_and_preserves_anchor(
    tmp_path: Path,
) -> None:
    database = tmp_path / "live.db"
    captured_at = datetime(2026, 7, 11, 17, 7, 38, tzinfo=timezone.utc)
    observation = VisionObservation(
        "42",
        1,
        captured_at,
        600,
        False,
        (1, 2, 3, 4, 5),
        (6, 7, 8, 9, 10),
        0.94,
        0.96,
        "trusted.jpg",
        "game",
        "team_one",
    )
    with LiveBettingStore(database) as store:
        store.init_schema()
        assert store.insert_vision_observation(observation)
        before = tuple(
            store.connection.execute(
                """SELECT draft_hash, radiant_hero_ids, dire_hero_ids,
                      anchored_at, source_frame_ref
                 FROM vision_draft_anchors
                WHERE raybet_match_id='42' AND map_number=1"""
            ).fetchone()
        )

    assert (
        freeze_draft_map(
            database,
            match_id="42",
            map_number=1,
            reason="operator freeze",
        )
        == 1
    )

    with LiveBettingStore(database) as store:
        after = store.connection.execute(
            """SELECT draft_hash, radiant_hero_ids, dire_hero_ids,
                      anchored_at, source_frame_ref, status
                 FROM vision_draft_anchors
                WHERE raybet_match_id='42' AND map_number=1"""
        ).fetchone()
        assert tuple(after[:5]) == before
        assert after[5] == "conflict"
        assert (
            store.connection.execute(
                """SELECT COUNT(*) FROM vision_draft_conflicts
                WHERE raybet_match_id='42' AND map_number=1
                  AND source_frame_ref='trusted.jpg'"""
            ).fetchone()[0]
            == 1
        )


def test_vision_invalidation_propagates_to_post_frame_lineage(
    tmp_path: Path,
) -> None:
    database = tmp_path / "live.db"
    backup = tmp_path / "backups" / "before.db"
    before = datetime(2026, 7, 11, 17, 7, 38, tzinfo=timezone.utc)
    invalid = before + timedelta(seconds=10)
    after = invalid + timedelta(seconds=1)
    order_input_ref = "input-decision-after"
    order_key = hashlib.sha256(
        "|".join(
            (
                "42",
                "odds-1",
                "group-1",
                "team_two",
                "winner|map_1|team_two|",
                "strategy-v1",
                order_input_ref,
            )
        ).encode("utf-8")
    ).hexdigest()[:32]
    with LiveBettingStore(database) as store:
        store.init_schema()
        for captured_at, frame in (
            (before.isoformat(), "before.jpg"),
            (invalid.isoformat(), "invalid.jpg"),
        ):
            store.connection.execute(
                """INSERT INTO vision_observations
                   (raybet_match_id, map_number, captured_at, game_clock_seconds,
                    is_paused, radiant_hero_ids, dire_hero_ids,
                    radiant_team_side, clock_confidence, draft_confidence,
                    source_frame_ref, screen_state, confirmed)
                   VALUES ('42', 1, ?, 1392, 0, '[1,2,3,4,5]',
                           '[6,7,8,9,10]', 'team_one', 0.94, 0.96,
                           ?, 'game', 1)""",
                (captured_at, frame),
            )
        for key, decided_at in (
            ("decision-before", before),
            ("decision-after", after),
        ):
            store.connection.execute(
                """INSERT INTO strategy_decisions
                   (decision_key, raybet_match_id, map_number, decided_at,
                    underdog_side, market_probability, model_probability,
                    edge, data_quality, eligible, reason, contributions_json,
                    input_ref, strategy_version)
                   VALUES (?, '42', 1, ?, 'team_two', 0.4, 0.5,
                           0.1, 0.8, 1, 'eligible', ?, ?, 'strategy-v1')""",
                (
                    key,
                    decided_at.isoformat(),
                    json.dumps(
                        {
                            "__inputs__": {
                                "strict_live_eligibility": {
                                    "mapping_refs": {"strict_mapping_id": 1}
                                }
                            }
                        },
                        sort_keys=True,
                    ),
                    f"input-{key}",
                ),
            )
        store.connection.execute(
            """INSERT INTO shadow_orders
               (order_key, raybet_match_id, strict_mapping_id, odds_id,
                market_key, signaled_at, model_probability, market_probability,
                signal_price, signal_transport_key, signal_transport_at,
                expires_at, signal_odds_group_id, signal_outcome_key,
                signal_identity_verified, stake, status, fill_price, filled_at,
                rejection_reason)
               VALUES (?, '42', 1, 'odds-1',
                       'winner|map_1|team_two|', ?, 0.5, 0.4, 3.0,
                       'transport-after', ?, ?, 'group-1', 'team_two',
                       1, 1.0, 'filled', 3.0, ?, NULL)""",
            (
                order_key,
                after.isoformat(),
                after.isoformat(),
                (after + timedelta(seconds=15)).isoformat(),
                (after + timedelta(seconds=2)).isoformat(),
            ),
        )
        store.connection.execute(
            """INSERT INTO shadow_map_attempts
               (raybet_match_id, map_number, order_key, status, created_at)
               VALUES ('42', 1, ?, 'filled', ?)""",
            (order_key, after.isoformat()),
        )
        store.connection.execute(
            """INSERT INTO settlements
               (order_key, result, return_units, settled_at, evidence_ref,
                review_required)
               VALUES (?, 'win', 2.0, ?, 'result:42:1', 0)""",
            (order_key, (after + timedelta(hours=1)).isoformat()),
        )
        store.connection.execute(
            """INSERT INTO settlement_reconciliations
               (raybet_match_id, map_number, dota_match_id,
                raybet_winner_side, opendota_winner_side,
                raybet_evidence_ref, opendota_evidence_ref, status, reason,
                first_observed_at, updated_at)
               VALUES ('42', 1, 4242, 'team_two', 'team_two',
                       'raybet:42:1', 'opendota:4242', 'confirmed', 'matched',
                       ?, ?)""",
            (after.isoformat(), after.isoformat()),
        )
        store.connection.execute(
            """INSERT INTO notification_outbox
               (order_key, event_type, channel, status, recipient, message_id,
                payload_json, statistics_cutoff, template_version,
                attempt_count, next_attempt_at, created_at, updated_at)
               VALUES (?, 'filled', 'email', 'pending',
                       'test@example.com', 'message-1', '{}', ?, 'v1',
                       0, ?, ?, ?)""",
            (
                order_key,
                after.isoformat(),
                after.isoformat(),
                after.isoformat(),
                after.isoformat(),
            ),
        )
        store.connection.commit()

    count = invalidate(
        database,
        match_id="42",
        map_number=1,
        clock_seconds=1392,
        after=before.isoformat(),
        reason="stale_clock_republished_with_new_capture_time",
        backup=backup,
    )

    assert count == 1
    with LiveBettingStore(database) as store:
        invalidations = store.connection.execute(
            """SELECT dependent_type, dependent_key
                 FROM vision_derived_invalidations
                ORDER BY dependent_type, dependent_key"""
        ).fetchall()
        assert [tuple(row) for row in invalidations] == [
            ("shadow_order", order_key),
            ("strategy_decision", "decision-after"),
        ]
        assert (
            store.connection.execute(
                """SELECT COUNT(*) FROM vision_derived_invalidations
                WHERE dependent_type='strategy_decision'
                  AND dependent_key='decision-before'"""
            ).fetchone()[0]
            == 0
        )
        settlement = store.connection.execute(
            """SELECT result, return_units, review_required FROM settlements
                WHERE order_key=?""",
            (order_key,),
        ).fetchone()
        assert tuple(settlement) == ("win", 2.0, 1)
        reconciliation = store.connection.execute(
            """SELECT status, reason FROM settlement_reconciliations
                WHERE raybet_match_id='42' AND map_number=1"""
        ).fetchone()
        assert tuple(reconciliation) == (
            "manual_review",
            "vision_observation_invalidated",
        )
        outbox = store.connection.execute(
            """SELECT status, last_error FROM notification_outbox
                WHERE order_key=?""",
            (order_key,),
        ).fetchone()
        assert tuple(outbox) == ("dead_letter", "vision_observation_invalidated")
        blocked = SimpleNamespace(
            decision_key="decision-new-blocked",
            raybet_match_id="42",
            map_number=1,
            decided_at=after,
            underdog_side="team_one",
            market_probability=0.4,
            model_probability=0.5,
            edge=0.1,
            data_quality=0.8,
            eligible=True,
            reason="eligible",
            contributions={},
            input_ref="new-input",
            strategy_version="strategy-v1",
        )
        assert not store.insert_decision(blocked)
        store.connection.execute(
            """INSERT INTO vision_observations
               (raybet_match_id, map_number, captured_at, game_clock_seconds,
                is_paused, radiant_hero_ids, dire_hero_ids,
                radiant_team_side, clock_confidence, draft_confidence,
                source_frame_ref, screen_state, confirmed)
               VALUES ('42', 1, ?, 1393, 0, '[1,2,3,4,5]',
                       '[6,7,8,9,10]', 'team_one', 0.94, 0.96,
                       'replacement.jpg', 'game', 1)""",
            ((after + timedelta(seconds=1)).isoformat(),),
        )
        store.connection.commit()
        replacement = SimpleNamespace(
            **{
                **blocked.__dict__,
                "decision_key": "decision-after-replacement",
                "decided_at": after + timedelta(seconds=2),
            }
        )
        assert store.insert_decision(replacement)


class FakeProcess:
    def __init__(self) -> None:
        self.running = True
        self.terminated = False

    def poll(self) -> int | None:
        return None if self.running else 0

    def terminate(self) -> None:
        self.terminated = True
        self.running = False

    def wait(self, timeout: float) -> int:
        del timeout
        self.running = False
        return 0


def test_supervisor_requires_live_child_and_current_output(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    output = tmp_path / "42.jsonl"
    output.write_text("old output\n", encoding="utf-8")
    baseline = (output.stat().st_size, output.stat().st_mtime_ns)
    child = FakeProcess()
    children = {"42": (child, StringIO(), StringIO())}

    status, details, error = supervisor_health(
        {"42"},
        children,
        tmp_path,
        started_at={"42": now},
        output_baselines={"42": baseline},
        now=now,
    )

    assert status == "starting"
    assert error is None
    assert details["desired_watchers"] == 1
    assert details["running_watchers"] == 1
    assert details["producing_watchers"] == 0
    assert details["watchers"]["42"]["reason"] == "awaiting_first_output"

    output.write_text("old output\nnew frame\n", encoding="utf-8")
    os.utime(output, (now.timestamp(), now.timestamp()))
    status, details, error = supervisor_health(
        {"42"},
        children,
        tmp_path,
        started_at={"42": now},
        output_baselines={"42": baseline},
        now=now,
    )

    assert status == "healthy"
    assert error is None
    assert details["producing_watchers"] == 1
    assert details["watchers"]["42"]["state"] == "producing"


def test_supervisor_rejects_dead_or_stale_watchers(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    started = now - timedelta(minutes=5)
    output = tmp_path / "42.jsonl"
    output.write_text("frame\n", encoding="utf-8")
    os.utime(output, (started.timestamp(), started.timestamp()))
    children = {"42": (FakeProcess(), StringIO(), StringIO())}

    status, details, error = supervisor_health(
        {"42"}, children, tmp_path, started_at={"42": started}, now=now
    )

    assert status == "unhealthy"
    assert error == "watchers not producing fresh output: 42"
    assert details["running_watchers"] == 1
    assert details["producing_watchers"] == 0
    assert details["watchers"]["42"]["reason"] == "output_stale"

    children["42"][0].running = False
    status, details, error = supervisor_health(
        {"42"}, children, tmp_path, started_at={"42": started}, now=now
    )

    assert status == "unhealthy"
    assert error == "watchers not running: 42"
    assert details["running_watchers"] == 0
    assert details["watchers"]["42"]["state"] == "desired"


def test_supervisor_terminates_watchers_that_are_no_longer_active() -> None:
    process = FakeProcess()
    stdout = StringIO()
    stderr = StringIO()
    children = {"42": (process, stdout, stderr)}

    reap_children(children, set())

    assert process.terminated
    assert stdout.closed and stderr.closed
    assert children == {}


def test_observation_defaults_are_anchored_in_predictor() -> None:
    expected = ROOT / "data" / "live_betting" / "live_observations"
    assert WATCHER_OBSERVATION_DIR == expected
    assert SUPERVISOR_OBSERVATION_DIR == expected
