from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from database.session import PostgresSession
from event_intelligence.live_draft_prospective_bridge import (
    LOCK_CONFIRMATION,
    LiveDraftProspectiveBridgeRepository,
    generate_live_draft_prediction,
)
from event_intelligence.prospective_team_rating import ProspectiveTeamRatingRepository
from live_betting.live_match_state import DraftSlotInput, save_live_draft_mapping
from live_betting.stratz_rosh_client import StratzRoshError
from tests.integration.postgres.test_prospective_rosh_operational_collector import (
    FIXTURE,
    FixtureTransport,
)
from tests.integration.postgres.test_prospective_team_rating_producer import (
    TARGET_ORIGIN,
    _seed_formal_data,
    _store_seed,
)


def _slots(*, reverse: bool = False) -> list[DraftSlotInput]:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    heroes = [*fixture["radiant_heroes"], *fixture["dire_heroes"]]
    if reverse:
        heroes = list(reversed(heroes))
    return [
        DraftSlotInput(
            team_id=10 if index < 5 else 20,
            side="radiant" if index < 5 else "dire",
            position=index % 5 + 1,
            hero_id=int(hero_id),
            player_id=None,
        )
        for index, hero_id in enumerate(heroes)
    ]


class FailingTransport:
    def fetch_legacy_lineup_batch(self, *_args: object, **_kwargs: object) -> None:
        raise StratzRoshError("STRATZ unavailable")


def test_locked_live_draft_paired_and_p0_only_are_immutable(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    _seed_formal_data(postgres_engine, history_count=5, target_count=0)
    session = PostgresSession(postgres_engine)
    lock_time = TARGET_ORIGIN - timedelta(minutes=30)
    try:
        _store_seed(session, ProspectiveTeamRatingRepository(session))
        session.execute(
            """INSERT INTO raybet_matches
               (raybet_match_id, status, raw_json, updated_at)
               VALUES (?, ?, ?, ?)""",
            ("raybet-live-bridge", "2", "{}", lock_time.isoformat()),
        )
        first_mapping = save_live_draft_mapping(
            session,
            raybet_match_id="raybet-live-bridge",
            map_number=1,
            slots=_slots(),
            is_locked=True,
            actor="local-operator",
            created_at=lock_time,
        )
        repository = LiveDraftProspectiveBridgeRepository(session)
        paired = generate_live_draft_prediction(
            repository,
            FixtureTransport(),
            artifact_root=tmp_path / "artifacts",
            raybet_match_id="raybet-live-bridge",
            map_number=1,
            mapping_version=first_mapping["version"],
            operator_identity="local-operator",
            confirmation_text=LOCK_CONFIRMATION,
            confirmed_at=lock_time + timedelta(seconds=2),
            game_clock_seconds=120,
            draft_state_marker="in_game",
        )
        repeated = generate_live_draft_prediction(
            repository,
            FailingTransport(),
            artifact_root=tmp_path / "artifacts",
            raybet_match_id="raybet-live-bridge",
            map_number=1,
            mapping_version=first_mapping["version"],
            operator_identity="local-operator",
            confirmation_text=LOCK_CONFIRMATION,
            confirmed_at=lock_time + timedelta(seconds=3),
            draft_state_marker="draft_complete",
        )

        assert paired["status"] == "created"
        assert paired["prediction"]["record_status"] == "paired"
        assert paired["prediction"]["p1_probability"] is not None
        assert paired["prediction"]["causal_evidence"]["causal_status"] == "eligible"
        assert repeated["status"] == "unchanged"
        assert repeated["prediction"]["prediction_hash"] == paired["prediction"]["prediction_hash"]

        session.execute(
            "UPDATE raybet_matches SET status=? WHERE raybet_match_id=?",
            ("unknown", "raybet-live-bridge"),
        )
        second_mapping = save_live_draft_mapping(
            session,
            raybet_match_id="raybet-live-bridge",
            map_number=1,
            slots=_slots(reverse=True),
            is_locked=True,
            actor="local-operator",
            created_at=lock_time + timedelta(minutes=1),
        )
        p0_only = generate_live_draft_prediction(
            repository,
            FailingTransport(),
            artifact_root=tmp_path / "artifacts",
            raybet_match_id="raybet-live-bridge",
            map_number=1,
            mapping_version=second_mapping["version"],
            operator_identity="local-operator",
            confirmation_text=LOCK_CONFIRMATION,
            confirmed_at=lock_time + timedelta(minutes=1, seconds=2),
            draft_state_marker=None,
        )
        assert p0_only["prediction"]["record_status"] == "p0_only"
        assert p0_only["prediction"]["missing_reason"] == "prospective_rosh_evidence_unavailable"
        assert p0_only["prediction"]["causal_evidence"]["causal_status"] == "unverified"

        session.execute(
            "UPDATE raybet_matches SET status=? WHERE raybet_match_id=?",
            ("completed", "raybet-live-bridge"),
        )
        third_mapping = save_live_draft_mapping(
            session,
            raybet_match_id="raybet-live-bridge",
            map_number=1,
            slots=_slots(),
            is_locked=True,
            actor="local-operator",
            created_at=lock_time + timedelta(minutes=2),
        )
        with pytest.raises(ValueError, match="match_lifecycle_ended"):
            generate_live_draft_prediction(
                repository,
                FailingTransport(),
                artifact_root=tmp_path / "artifacts",
                raybet_match_id="raybet-live-bridge",
                map_number=1,
                mapping_version=third_mapping["version"],
                operator_identity="local-operator",
                confirmation_text=LOCK_CONFIRMATION,
                confirmed_at=lock_time + timedelta(minutes=2, seconds=2),
                game_clock_seconds=240,
                draft_state_marker="in_game",
            )
        assert session.execute(
            "SELECT COUNT(*) FROM live_draft_prospective_predictions"
        ).scalar_one() == 2

        with pytest.raises(DBAPIError, match="append-only"):
            with session.transaction():
                session.execute(
                    "UPDATE live_draft_prospective_predictions SET support=support+1"
                )
    finally:
        session.close()

    columns = {
        column["name"]
        for column in inspect(postgres_engine).get_columns(
            "live_draft_prospective_predictions"
        )
    }
    assert "official_match_id" not in columns
    assert "dota_match_id" not in columns


def test_unlocked_mapping_cannot_enter_live_draft_prediction(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    session = PostgresSession(postgres_engine)
    try:
        mapping = save_live_draft_mapping(
            session,
            raybet_match_id="unlocked-live-bridge",
            map_number=1,
            slots=_slots(),
            is_locked=False,
            actor="local-operator",
            created_at=TARGET_ORIGIN,
        )
        with pytest.raises(ValueError, match="locked"):
            generate_live_draft_prediction(
                LiveDraftProspectiveBridgeRepository(session),
                FailingTransport(),
                artifact_root=tmp_path / "artifacts",
                raybet_match_id="unlocked-live-bridge",
                map_number=1,
                mapping_version=mapping["version"],
                operator_identity="local-operator",
                confirmation_text=LOCK_CONFIRMATION,
                confirmed_at=TARGET_ORIGIN + timedelta(seconds=1),
            )
    finally:
        session.close()
