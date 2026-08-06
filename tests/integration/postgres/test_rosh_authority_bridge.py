from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from database.session import PostgresSession
from event_intelligence.rosh_authority_bridge import (
    audit_rosh_authority_bridge,
    persist_rosh_authority_bridge,
    replay_rosh_authority_bridge_record,
)
from scripts.verify_rosh_authority_bridge import (
    AVAILABLE_AT,
    MATCH_ID,
    _seed_match_authority,
    _seed_runs_and_legacy,
    _target,
)


def test_bridge_replays_hero_positions_without_player_identity(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    inspector = inspect(postgres_engine)
    columns = {
        column["name"]: column
        for column in inspector.get_columns("rosh_authority_bridge_records")
    }
    for name in (
        "radiant_player_ids_json",
        "dire_player_ids_json",
        "player_coverage_count",
    ):
        assert columns[name]["nullable"] is True
    checks = " ".join(
        str(row["sqltext"])
        for row in inspector.get_check_constraints("rosh_authority_bridge_records")
    )
    assert "player_coverage_count = 10" not in checks

    artifact_root = tmp_path / "rosh-artifacts"
    session = PostgresSession(postgres_engine)
    try:
        with session.transaction():
            _seed_match_authority(session)
            _seed_runs_and_legacy(session, artifact_root, 1)
        report = audit_rosh_authority_bridge(
            session,
            artifact_root=artifact_root,
            max_rows=1,
            created_at=AVAILABLE_AT,
            draft_targets={MATCH_ID: _target()},
        )

        assert "player_coverage_complete" not in {
            stage.stage for stage in report.stages
        }
        assert report.stages[-1].support == 1
        assert report.player_identity_support == 0
        assert {
            row.reason: row.support for row in report.player_identity_diagnostics
        } == {
            "player_coverage_incomplete": 1,
            "player_ids_unavailable": 1,
        }
        record = report.eligible_records[0]
        assert record.radiant_player_ids is None
        assert record.dire_player_ids is None
        assert record.player_coverage_count == 0

        persisted = persist_rosh_authority_bridge(session, report)
        assert persisted.inserted_records == 1
        snapshot = replay_rosh_authority_bridge_record(
            session,
            record,
            artifact_root=artifact_root,
        )
        assert snapshot.status == "available"
    finally:
        session.close()
