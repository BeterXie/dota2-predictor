from __future__ import annotations
# ruff: noqa: F821

import contextlib
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

import scripts.supervise_raybet_streams as visual_supervisor
import scripts.watch_raybet_stream as visual_watcher
from scripts.supervise_raybet_streams import (
    MAX_CONCURRENT_WATCHERS,
    WATCHER_MAX_START_FAILURES,
    active_match_evidence,
    active_matches,
    reap_children,
    record_supervisor_health,
    resolve_data_paths as resolve_supervisor_data_paths,
    run_evidence_retention,
    spawn_watcher,
    startable_matches,
    supervisor_health,
    watcher_retry_after_failure,
    watcher_command,
)
from scripts.invalidate_vision_observations import freeze_draft_map, invalidate
from scripts.watch_raybet_stream import (
    ALLOWED_STREAM_HOSTS,
    ROOT,
    _meaningful,
    _draft_for_tracking,
    _sanitized_stream_location,
    _should_persist_frame,
    _suppress_native_video_stderr,
    _validate_stream_url,
    _write_evidence_frame,
    _write_capture_heartbeat,
    capture_heartbeat_path,
    completion_check_due,
    current_frame_comeback_state,
    current_frame_clock_fields,
    allow_live_hud_tracking,
    match_is_complete,
    match_source,
    resolve_source,
    resolve_data_paths as resolve_watcher_data_paths,
)
from contracts.live_observation import ComebackState, LiveObservation
from vision.clock_reader import ClockReading
from vision.hero_recognizer import DraftReading
from vision.hud_reader import HudFrameReading
from vision.layout_selector import LayoutSelection
from vision.layouts import EPL_MASTERS_LIVE, STANDARD_DOTA_HUD
from vision.map_state import ConfirmedClock
from vision.scoreboard_reader import (
    NetWorthAdvantageReading,
    NetWorthAdvantageTracker,
    ReplayGateReading,
    ScoreboardReading,
    ScoreboardTracker,
)
from vision.map_state import MapStateTracker
from live_betting.storage import LiveBettingStore
from live_betting.shadow_monitor import (
    _bind_source_comeback_state,
    _source_comeback_state_index,
)
from live_betting.process_control import TerminationResult
from live_betting.sanitize import (
    PUBLIC_STREAM_EVIDENCE_KEY,
    public_stream_evidence,
)
from live_betting.vision import VisionObservation
from live_betting.vision_frame_registry import publish_vision_frame_bytes


STREAM_URL = "https://play.ehome.gg/live.m3u8"


def _insert_test_strict_mapping(
    store: LiveBettingStore,
    *,
    raybet_match_id: str,
    map_number: int,
    event_id: str,
    available_at: str,
) -> int:
    store.connection.execute(
        "CREATE TABLE IF NOT EXISTS event_registry (event_id TEXT PRIMARY KEY)"
    )
    store.connection.execute(
        "INSERT OR IGNORE INTO event_registry (event_id) VALUES (?)",
        (event_id,),
    )
    identity_json = "{}"
    identity_hash = hashlib.sha256(identity_json.encode("utf-8")).hexdigest()
    cursor = store.connection.execute(
        """INSERT INTO strict_live_map_mappings
           (raybet_match_id, map_number, event_id, team_one_id, team_two_id,
            canonical_team_one_id, canonical_team_one_name,
            canonical_team_two_id, canonical_team_two_name,
            canonical_identity_json, canonical_identity_hash,
            crosswalk_evidence_json, crosswalk_evidence_hash, stage_scope,
            scheduled_at_utc, raybet_best_of, raybet_identity_json,
            raybet_identity_hash, raybet_metadata_updated_at, source,
            evidence_json, evidence_hash, mapping_version, acceptance_mode,
            automatic_approval_id, accepted_by, accepted_at, recorded_at,
            created_at)
           VALUES (?, ?, ?, 101, 202, 10, 'Canonical One',
                   20, 'Canonical Two', ?, ?, ?, ?, 'main_event', ?, 5,
                   ?, ?, ?, 'test', ?, ?, 'test-v1', 'manual_exact', NULL,
                   'test', ?, ?, ?)""",
        (
            raybet_match_id,
            map_number,
            event_id,
            identity_json,
            identity_hash,
            identity_json,
            identity_hash,
            available_at,
            identity_json,
            identity_hash,
            available_at,
            identity_json,
            identity_hash,
            available_at,
            available_at,
            available_at,
        ),
    )
    return int(cursor.lastrowid)


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


def test_visual_supervisor_and_database_watcher_hold_lifetime_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    held: list[Path] = []

    @contextlib.contextmanager
    def authority(path: Path):
        held.append(path)
        try:
            yield
        finally:
            held.remove(path)

    supervisor_args = SimpleNamespace(database=database)
    monkeypatch.setattr(visual_supervisor, "_parse_args", lambda: supervisor_args)
    monkeypatch.setattr(visual_supervisor, "database_writer_authority", authority)
    monkeypatch.setattr(
        visual_supervisor,
        "_run_cli",
        lambda args: 17 if held == [args.database] else -1,
    )
    assert visual_supervisor.main() == 17
    assert held == []

    watcher_args = SimpleNamespace(database=database)
    parser = SimpleNamespace(error=lambda message: pytest.fail(message))
    monkeypatch.setattr(
        visual_watcher,
        "_parse_args",
        lambda: (parser, watcher_args),
    )
    monkeypatch.setattr(visual_watcher, "database_writer_authority", authority)
    monkeypatch.setattr(
        visual_watcher,
        "_run_cli",
        lambda args: 23 if held == [args.database] else -1,
    )
    assert visual_watcher.main() == 23
    assert held == []


def test_standalone_supervisor_spawns_real_child_without_reacquiring_locks(
    tmp_path: Path,
) -> None:
    database = tmp_path / "candidate.db"
    database.touch()
    probe = tmp_path / "watcher_probe.py"
    probe.write_text(
        "import sys\n"
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "from live_betting.service_coordination import database_writer_authority\n"
        "db = Path(sys.argv[sys.argv.index('--database') + 1])\n"
        "with database_writer_authority(db):\n"
        "    print('watcher-authorized', flush=True)\n"
        "marker = json.loads(os.environ['DOTA2_MANAGER_CHILD_AUTHORITY_V1'])\n"
        "print('watcher-locks=' + json.dumps([\n"
        "    owner['lock_path'] for owner in marker['root_lock_owners']\n"
        "]), flush=True)\n",
        encoding="utf-8",
    )
    command = [
        sys.executable,
        str(probe),
        "--database",
        str(database.resolve()),
    ]

    with database_writer_authority(
        database,
        environ={},
        writer_scanner=lambda _: WriterScanResult((), ()),
    ):
        process, authority = spawn_watcher(
            database,
            command,
            subprocess.PIPE,
            subprocess.PIPE,
        )
        stdout, stderr = process.communicate(timeout=15)
        authority.__exit__(None, None, None)

    assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
    assert b"watcher-authorized" in stdout
    lock_line = next(
        line
        for line in stdout.decode("utf-8").splitlines()
        if line.startswith("watcher-locks=")
    )
    lock_paths = tuple(
        Path(path).resolve()
        for path in json.loads(lock_line.removeprefix("watcher-locks="))
    )
    assert lock_paths == database_service_authority_lock_paths(database)
    assert not set(lock_paths).intersection(database_web_authority_lock_paths(database))


def test_watcher_bind_keyboard_interrupt_terminates_and_cleans_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "watcher-interrupt.db"
    database.touch()
    exited: list[bool] = []

    class Authority:
        def __enter__(self) -> dict[str, str]:
            return {"DOTA2_MANAGER_CHILD_AUTHORITY_V1": "marker"}

        def __exit__(self, *_: object) -> None:
            exited.append(True)

    process = SimpleNamespace(pid=8801, poll=lambda: None)
    monkeypatch.setattr(visual_supervisor, "managed_child_command", lambda value: value)
    monkeypatch.setattr(
        visual_supervisor,
        "delegated_writer_process_environment",
        lambda *_args, **_kwargs: Authority(),
    )
    monkeypatch.setattr(visual_supervisor.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(
        visual_supervisor,
        "bind_manager_child_authority",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(
        visual_supervisor,
        "terminate_subprocess_tree",
        lambda *_args, **_kwargs: TerminationResult(True),
    )

    with pytest.raises(KeyboardInterrupt):
        spawn_watcher(database, [sys.executable, "target.py"], None, None)
    assert exited == [True]


def test_watcher_registration_system_exit_terminates_and_cleans_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "watcher-register.db"
    database.touch()
    exited: list[bool] = []

    class Authority:
        def __enter__(self) -> dict[str, str]:
            return {"DOTA2_MANAGER_CHILD_AUTHORITY_V1": "marker"}

        def __exit__(self, *_: object) -> None:
            exited.append(True)

    process = SimpleNamespace(pid=8802, poll=lambda: None)
    monkeypatch.setattr(visual_supervisor, "managed_child_command", lambda value: value)
    monkeypatch.setattr(
        visual_supervisor,
        "delegated_writer_process_environment",
        lambda *_args, **_kwargs: Authority(),
    )
    monkeypatch.setattr(visual_supervisor.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(
        visual_supervisor,
        "bind_manager_child_authority",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        visual_supervisor,
        "terminate_subprocess_tree",
        lambda *_args, **_kwargs: TerminationResult(True),
    )

    def interrupt_registration(*_: object) -> None:
        raise SystemExit(23)

    with pytest.raises(SystemExit, match="23"):
        spawn_watcher(
            database,
            [sys.executable, "target.py"],
            None,
            None,
            register=interrupt_registration,
        )
    assert exited == [True]

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


def test_visual_supervisor_applies_retention_with_active_match_exclusion(
    tmp_path: Path,
) -> None:
    expected = SimpleNamespace(as_dict=lambda: {"status": "ok", "deleted_files": 2})
    with patch(
        "scripts.supervise_raybet_streams.prune_vision_evidence",
        return_value=expected,
    ) as prune:
        result = run_evidence_retention(
            tmp_path / "live.db", tmp_path / "evidence", {"42", "99"}
        )
    assert result == {"status": "ok", "deleted_files": 2}
    prune.assert_called_once_with(
        tmp_path / "live.db",
        tmp_path / "evidence",
        excluded_match_ids={"42", "99"},
        dry_run=False,
    )


def _source_database(
    tmp_path: Path,
    raw: dict,
    rows: list[tuple],
    *,
    status: str = "2",
    updated_at: str = "2026-07-14T01:00:00+00:00",
) -> Path:
    stored_raw = dict(raw)
    stored_raw[PUBLIC_STREAM_EVIDENCE_KEY] = public_stream_evidence(STREAM_URL)
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
                json.dumps(stored_raw),
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


def _audited_source_database(tmp_path: Path, raw: dict) -> Path:
    database = tmp_path / "audited-live.db"
    with LiveBettingStore(database) as store:
        store.init_schema()
        store.connection.execute(
            """INSERT INTO raybet_matches
               (raybet_match_id, best_of, status, live_url, raw_json, updated_at)
               VALUES ('42', 3, '2', ?, ?, '2026-07-14T01:00:00+00:00')""",
            (STREAM_URL, json.dumps(raw)),
        )
        store.connection.commit()
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
    database = _audited_source_database(tmp_path, raw)
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
        client.match_odds_response.return_value = response
        assert match_source(database, "42", refresh_url=True) == (signed, 1)
        client.match_odds_response.assert_called_once_with("42")

    connection = sqlite3.connect(database)
    try:
        stored = connection.execute(
            "SELECT live_url, raw_json FROM raybet_matches WHERE raybet_match_id='42'"
        ).fetchone()
    finally:
        connection.close()
    assert stored[0] == STREAM_URL
    assert "EPHEMERAL_TOKEN" not in stored[1]


def test_match_source_rejects_legacy_query_stripped_stream_without_provenance(
    tmp_path: Path,
) -> None:
    database = _source_database(tmp_path, _raybet_payload(), [])
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE raybet_matches SET raw_json='{}' WHERE raybet_match_id='42'"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ValueError, match="unsigned provenance is missing"):
        match_source(database, "42")


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


def test_signed_stream_query_is_removed_from_diagnostics() -> None:
    marker = "SIGNED_QUERY_MARKER_9f71"
    url = f"https://qplay.ehome.gg/live/42.m3u8?auth_key={marker}"

    assert _sanitized_stream_location(url) == "qplay.ehome.gg/live/42.m3u8"
    assert marker not in str(_sanitized_stream_location(url))


def test_native_video_stderr_is_suppressed(capfd: pytest.CaptureFixture[str]) -> None:
    marker = b"SIGNED_NATIVE_MARKER_0c2a"

    with _suppress_native_video_stderr():
        os.write(2, marker + b"\n")

    captured = capfd.readouterr()
    assert marker.decode() not in captured.err


def test_watcher_cli_never_prints_exception_secret(
    capsys: pytest.CaptureFixture[str],
) -> None:
    marker = "SIGNED_EXCEPTION_MARKER_8b41"
    args = SimpleNamespace(
        database=None,
        url=f"https://play.ehome.gg/live/42.m3u8?token={marker}",
    )
    parser = object()
    with (
        patch.object(visual_watcher, "_parse_args", return_value=(parser, args)),
        patch.object(
            visual_watcher,
            "_run_cli",
            side_effect=RuntimeError(f"backend failed for {marker}"),
        ),
    ):
        assert visual_watcher.main() == 2

    captured = capsys.readouterr()
    assert marker not in captured.out
    assert marker not in captured.err
    assert "watcher_failed" in captured.err
    assert "play.ehome.gg/live/42.m3u8" in captured.err


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
    database = _audited_source_database(tmp_path, {})
    response = {
        "result": {
            "id": 42,
            "game_id": 151,
            "live_url": "http://127.0.0.1:8000/private.m3u8",
        }
    }
    with patch("scripts.watch_raybet_stream.RayBetClient") as client_type:
        client = client_type.return_value.__enter__.return_value
        client.match_odds_response.return_value = response
        with pytest.raises(ValueError, match="invalid fresh live URL"):
            match_source(database, "42", refresh_url=True)
        client.match_odds_response.assert_called_once_with("42")


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
    evidence_index = command.index("--evidence-dir")
    assert command[evidence_index + 1] == str(tmp_path / "evidence")


def _upsert_supervisor_match(
    store: LiveBettingStore,
    match_id: str,
    *,
    status: str,
    updated_at: datetime,
    game_id: int = 151,
    live_url: str | None = None,
) -> None:
    row = {
        "id": match_id,
        "game_id": game_id,
        "status": status,
        "round": "bo3",
        "team": [
            {"pos": 1, "team_id": 101, "team_name": "Team One"},
            {"pos": 2, "team_id": 202, "team_name": "Team Two"},
        ],
    }
    if live_url is not None:
        row["live_url"] = live_url
    store.upsert_raybet_match(
        row,
        updated_at,
        public_live_url=live_url,
    )


def test_supervisor_probes_signed_only_live_match_without_browser_video(
    tmp_path: Path,
) -> None:
    database = tmp_path / "live.db"
    now = datetime(2026, 7, 15, 14, 2, tzinfo=timezone.utc)
    signed_url = (
        "https://qplay.ehome.gg/live/42.m3u8?auth_key=EPHEMERAL_TOKEN"
    )
    with LiveBettingStore(database) as store:
        store.init_schema()
        _upsert_supervisor_match(
            store,
            "42",
            status="2",
            updated_at=now - timedelta(seconds=10),
            live_url=signed_url,
        )
        stored = store.connection.execute(
            "SELECT live_url, raw_json FROM raybet_matches WHERE raybet_match_id='42'"
        ).fetchone()

    assert stored["live_url"] is None
    assert "EPHEMERAL_TOKEN" not in stored["raw_json"]
    assert active_match_evidence(database, now=now) == {
        "42": "ephemeral_stream_refresh_probe"
    }
    assert startable_matches(
        {"42"}, {}, {}, now=now
    ) == ["42"]
    assert "--refresh-url" in watcher_command(
        database,
        "42",
        tmp_path / "observations",
        tmp_path / "evidence",
    )


def test_supervisor_selects_only_fresh_live_provider_rows(tmp_path: Path) -> None:
    database = tmp_path / "live.db"
    now = datetime(2026, 7, 15, 14, 2, tzinfo=timezone.utc)
    with LiveBettingStore(database) as store:
        store.init_schema()
        rows = [
            ("live", "2", now - timedelta(seconds=10), 151, STREAM_URL),
            ("prematch", "1", now - timedelta(seconds=10), 151, None),
            ("stale", "2", now - timedelta(minutes=2), 151, None),
            ("completed", "3", now - timedelta(seconds=10), 151, None),
            ("wrong-game", "2", now - timedelta(seconds=10), 152, None),
            ("future", "2", now + timedelta(seconds=1), 151, None),
        ]
        for match_id, status, updated_at, game_id, live_url in rows:
            _upsert_supervisor_match(
                store,
                match_id,
                status=status,
                updated_at=updated_at,
                game_id=game_id,
                live_url=live_url,
            )

    assert active_matches(database, now=now) == ["live"]
    assert active_match_evidence(database, now=now) == {
        "live": "verified_public_stream"
    }


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


def test_epl_draft_tracking_uses_current_or_last_confirmed_early_clock() -> None:
    draft = DraftReading((1, 2, 3, 4, 5), (6, 7, 8, 9, 10), 0.91)
    early = ConfirmedClock(1, 180, False, 0.94)

    assert _draft_for_tracking(EPL_MASTERS_LIVE, draft, early, None) == draft
    assert _draft_for_tracking(EPL_MASTERS_LIVE, draft, None, early) == draft


@pytest.mark.parametrize(
    ("confirmed_clock", "last_clock"),
    [
        (ConfirmedClock(1, 181, False, 0.94), None),
        (None, ConfirmedClock(1, 181, False, 0.94)),
        (None, None),
    ],
)
def test_epl_draft_tracking_fails_closed_without_safe_early_clock(
    confirmed_clock: ConfirmedClock | None,
    last_clock: ConfirmedClock | None,
) -> None:
    draft = DraftReading((1, 2, 3, 4, 5), (6, 7, 8, 9, 10), 0.91)

    assert _draft_for_tracking(
        EPL_MASTERS_LIVE,
        draft,
        confirmed_clock,
        last_clock,
    ) == DraftReading((), (), 0.0)


def test_standard_layout_draft_tracking_has_no_clock_window() -> None:
    draft = DraftReading((1, 2, 3, 4, 5), (6, 7, 8, 9, 10), 0.91)

    assert _draft_for_tracking(STANDARD_DOTA_HUD, draft, None, None) == draft


def test_comeback_state_requires_current_confirmed_hud_inputs() -> None:
    clock = ConfirmedClock(1, 1_800, False, 0.96)
    scoreboard = ScoreboardReading(18, 25, 0.95)
    advantage = NetWorthAdvantageReading("dire", 5_000, 5_999, 0.94)

    unavailable = current_frame_comeback_state(None, scoreboard, advantage)
    assert unavailable.status == "unavailable"
    assert unavailable.unavailable_reason == "hud_clock_unconfirmed"

    unavailable = current_frame_comeback_state(clock, None, advantage)
    assert unavailable.status == "unavailable"
    assert unavailable.unavailable_reason == "hud_kill_score_unconfirmed"

    unavailable = current_frame_comeback_state(clock, scoreboard, None)
    assert unavailable.status == "unavailable"
    assert unavailable.unavailable_reason == (
        "hud_net_worth_advantage_unconfirmed"
    )

    available = current_frame_comeback_state(clock, scoreboard, advantage)
    assert available.status == "available"
    assert available.source == "vision_hud"
    assert available.confidence == 0.94
    assert available.radiant_kills == 18
    assert available.dire_kills == 25
    assert available.radiant_net_worth is None
    assert available.dire_net_worth is None
    assert available.net_worth_advantage_side == "dire"
    assert available.net_worth_advantage_min == 5_000
    assert available.net_worth_advantage_max == 5_999


@pytest.mark.parametrize("status", ["replay", "untrusted"])
def test_replay_gate_resets_all_live_hud_trackers(status: str) -> None:
    clock_tracker = MapStateTracker()
    clock_tracker.reset_map(2)
    clock_tracker.last_seconds = 1_800
    scoreboard_tracker = ScoreboardTracker()
    scoreboard_tracker.update(ScoreboardReading(18, 25, 0.95))
    advantage_tracker = NetWorthAdvantageTracker()
    advantage_tracker.update(NetWorthAdvantageReading("dire", 5_000, 5_999, 0.95))

    assert not allow_live_hud_tracking(
        ReplayGateReading(status, 0.95),
        map_number=2,
        clock_tracker=clock_tracker,
        scoreboard_tracker=scoreboard_tracker,
        advantage_tracker=advantage_tracker,
    )
    assert clock_tracker.last_seconds is None
    assert scoreboard_tracker.update(ScoreboardReading(18, 25, 0.95)) is None
    assert (
        advantage_tracker.update(
            NetWorthAdvantageReading("dire", 5_000, 5_999, 0.95)
        )
        is None
    )


def test_jsonl_comeback_state_rebind_requires_exact_frame_identity(
    tmp_path: Path,
) -> None:
    captured_at = datetime.now(timezone.utc)
    state = visual_watcher.current_frame_comeback_state(
        ConfirmedClock(1, 1_800, False, 0.96),
        ScoreboardReading(18, 25, 0.95),
        NetWorthAdvantageReading("dire", 5_000, 5_999, 0.94),
    )
    contract = LiveObservation(
        raybet_match_id="42",
        map_number=1,
        captured_at_utc=captured_at,
        source_frame_ref="vision-frame:source",
        comeback_state=state,
    )
    path = tmp_path / "42.jsonl"
    path.write_text(contract.model_dump_json() + "\n", encoding="utf-8")
    stored = VisionObservation(
        "42",
        1,
        captured_at,
        None,
        None,
        (),
        (),
        0.0,
        0.0,
        "vision-frame:source",
        "game",
    )

    states = _source_comeback_state_index(path)
    rebound = _bind_source_comeback_state(stored, states)
    mismatched = _bind_source_comeback_state(
        VisionObservation(
            **{
                **stored.__dict__,
                "source_frame_ref": "vision-frame:different",
            }
        ),
        states,
    )

    assert rebound.comeback_state.is_available
    assert rebound.comeback_state.radiant_kills == 18
    assert not mismatched.comeback_state.is_available


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

    state_available = row(600, False)
    state_available.comeback_state = ComebackState(
        status="available",
        source="vision_hud",
        confidence=0.95,
        radiant_kills=18,
        dire_kills=25,
        radiant_net_worth=42_000,
        dire_net_worth=49_500,
        unavailable_reason=None,
    )
    assert _meaningful(confirmed, state_available)


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
    image = np.full((20, 30, 3), 127, dtype=np.uint8)

    receipt = _write_evidence_frame(tmp_path, image)

    assert receipt.frame_ref == f"vision-frame:sha256:{receipt.content_sha256}"
    assert receipt.storage_path.name == f"{receipt.content_sha256}.jpg"
    assert receipt.storage_path.stat().st_size == receipt.byte_length


def test_failed_evidence_write_cannot_publish_a_frame_reference(
    tmp_path: Path,
) -> None:
    with patch(
        "scripts.watch_raybet_stream.cv2.imencode",
        return_value=(False, None),
    ):
        with pytest.raises(OSError, match="content-addressed evidence frame"):
            _write_evidence_frame(tmp_path, object())
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
                """INSERT INTO vision_observations
                   (raybet_match_id, map_number, captured_at,
                    game_clock_seconds, is_paused, radiant_hero_ids,
                    dire_hero_ids, radiant_team_side, clock_confidence,
                    draft_confidence, source_frame_ref, screen_state, confirmed)
                   VALUES ('42', 1, ?, 1392, 0, '[1,2,3,4,5]',
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
    receipt = publish_vision_frame_bytes(tmp_path / "evidence", b"trusted-frame")
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
        receipt.frame_ref,
        "game",
        "team_one",
        source_frame_sha256=receipt.content_sha256,
        source_frame_bytes=receipt.byte_length,
        source_frame_path=str(receipt.storage_path),
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
                  AND source_frame_ref=?""",
                (receipt.frame_ref,),
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
        # Seed pre-v8 rows so this migration audit test can exercise causal
        # invalidation without treating the legacy rows as current authority.
        for trigger in (
            "strategy_decision_draft_authority_insert",
            "strategy_decision_vision_authority_insert",
            "shadow_order_draft_authority_insert",
            "shadow_order_vision_authority_insert",
            "settlements_authority_insert_guard",
            "settlement_reconciliation_authority_insert",
        ):
            store.connection.execute(f'DROP TRIGGER "{trigger}"')
        strict_mapping_id = _insert_test_strict_mapping(
            store,
            raybet_match_id="42",
            map_number=1,
            event_id="event-42",
            available_at=(before - timedelta(days=1)).isoformat(),
        )
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
                                    "mapping_refs": {
                                        "strict_mapping_id": strict_mapping_id
                                    }
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
               VALUES (?, '42', ?, 'odds-1',
                       'winner|map_1|team_two|', ?, 0.5, 0.4, 3.0,
                       'transport-after', ?, ?, 'group-1', 'team_two',
                       1, 1.0, 'filled', 3.0, ?, NULL)""",
            (
                order_key,
                strict_mapping_id,
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
               (raybet_match_id, map_number, strict_mapping_id, dota_match_id,
                raybet_winner_side, opendota_winner_side,
                raybet_evidence_ref, opendota_evidence_ref, status, reason,
                first_observed_at, updated_at)
               VALUES ('42', 1, ?, 4242, 'team_two', 'team_two',
                       'raybet:42:1', 'opendota:4242', 'confirmed', 'matched',
                       ?, ?)""",
            (strict_mapping_id, after.isoformat(), after.isoformat()),
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
            "legacy_source_authority_missing",
        )
        outbox = store.connection.execute(
            """SELECT status, last_error FROM notification_outbox
                WHERE order_key=?""",
            (order_key,),
        ).fetchone()
        assert tuple(outbox) == ("dead_letter", "decision_lineage_unavailable")
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
        # A path-only replacement cannot restore v8 eligible authority.
        assert not store.insert_decision(replacement)


class FakeProcess:
    def __init__(self, exit_code: int = 0) -> None:
        self.running = True
        self.terminated = False
        self.exit_code = exit_code

    def poll(self) -> int | None:
        return None if self.running else self.exit_code

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


def test_supervisor_distinguishes_partial_capture_from_a_stall(
    tmp_path: Path,
) -> None:
    now = datetime.now(timezone.utc)
    started = now - timedelta(minutes=5)
    output = tmp_path / "42.jsonl"
    output.write_text("unknown frame\n", encoding="utf-8")
    os.utime(output, (started.timestamp(), started.timestamp()))
    reading = HudFrameReading(
        LayoutSelection(EPL_MASTERS_LIVE, 0.98, True),
        "game",
        0.98,
        ReplayGateReading("untrusted", 0.8),
        ClockReading(None, 0.0, None),
        ScoreboardReading(None, None, 0.0),
        NetWorthAdvantageReading(None, None, None, 0.0),
        DraftReading((), (), 0.0),
    )
    _write_capture_heartbeat(
        output,
        match_id="42",
        captured_at=now,
        capture_status="capturing_partial",
        diagnostics=reading.diagnostics,
    )
    children = {"42": (FakeProcess(), StringIO(), StringIO())}

    status, details, error = supervisor_health(
        {"42"}, children, tmp_path, started_at={"42": started}, now=now
    )

    assert status == "degraded"
    assert error is None
    assert details["capturing_watchers"] == 1
    assert details["producing_watchers"] == 0
    assert details["watchers"]["42"]["capture_state"] == "capturing_partial"
    assert details["watchers"]["42"]["blocker_code"] == "replay_gate_untrusted"
    assert details["watchers"]["42"]["layout_profile"] == "epl_masters_live_1080p"

    heartbeat = capture_heartbeat_path(output)
    stale_payload = json.loads(heartbeat.read_text(encoding="utf-8"))
    stale_payload["captured_at"] = started.isoformat()
    heartbeat.write_text(json.dumps(stale_payload), encoding="utf-8")
    status, details, error = supervisor_health(
        {"42"}, children, tmp_path, started_at={"42": started}, now=now
    )

    assert status == "unhealthy"
    assert error == "watchers capture stalled: 42"
    assert details["watchers"]["42"]["capture_state"] == "capture_stalled"


def test_supervisor_terminates_watchers_that_are_no_longer_active() -> None:
    process = FakeProcess()
    stdout = StringIO()
    stderr = StringIO()
    children = {"42": (process, stdout, stderr)}

    reap_children(children, set())

    assert process.terminated
    assert stdout.closed and stderr.closed
    assert children == {}


def test_source_refresh_failure_has_bounded_retry_and_visible_health(
    tmp_path: Path,
) -> None:
    failed_at = datetime(2026, 7, 15, 14, 2, tzinfo=timezone.utc)
    process = FakeProcess(exit_code=2)
    process.running = False
    children = {"42": (process, StringIO(), StringIO())}

    assert reap_children(children, {"42"}) == {"42": 2}
    assert children == {}

    retry = watcher_retry_after_failure(
        None,
        exit_code=2,
        produced_output=False,
        failed_at=failed_at,
    )
    status, details, error = supervisor_health(
        {"42"},
        {},
        tmp_path,
        retry_states={"42": retry},
        now=failed_at,
    )
    assert status == "degraded"
    assert error == "watcher startup retry scheduled: 42"
    assert details["reason"] == "watcher_retry_scheduled"
    watcher = details["watchers"]["42"]
    assert watcher["reason"] == "source_refresh_failed_retry_scheduled"
    assert watcher["retry"]["last_exit_code"] == 2
    assert watcher["retry"]["attempts"] == 1
    assert startable_matches(
        {"42"}, {}, {"42": retry}, now=failed_at
    ) == []

    for attempt in range(2, WATCHER_MAX_START_FAILURES + 1):
        assert retry.retry_at is not None
        retry = watcher_retry_after_failure(
            retry,
            exit_code=2,
            produced_output=False,
            failed_at=retry.retry_at,
        )
        assert retry.attempts == attempt

    assert retry.exhausted
    assert retry.retry_at is None
    assert startable_matches(
        {"42"}, {}, {"42": retry}, now=failed_at + timedelta(hours=1)
    ) == []
    status, details, error = supervisor_health(
        {"42"},
        {},
        tmp_path,
        retry_states={"42": retry},
        now=failed_at + timedelta(hours=1),
    )
    assert status == "unhealthy"
    assert error == "watcher retry exhausted: 42"
    assert details["retry_exhausted_match_ids"] == ["42"]
    assert details["watchers"]["42"]["retry"]["exhausted"] is True


def test_supervisor_never_exceeds_global_watcher_limit() -> None:
    now = datetime(2026, 7, 15, 14, 2, tzinfo=timezone.utc)
    active = {str(match_id) for match_id in range(10)}

    selected = startable_matches(active, {}, {}, now=now)

    assert len(selected) == MAX_CONCURRENT_WATCHERS
    children = {
        match_id: (FakeProcess(), StringIO(), StringIO())
        for match_id in selected
    }
    assert startable_matches(active, children, {}, now=now) == []


def test_vision_defaults_follow_the_selected_database(tmp_path: Path) -> None:
    database = tmp_path / "candidate" / "dota2.db"
    supervisor = resolve_supervisor_data_paths(SimpleNamespace(
        database=database,
        output_dir=None,
        evidence_dir=None,
        log_dir=None,
    ))
    watcher = resolve_watcher_data_paths(SimpleNamespace(
        database=database,
        output=None,
        evidence_dir=None,
        match_id="42",
    ))
    live_root = database.resolve().parent / "live_betting"

    assert supervisor.database == database.resolve()
    assert supervisor.output_dir == live_root / "live_observations"
    assert supervisor.evidence_dir == live_root / "live_evidence"
    assert supervisor.log_dir == live_root / "watcher_logs"
    assert watcher.database == database.resolve()
    assert watcher.output == live_root / "live_observations" / "42.jsonl"
    assert watcher.evidence_dir == live_root / "live_evidence"


def test_url_only_watcher_requires_explicit_output_roots() -> None:
    with pytest.raises(ValueError, match="--database is required"):
        resolve_watcher_data_paths(SimpleNamespace(
            database=None,
            output=None,
            evidence_dir=None,
            match_id="42",
        ))
