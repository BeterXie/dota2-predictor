from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from live_betting.storage import LiveBettingStore
from live_betting.vision_retention import (
    RetentionSafetyError,
    prune_vision_evidence,
)
from scripts.cleanup_vision_evidence import main as cleanup_main


NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
RADIANT = "[1,2,3,4,5]"
DIRE = "[6,7,8,9,10]"


def _frame(root: Path, match_id: str, name: str, captured: datetime) -> Path:
    path = root / match_id / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"jpeg-fixture")
    timestamp = captured.timestamp()
    os.utime(path, (timestamp, timestamp))
    return path.resolve()


def _observation(
    store: LiveBettingStore,
    match_id: str,
    frame: Path,
    captured: datetime,
    clock: int,
) -> None:
    store.connection.execute(
        """INSERT INTO vision_observations
           (raybet_match_id, map_number, captured_at, game_clock_seconds,
            is_paused, radiant_hero_ids, dire_hero_ids, radiant_team_side,
            clock_confidence, draft_confidence, source_frame_ref,
            screen_state, confirmed)
           VALUES (?, 1, ?, ?, 0, ?, ?, 'team_one', 0.95, 0.96, ?, 'game', 1)""",
        (match_id, captured.isoformat(), clock, RADIANT, DIRE, str(frame)),
    )


def _database(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / "live.db"
    with LiveBettingStore(database) as store:
        store.init_schema()
    return database


def test_dry_run_then_apply_preserves_lineage_audit_active_and_recent_frames(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    root = tmp_path / "evidence"
    root.mkdir()
    referenced = _frame(root, "42", "referenced.jpg", NOW - timedelta(days=40))
    stale = _frame(root, "42", "stale.jpg", NOW - timedelta(days=20))
    audit = _frame(root, "42", "audit.jpg", NOW - timedelta(days=15))
    over_capacity = _frame(root, "42", "capacity.jpg", NOW - timedelta(days=2))
    recent = _frame(root, "42", "recent.jpg", NOW - timedelta(days=1))
    active = _frame(root, "99", "active.jpg", NOW - timedelta(days=30))
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")

    with LiveBettingStore(database) as store:
        _observation(store, "42", referenced, NOW - timedelta(days=40), 0)
        _observation(store, "42", stale, NOW - timedelta(days=20), 60)
        _observation(store, "42", audit, NOW - timedelta(days=15), 600)
        _observation(store, "42", over_capacity, NOW - timedelta(days=2), 650)
        _observation(store, "42", recent, NOW - timedelta(days=1), 700)
        store.connection.execute(
            """INSERT INTO strategy_decisions
               (decision_key, raybet_match_id, map_number, decided_at,
                underdog_side, market_probability, model_probability, edge,
                data_quality, eligible, reason, contributions_json, input_ref,
                strategy_version)
               VALUES ('decision-1', '42', 1, ?, 'team_one', 0.3, 0.4,
                       0.1, 0.8, 1, 'eligible', ?, 'input', 'test-v1')""",
            (
                (NOW - timedelta(days=39)).isoformat(),
                json.dumps(
                    {
                        "__inputs__": {
                            "vision": {"source_frame_ref": str(referenced)}
                        }
                    }
                ),
            ),
        )
        store.connection.commit()

    dry_run = prune_vision_evidence(
        database,
        root,
        now=NOW,
        ttl=timedelta(days=7),
        max_unprotected_per_match=1,
        excluded_match_ids={"99"},
    )
    assert dry_run.dry_run
    assert set(dry_run.planned_deletions) == {stale, over_capacity}
    assert dry_run.protected_reference_files == 1
    assert dry_run.protected_audit_files == 1
    assert dry_run.protected_active_files == 1
    assert all(path.exists() for path in (referenced, stale, audit, over_capacity, recent, active))

    applied = prune_vision_evidence(
        database,
        root,
        now=NOW,
        ttl=timedelta(days=7),
        max_unprotected_per_match=1,
        excluded_match_ids={"99"},
        dry_run=False,
    )
    assert applied.deleted_files == 2
    assert not stale.exists()
    assert not over_capacity.exists()
    assert all(path.exists() for path in (referenced, audit, recent, active))
    assert outside.exists()


def test_all_persisted_prediction_and_order_lineages_protect_source_frames(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    root = tmp_path / "evidence"
    root.mkdir()
    decision = _frame(root, "d", "decision.jpg", NOW - timedelta(days=30))
    research = _frame(root, "r", "research.jpg", NOW - timedelta(days=30))
    order = _frame(root, "o", "order.jpg", NOW - timedelta(days=30))
    settlement = _frame(root, "s", "settlement.jpg", NOW - timedelta(days=30))

    with LiveBettingStore(database) as store:
        for index, (match_id, frame) in enumerate(
            (("d", decision), ("r", research), ("o", order))
        ):
            _observation(
                store,
                match_id,
                frame,
                NOW - timedelta(days=30, seconds=10 - index),
                100 + index,
            )
        store.connection.execute(
            """INSERT INTO strategy_decisions VALUES
               ('decision-lineage', 'd', 1, ?, 'team_one', 0.3, 0.4,
                0.1, 0.8, 1, 'eligible', ?, 'input', 'test-v1')""",
            (
                (NOW - timedelta(days=30)).isoformat(),
                json.dumps({"vision": {"source_frame_ref": str(decision)}}),
            ),
        )
        transport_at = (NOW - timedelta(days=30)).isoformat()
        store.connection.execute(
            """INSERT INTO odds_transport_observations
               VALUES ('research-transport', 'direct', NULL, 'r', ?, ?,
                       'on_time', 'processed', 1)""",
            (transport_at, "a" * 64),
        )
        store.connection.execute(
            """INSERT INTO research_live_predictions
               (prediction_key, schema_version, raybet_match_id, map_number,
                observed_at, game_clock_seconds, game_minute, selected_side,
                market_probability, market_price, raw_model_probability,
                feature_hash, model_hash, calibration_hash, transport_key,
                transport_hash, radiant_hero_ids_json, dire_hero_ids_json,
                radiant_team_side, strict_mapping_id, clock_source, clock_trust,
                manual_clock_event_id, manual_clock_seconds, manual_clock_trust,
                manual_clock_validation, actionability, gate_status,
                gate_failures_json, input_context_hash, created_at)
               VALUES ('research-prediction', 'research-v1', 'r', 1, ?, 110,
                       1.833, 'team_one', 0.3, 3.0, NULL, NULL, NULL, NULL,
                       'research-transport', ?, ?, ?, 'team_one', 1, 'vision',
                       'trusted_vision', NULL, NULL, 'not_observed',
                       'not_observed', 'research_only', 'failed', '[]', ?, ?)""",
            (transport_at, "a" * 64, RADIANT, DIRE, "b" * 64, transport_at),
        )
        store.connection.execute(
            """INSERT INTO shadow_orders
               (order_key, raybet_match_id, strict_mapping_id, odds_id,
                market_key, signaled_at, model_probability, market_probability,
                signal_price, signal_transport_key, signal_transport_at,
                expires_at, signal_odds_group_id, signal_outcome_key,
                signal_identity_verified, stake, status, fill_price, filled_at,
                rejection_reason)
               VALUES ('legacy-order', 'o', 1, 'odds', 'winner:map_1:team_one',
                       ?, 0.4, 0.3, 3.0, 'transport', ?, ?, 'group', 'team_one',
                       1, 1.0, 'pending', NULL, NULL, NULL)""",
            (
                transport_at,
                transport_at,
                (NOW - timedelta(days=30) + timedelta(seconds=15)).isoformat(),
            ),
        )
        store.connection.execute(
            """INSERT INTO shadow_map_attempts
               VALUES ('o', 1, 'legacy-order', 'pending', ?)""",
            (transport_at,),
        )
        store.connection.execute(
            """INSERT INTO settlements VALUES
               ('settled-order', 'win', 2.0, ?, ?, 0)""",
            (transport_at, str(settlement)),
        )
        store.connection.commit()

    result = prune_vision_evidence(
        database,
        root,
        now=NOW,
        ttl=timedelta(days=7),
        max_unprotected_per_match=0,
        dry_run=False,
    )
    assert result.deleted_files == 0
    assert result.protected_reference_files == 4
    assert all(path.exists() for path in (decision, research, order, settlement))


def test_incomplete_or_corrupt_lineage_fails_closed_before_deletion(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    stale = _frame(root, "42", "stale.jpg", NOW - timedelta(days=30))
    database = tmp_path / "incomplete.db"
    sqlite3.connect(database).close()

    with pytest.raises(RetentionSafetyError, match="schema is incomplete"):
        prune_vision_evidence(database, root, now=NOW, dry_run=False)
    assert stale.exists()

    database = _database(tmp_path / "complete")
    with LiveBettingStore(database) as store:
        store.connection.execute(
            """INSERT INTO strategy_decisions VALUES
               ('bad-json', '42', 1, ?, 'team_one', 0.3, 0.4, 0.1, 0.8,
                1, 'eligible', '{', 'input', 'test-v1')""",
            (NOW.isoformat(),),
        )
        store.connection.commit()
    with pytest.raises(RetentionSafetyError, match="lineage JSON is invalid"):
        prune_vision_evidence(database, root, now=NOW, dry_run=False)
    assert stale.exists()


def test_capacity_limit_never_deletes_during_ingestion_grace(tmp_path: Path) -> None:
    database = _database(tmp_path)
    root = tmp_path / "evidence"
    root.mkdir()
    recent = _frame(root, "42", "recent.jpg", NOW - timedelta(minutes=30))

    grace = prune_vision_evidence(
        database,
        root,
        now=NOW,
        ttl=timedelta(days=7),
        max_unprotected_per_match=0,
        dry_run=False,
    )
    assert grace.planned_deletions == ()
    assert recent.exists()

    after_grace = prune_vision_evidence(
        database,
        root,
        now=NOW + timedelta(hours=2),
        ttl=timedelta(days=7),
        max_unprotected_per_match=0,
        dry_run=False,
    )
    assert after_grace.deleted_files == 1
    assert not recent.exists()

def test_linked_candidate_is_never_followed_or_deleted(tmp_path: Path) -> None:
    database = _database(tmp_path)
    root = tmp_path / "evidence"
    match_dir = root / "42"
    match_dir.mkdir(parents=True)
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    stale = _frame(root, "43", "stale.jpg", NOW - timedelta(days=30))
    linked = match_dir / "linked.jpg"
    try:
        linked.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are unavailable on this Windows host")

    result = prune_vision_evidence(database, root, now=NOW, dry_run=False)
    assert result.unsafe_paths == 1
    assert result.deleted_files == 0
    assert stale.exists()
    assert outside.read_bytes() == b"outside"


def test_cleanup_cli_is_dry_run_and_reports_fail_closed_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    stale = _frame(root, "42", "stale.jpg", NOW - timedelta(days=30))
    incomplete = tmp_path / "incomplete.db"
    sqlite3.connect(incomplete).close()

    code = cleanup_main([
        "--database",
        str(incomplete),
        "--evidence-dir",
        str(root),
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload == {
        "dry_run": True,
        "error_type": "RetentionSafetyError",
        "status": "error",
    }
    assert stale.exists()
