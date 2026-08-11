from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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
from event_intelligence.prospective_team_rating import (
    ProspectiveTeamRatingRepository,
    build_prospective_team_rating_seed,
)
from event_intelligence.raw_archive import canonical_json_bytes
from event_intelligence.team_rating import TEAM_RATING_VERSION, TeamRatingConfig
from live_betting.live_match_state import DraftSlotInput, save_live_draft_mapping
from live_betting.map_decision_checkpoints import (
    latest_map_checkpoints,
    record_due_checkpoints,
    record_pregame_checkpoint,
)
from live_betting.stratz_rosh_client import (
    FetchedLegacyRoshBatch,
    StratzRoshError,
)
from live_betting.vision_frame_registry import (
    publish_vision_frame_bytes,
    register_vision_frame_artifact,
)
from scripts.freeze_prospective_team_rating_seed import freeze_seed
from prematch.stratz_rosh import build_rosh_query_requests


UTC = timezone.utc
TARGET_ORIGIN = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
SEED_CUTOFF = TARGET_ORIGIN - timedelta(days=1)
FROZEN_AT = TARGET_ORIGIN - timedelta(hours=12)
FIXTURE = Path(__file__).parents[2] / "fixtures" / "stratz-rosh.json"
CONFIG = TeamRatingConfig(
    initial_rating=1_500.0,
    scale=400.0,
    k_factor=16.0,
    inactivity_half_life_days=180.0,
    roster_carry_power=1.0,
    radiant_side_logit=0.0,
    config_version=TEAM_RATING_VERSION,
)


class FixtureTransport:
    def __init__(self) -> None:
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def fetch_legacy_lineup_batch(
        self,
        radiant_heroes: Any,
        dire_heroes: Any,
        *,
        statistics_cutoff: datetime,
    ) -> FetchedLegacyRoshBatch:
        requests = build_rosh_query_requests(
            (*radiant_heroes, *dire_heroes),
            int(statistics_cutoff.timestamp()),
        )
        return FetchedLegacyRoshBatch(
            request_bodies={
                operation: canonical_json_bytes(payload)
                for operation, payload in requests.items()
            },
            response_bodies={
                operation: canonical_json_bytes(payload)
                for operation, payload in self.fixture["responses"].items()
            },
            collected_at=statistics_cutoff + timedelta(seconds=1),
        )


def _store_seed(repository: ProspectiveTeamRatingRepository) -> None:
    seed = build_prospective_team_rating_seed(
        config=CONFIG,
        source_results=(),
        seed_as_of=SEED_CUTOFF,
        seed_training_cutoff=SEED_CUTOFF,
        frozen_at=FROZEN_AT,
    )
    assert repository.store_seed(seed)


def test_operational_seed_freezer_is_idempotent(postgres_engine) -> None:
    session = PostgresSession(postgres_engine)
    try:
        first = freeze_seed(
            session,
            seed_cutoff=SEED_CUTOFF,
            frozen_at=FROZEN_AT,
        )
        second = freeze_seed(
            session,
            seed_cutoff=SEED_CUTOFF,
            frozen_at=FROZEN_AT,
        )
        stored = ProspectiveTeamRatingRepository(session).load_seed(TARGET_ORIGIN)
    finally:
        session.close()

    assert first["status"] == "stored"
    assert first["configuration_hash"] == (
        "b527319ab1035d6cae6550820cd0854b467f845537d033909b4f2e45e706c19a"
    )
    assert second["status"] == "unchanged"
    assert stored is not None
    assert stored.seed_hash == first["seed_hash"]


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


def _insert_vision_authority(
    session: PostgresSession,
    evidence_root: Path,
    *,
    match_id: str,
    map_number: int,
    slots: list[DraftSlotInput],
    captured_at: datetime,
) -> None:
    radiant = [slot.hero_id for slot in slots if slot.side == "radiant"]
    dire = [slot.hero_id for slot in slots if slot.side == "dire"]
    receipt = publish_vision_frame_bytes(
        evidence_root,
        f"{match_id}:{map_number}".encode("ascii"),
    )
    with session.transaction():
        register_vision_frame_artifact(
            session,
            receipt,
            registered_at=captured_at,
        )
        session.execute(
            """INSERT INTO vision_draft_anchors
               (raybet_match_id, map_number, draft_hash,
                radiant_hero_ids, dire_hero_ids, radiant_team_side,
                team_side_anchored_at, team_side_source_frame_ref,
                anchored_at, source_frame_ref, status, conflict_at)
               VALUES (?, ?, ?, ?, ?, 'team_one', ?, ?, ?, ?, 'anchored', NULL)""",
            (
                match_id,
                map_number,
                f"{map_number:064x}",
                json.dumps(radiant, separators=(",", ":")),
                json.dumps(dire, separators=(",", ":")),
                captured_at.isoformat(),
                receipt.frame_ref,
                captured_at.isoformat(),
                receipt.frame_ref,
            ),
        )
        session.execute(
            """INSERT INTO vision_observations
               (raybet_match_id, map_number, captured_at, game_clock_seconds,
                is_paused, radiant_hero_ids, dire_hero_ids, radiant_team_side,
                clock_confidence, draft_confidence, source_frame_ref,
                source_frame_sha256, source_frame_bytes, screen_state, confirmed)
               VALUES (?, ?, ?, NULL, NULL, ?, ?, 'team_one', 0.0, 0.97,
                       ?, ?, ?, 'draft', 1)""",
            (
                match_id,
                map_number,
                captured_at.isoformat(),
                json.dumps(radiant, separators=(",", ":")),
                json.dumps(dire, separators=(",", ":")),
                receipt.frame_ref,
                receipt.content_sha256,
                receipt.byte_length,
            ),
        )


def _insert_current_winner_market(
    session: PostgresSession,
    *,
    match_id: str,
    map_number: int,
    observed_at: datetime,
) -> None:
    identity = f"{match_id}:{map_number}"
    artifact_hash = f"{map_number:063x}a"
    response_hash = f"{map_number:063x}b"
    normalized_hash = f"{map_number:063x}c"
    observation_key = f"winner-{match_id}-{map_number}"
    with session.transaction():
        session.execute(
            """INSERT INTO odds_raw_artifacts
               (artifact_hash, source, storage_path, uncompressed_bytes,
                compressed_bytes, schema_fingerprint)
               VALUES (?, 'raybet', ?, 100, 50, ?)""",
            (artifact_hash, f"raw/{identity}.json.gz", "f" * 64),
        )
        session.execute(
            """INSERT INTO odds_response_states
               (response_state_hash, raybet_match_id, normalized_state_hash,
                normalized_state_hash_version, outcome_count)
               VALUES (?, ?, ?, 2, 2)""",
            (response_hash, match_id, normalized_hash),
        )
        session.executemany(
            """INSERT INTO odds_response_state_outcomes
               (response_state_hash, odds_id, odds_group_id, price, status,
                market_type, period, side, outcome_key, supported)
               VALUES (?, ?, ?, ?, 'open', 'winner', ?, ?, ?, 1)""",
            [
                (
                    response_hash,
                    f"{identity}:team-one",
                    f"{identity}:winner",
                    2.20,
                    f"map_{map_number}",
                    "team_one",
                    "team_one",
                ),
                (
                    response_hash,
                    f"{identity}:team-two",
                    f"{identity}:winner",
                    1.70,
                    f"map_{map_number}",
                    "team_two",
                    "team_two",
                ),
            ],
        )
        session.execute(
            """INSERT INTO odds_transport_observations
               (observation_key, source, raybet_match_id, observed_at,
                normalized_state_hash, normalized_state_hash_version,
                response_state_hash, response_artifact_hash, timing_status,
                processing_status, normalized_change_count)
               VALUES (?, 'direct', ?, ?, ?, 2, ?, ?, 'on_time', 'processed', 2)""",
            (
                observation_key,
                match_id,
                observed_at.isoformat(),
                normalized_hash,
                response_hash,
                artifact_hash,
            ),
        )


class FailingTransport:
    def fetch_legacy_lineup_batch(self, *_args: object, **_kwargs: object) -> None:
        raise StratzRoshError("STRATZ unavailable")


def test_locked_live_draft_paired_and_p0_only_are_immutable(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    session = PostgresSession(postgres_engine)
    lock_time = TARGET_ORIGIN - timedelta(minutes=30)
    try:
        _store_seed(ProspectiveTeamRatingRepository(session))
        with session.transaction():
            session.executemany(
                "INSERT INTO teams (team_id, name, tag) VALUES (?, ?, ?)",
                [(10, "Radiant", "RAD"), (20, "Dire", "DIRE")],
            )
            session.execute(
                """INSERT INTO raybet_matches
                   (raybet_match_id, team_one, team_two, status, raw_json, updated_at)
                   VALUES (?, 'Radiant', 'Dire', ?, ?, ?)""",
                (
                    "raybet-live-bridge",
                    "2",
                    json.dumps(
                        {
                            "team": [
                                {"team_id": 10, "team_name": "Radiant", "pos": 1},
                                {"team_id": 20, "team_name": "Dire", "pos": 2},
                            ]
                        }
                    ),
                    lock_time.isoformat(),
                ),
            )
        _insert_current_winner_market(
            session,
            match_id="raybet-live-bridge",
            map_number=1,
            observed_at=lock_time + timedelta(seconds=1),
        )
        first_slots = _slots()
        _insert_vision_authority(
            session,
            tmp_path / "evidence",
            match_id="raybet-live-bridge",
            map_number=1,
            slots=first_slots,
            captured_at=lock_time - timedelta(seconds=1),
        )
        first_mapping = save_live_draft_mapping(
            session,
            raybet_match_id="raybet-live-bridge",
            map_number=1,
            slots=first_slots,
            is_locked=True,
            actor="local-operator",
            evidence_source_url="https://example.test/evidence/bridge/map-1/v1",
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
            operator_identity=str(first_mapping["created_by"]),
            confirmation_text=LOCK_CONFIRMATION,
            confirmed_at=lock_time + timedelta(seconds=2),
        )
        repeated = generate_live_draft_prediction(
            repository,
            FailingTransport(),
            artifact_root=tmp_path / "artifacts",
            raybet_match_id="raybet-live-bridge",
            map_number=1,
            mapping_version=first_mapping["version"],
            operator_identity=str(first_mapping["created_by"]),
            confirmation_text=LOCK_CONFIRMATION,
            confirmed_at=lock_time + timedelta(seconds=3),
        )
        worker_result = record_due_checkpoints(
            session,
            now=lock_time + timedelta(seconds=2),
        )
        checkpoint = latest_map_checkpoints(
            session,
            "raybet-live-bridge",
            1,
        )[0]
        repeated_worker_result = record_due_checkpoints(
            session,
            now=lock_time + timedelta(seconds=3),
        )
        repeated_checkpoint = latest_map_checkpoints(
            session,
            "raybet-live-bridge",
            1,
        )[0]

        assert paired["status"] == "created"
        assert paired["prediction"]["record_status"] == "paired"
        assert paired["prediction"]["p1_probability"] is not None
        assert paired["prediction"]["causal_evidence"]["causal_status"] == "eligible"
        assert paired["prediction"]["causal_evidence"]["game_clock_seconds"] is None
        assert paired["prediction"]["causal_evidence"]["draft_state_marker"] == (
            "draft_complete"
        )
        assert session.execute(
            """SELECT COUNT(*) FROM trusted_vision_observation_authority
                WHERE raybet_match_id=? AND map_number=1""",
            ("raybet-live-bridge",),
        ).scalar_one() == 0
        assert checkpoint["phase"] == "pregame"
        assert checkpoint["decision"] in {"bet_team_a", "bet_team_b", "skip"}
        assert checkpoint["assumed_stake_units"] == 1.0
        assert worker_result == {"created": 1, "unchanged": 0}
        assert repeated["status"] == "unchanged"
        assert repeated["prediction"]["prediction_hash"] == paired["prediction"]["prediction_hash"]
        assert repeated_checkpoint["checkpoint_id"] == checkpoint["checkpoint_id"]
        assert repeated_worker_result == {"created": 0, "unchanged": 1}
        assert session.execute(
            "SELECT COUNT(*) FROM map_decision_checkpoints"
        ).scalar_one() == 1

        wrong_mapping = save_live_draft_mapping(
            session,
            raybet_match_id="raybet-live-bridge",
            map_number=1,
            slots=_slots(reverse=True),
            is_locked=True,
            actor="local-operator",
            evidence_source_url="https://example.test/evidence/bridge/map-1/v2",
            created_at=lock_time + timedelta(seconds=30),
        )
        with pytest.raises(ValueError, match="vision_draft_anchor_mapping_mismatch"):
            generate_live_draft_prediction(
                repository,
                FailingTransport(),
                artifact_root=tmp_path / "artifacts",
                raybet_match_id="raybet-live-bridge",
                map_number=1,
                mapping_version=wrong_mapping["version"],
                operator_identity=str(wrong_mapping["created_by"]),
                confirmation_text=LOCK_CONFIRMATION,
                confirmed_at=lock_time + timedelta(seconds=31),
            )

        session.execute(
            "UPDATE raybet_matches SET status=? WHERE raybet_match_id=?",
            ("unknown", "raybet-live-bridge"),
        )
        second_slots = _slots(reverse=True)
        _insert_vision_authority(
            session,
            tmp_path / "evidence",
            match_id="raybet-live-bridge",
            map_number=2,
            slots=second_slots,
            captured_at=lock_time + timedelta(minutes=1) - timedelta(seconds=1),
        )
        second_mapping = save_live_draft_mapping(
            session,
            raybet_match_id="raybet-live-bridge",
            map_number=2,
            slots=second_slots,
            is_locked=True,
            actor="local-operator",
            evidence_source_url="https://example.test/evidence/bridge/map-2/v1",
            created_at=lock_time + timedelta(minutes=1),
        )
        p0_only = generate_live_draft_prediction(
            repository,
            FailingTransport(),
            artifact_root=tmp_path / "artifacts",
            raybet_match_id="raybet-live-bridge",
            map_number=2,
            mapping_version=second_mapping["version"],
            operator_identity=str(second_mapping["created_by"]),
            confirmation_text=LOCK_CONFIRMATION,
            confirmed_at=lock_time + timedelta(minutes=1, seconds=2),
        )
        assert p0_only["prediction"]["record_status"] == "p0_only"
        assert p0_only["prediction"]["missing_reason"] == "prospective_rosh_evidence_unavailable"
        assert p0_only["prediction"]["causal_evidence"]["causal_status"] == "unverified"
        assert p0_only["prediction"]["identity"]["map_number"] == 2
        assert paired["prediction"]["identity"]["map_number"] == 1
        p0_only_checkpoint = record_pregame_checkpoint(
            session,
            mapping=second_mapping,
            prediction=p0_only["prediction"],
            decided_at=lock_time + timedelta(minutes=1, seconds=2),
        )
        assert p0_only_checkpoint["decision"] == "skip"
        assert p0_only_checkpoint["model_probability_team_one"] is None
        assert p0_only_checkpoint["selected_edge"] is None

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
            evidence_source_url="https://example.test/evidence/bridge/map-1/v3",
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
                operator_identity=str(third_mapping["created_by"]),
                confirmation_text=LOCK_CONFIRMATION,
                confirmed_at=lock_time + timedelta(minutes=2, seconds=2),
            )
        assert session.execute(
            "SELECT COUNT(*) FROM live_draft_prospective_predictions"
        ).scalar_one() == 2

        with pytest.raises(DBAPIError, match="append-only"):
            with session.transaction():
                session.execute(
                    "UPDATE live_draft_prospective_predictions SET support=support+1"
                )
        with pytest.raises(DBAPIError, match="append-only"):
            with session.transaction():
                session.execute(
                    "UPDATE map_decision_checkpoints SET reason=reason"
                )
        with pytest.raises(DBAPIError, match="append-only"):
            with session.transaction():
                session.execute("DELETE FROM map_decision_checkpoints")
        assert session.execute("SELECT COUNT(*) FROM shadow_orders").scalar_one() == 0
        assert session.execute("SELECT version_num FROM alembic_version").scalar_one() == (
            "20260807_0035"
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
