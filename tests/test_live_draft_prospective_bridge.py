from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import event_intelligence.live_draft_prospective_bridge as bridge
from event_intelligence.live_draft_prospective_bridge import (
    LOCK_CONFIRMATION,
    LiveDraftProspectiveBridgeRepository,
    _causal_status,
    canonical_mapping_hash,
    generate_live_draft_prediction,
)
from event_intelligence.prospective_team_rating import (
    build_prospective_team_rating_seed,
)
from event_intelligence.prospective_rosh_candidate import (
    load_frozen_prospective_rosh_candidate,
)
from event_intelligence.team_rating import TeamRatingConfig


UTC = timezone.utc
LOCKED_AT = datetime(2026, 8, 8, 8, 0, tzinfo=UTC)


def _mapping(*, locked: bool = True, hero_offset: int = 0) -> dict[str, object]:
    return {
        "raybet_match_id": "raybet-live-1",
        "map_number": 2,
        "version": 3,
        "is_locked": locked,
        "created_by": "local-operator",
        "created_at": LOCKED_AT.isoformat(),
        "slots": [
            {
                "team_id": 10 if index < 5 else 20,
                "side": "radiant" if index < 5 else "dire",
                "position": index % 5 + 1,
                "hero_id": 100 + hero_offset + index,
                "player_id": None,
            }
            for index in range(10)
        ],
    }


def test_locked_mapping_hash_requires_exact_unique_ten_slot_authority() -> None:
    mapping = _mapping()

    assert canonical_mapping_hash(mapping) == canonical_mapping_hash(mapping)
    assert len(canonical_mapping_hash(mapping)) == 64
    with pytest.raises(ValueError, match="locked"):
        canonical_mapping_hash(_mapping(locked=False))

    duplicate = _mapping()
    duplicate["slots"][1]["hero_id"] = duplicate["slots"][0]["hero_id"]  # type: ignore[index]
    with pytest.raises(ValueError, match="unique"):
        canonical_mapping_hash(duplicate)

    missing_position = _mapping()
    missing_position["slots"][4]["position"] = 4  # type: ignore[index]
    with pytest.raises(ValueError, match="unique"):
        canonical_mapping_hash(missing_position)


class _TeamRepository:
    def __init__(self) -> None:
        config = TeamRatingConfig(
            initial_rating=1500.0,
            scale=200.0,
            k_factor=24.0,
            inactivity_half_life_days=180.0,
            roster_carry_power=2.0,
            radiant_side_logit=0.041210268646663106,
            config_version="team-rating-elo-v1",
        )
        self.seed = build_prospective_team_rating_seed(
            config=config,
            source_results=(),
            seed_as_of=LOCKED_AT - timedelta(days=2),
            seed_training_cutoff=LOCKED_AT - timedelta(days=2),
            frozen_at=LOCKED_AT - timedelta(days=1),
        )
        self.base = SimpleNamespace(
            authority_hash=None,
            as_of=self.seed.seed_as_of,
            state_hash=self.seed.state_hash,
            states=self.seed.states,
        )

    def load_seed(self, _cutoff: datetime) -> object:
        return self.seed

    def load_base_state(self, _seed: object, _cutoff: datetime) -> object:
        return self.base

    def load_results(self, **kwargs: object) -> tuple[()]:
        assert kwargs["target_match_id"] is None
        assert kwargs["allow_seed_observation"] is True
        return ()


def test_live_p0_uses_team_identity_and_frozen_state_not_draft_or_live_state() -> None:
    repository = object.__new__(LiveDraftProspectiveBridgeRepository)
    repository.team_rating = _TeamRepository()

    first = repository.build_p0(_mapping(), observed_at=LOCKED_AT + timedelta(seconds=1))
    changed_lineup = repository.build_p0(
        _mapping(hero_offset=1000),
        observed_at=LOCKED_AT + timedelta(seconds=1),
    )

    assert first is not None and changed_lineup is not None
    assert first == changed_lineup
    assert first.probability > 0.5
    artifact = first.to_payload()
    assert "hero" not in str(artifact).lower()
    assert "game_clock" not in str(artifact).lower()
    assert "odds" not in str(artifact).lower()


def test_causal_status_allows_positive_game_clock_for_active_match() -> None:
    assert _causal_status(
        live_state_input_used=False,
        game_clock_seconds=1,
        draft_state_marker="in_game",
        lifecycle_status="2",
    ) == ("eligible", None)
    assert _causal_status(
        live_state_input_used=False,
        game_clock_seconds=None,
        draft_state_marker=None,
        lifecycle_status=None,
    )[0] == "unverified"
    assert _causal_status(
        live_state_input_used=True,
        game_clock_seconds=600,
        draft_state_marker="draft_complete",
        lifecycle_status="running",
    )[0] == "unverified"
    with pytest.raises(ValueError, match="lifecycle_ended"):
        _causal_status(
            live_state_input_used=False,
            game_clock_seconds=600,
            draft_state_marker="in_game",
            lifecycle_status="completed",
        )


class _NoSeedRepository:
    def load_mapping(self, *_args: object) -> dict[str, object]:
        return _mapping()

    def load_prediction(self, *_args: object) -> None:
        return None

    def validate_prediction_target(self, *_args: object) -> str:
        return "2"

    def build_p0(self, *_args: object, **_kwargs: object) -> None:
        return None


def test_missing_prospective_seed_is_stable_blocker_without_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    def forbidden_client() -> None:
        raise AssertionError("STRATZ client must not be created without prospective P0")

    monkeypatch.setattr(bridge, "StratzRoshClient", forbidden_client)
    result = generate_live_draft_prediction(
        _NoSeedRepository(),  # type: ignore[arg-type]
        artifact_root=tmp_path,
        raybet_match_id="raybet-live-1",
        map_number=2,
        mapping_version=3,
        operator_identity="local-operator",
        confirmation_text=LOCK_CONFIRMATION,
        confirmed_at=LOCKED_AT + timedelta(seconds=1),
    )

    assert result == {
        "status": "blocked",
        "missing_reason": "prospective_team_rating_seed_unavailable",
        "prediction": None,
    }


class _ReadyRepository(_NoSeedRepository):
    connection = object()

    def build_p0(self, *_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(probability=0.6)


class _ProgrammingErrorTransport:
    def fetch_legacy_lineup_batch(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("programming error")


def test_programming_error_is_not_mislabeled_as_rosh_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    candidate_store = SimpleNamespace(store_candidate=lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bridge,
        "ProspectiveRoshShadowRepository",
        lambda _connection: candidate_store,
    )

    with pytest.raises(TypeError, match="programming error"):
        generate_live_draft_prediction(
            _ReadyRepository(),  # type: ignore[arg-type]
            _ProgrammingErrorTransport(),
            artifact_root=tmp_path,
            raybet_match_id="raybet-live-1",
            map_number=2,
            mapping_version=3,
            operator_identity="local-operator",
            confirmation_text=LOCK_CONFIRMATION,
            confirmed_at=LOCKED_AT + timedelta(seconds=1),
        )


def test_frozen_candidate_identity_and_positive_beta_are_unchanged() -> None:
    candidate = load_frozen_prospective_rosh_candidate()

    assert candidate.artifact_hash == (
        "84c4506f63b7c5b745b32373b0cb405383f837c60eae3231cc3d688a0b36e09d"
    )
    assert candidate.prospective_profile_id == "legacy-dematus-pure-rosh-prospective-v1"
    assert candidate.beta_rosh > 0


class _Result:
    def __init__(self, row: object) -> None:
        self.row = row

    def fetchone(self) -> object:
        return self.row


class _TargetValidationConnection:
    def __init__(
        self,
        *,
        result_exists: bool = False,
        settlement_exists: bool = False,
        lifecycle_status: str | None = None,
    ) -> None:
        self.result_exists = result_exists
        self.settlement_exists = settlement_exists
        self.lifecycle_status = lifecycle_status

    def execute(self, sql: str, _parameters: object = None) -> _Result:
        if "FROM map_results" in sql:
            return _Result((1,) if self.result_exists else None)
        if "FROM live_draft_prospective_settlements" in sql:
            return _Result((1,) if self.settlement_exists else None)
        if "FROM raybet_matches" in sql:
            return _Result(None if self.lifecycle_status is None else (self.lifecycle_status,))
        raise AssertionError(f"unexpected query: {sql}")


def test_prediction_target_rejects_only_authoritative_end_evidence() -> None:
    active = LiveDraftProspectiveBridgeRepository(
        _TargetValidationConnection(lifecycle_status="running")  # type: ignore[arg-type]
    )
    unknown = LiveDraftProspectiveBridgeRepository(
        _TargetValidationConnection()  # type: ignore[arg-type]
    )
    assert active.validate_prediction_target("match", 1) == "running"
    assert unknown.validate_prediction_target("match", 1) is None

    for connection, reason in (
        (_TargetValidationConnection(result_exists=True), "authoritative_result"),
        (_TargetValidationConnection(settlement_exists=True), "already_settled"),
        (_TargetValidationConnection(lifecycle_status="abandoned"), "lifecycle_ended"),
    ):
        repository = LiveDraftProspectiveBridgeRepository(connection)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match=reason):
            repository.validate_prediction_target("match", 1)


class _SettlementConnection:
    def __init__(
        self,
        *,
        prediction_created_at: str = "2026-08-08T08:30:00+00:00",
        result_usable_at: str = "2026-08-08T10:00:00+00:00",
        duration_seconds: int = 7200,
    ) -> None:
        self.statements: list[str] = []
        self.prediction_created_at = prediction_created_at
        self.result_usable_at = result_usable_at
        self.duration_seconds = duration_seconds

    def execute(self, sql: str, _parameters: object = None) -> _Result:
        self.statements.append(sql)
        if "JOIN map_results" in sql:
            return _Result(
                (
                    "raybet-live-1",
                    2,
                    10,
                    20,
                    "eligible",
                    None,
                    self.prediction_created_at,
                    7,
                    9_000_000_001,
                    "team_one",
                    self.result_usable_at,
                    "f" * 64,
                    10,
                    20,
                    int(datetime(2026, 8, 8, 8, 0, tzinfo=UTC).timestamp()),
                    self.duration_seconds,
                )
            )
        if "SELECT settlement_hash" in sql:
            return _Result(None)
        return _Result(("inserted",))

    @contextmanager
    def transaction(self):
        yield


def test_settlement_is_append_only_and_keeps_prediction_unchanged() -> None:
    connection = _SettlementConnection()
    repository = LiveDraftProspectiveBridgeRepository(connection)  # type: ignore[arg-type]

    settlement = repository.settle_prediction(
        "a" * 64,
        settled_at=datetime(2026, 8, 8, 10, 1, tzinfo=UTC),
    )

    assert settlement is not None
    assert settlement["winner_side"] == "radiant"
    assert settlement["post_settlement_causal_status"] == "eligible"
    assert any("INSERT INTO live_draft_prospective_settlements" in sql for sql in connection.statements)
    assert not any("UPDATE live_draft_prospective_predictions" in sql for sql in connection.statements)


@pytest.mark.parametrize(
    ("connection", "reason"),
    [
        (
            _SettlementConnection(
                prediction_created_at="2026-08-08T09:00:00+00:00",
                duration_seconds=3600,
            ),
            "prediction_not_before_authoritative_end",
        ),
        (
            _SettlementConnection(
                prediction_created_at="2026-08-08T09:30:00+00:00",
                result_usable_at="2026-08-08T09:00:00+00:00",
            ),
            "prediction_not_before_result_first_usable_at",
        ),
    ],
)
def test_settlement_audit_uses_end_and_result_availability_only(
    connection: _SettlementConnection,
    reason: str,
) -> None:
    settlement = LiveDraftProspectiveBridgeRepository(  # type: ignore[arg-type]
        connection
    ).settle_prediction(
        "a" * 64,
        settled_at=datetime(2026, 8, 8, 10, 1, tzinfo=UTC),
    )

    assert settlement is not None
    assert settlement["post_settlement_causal_status"] == "ineligible"
    assert settlement["post_settlement_causal_reason"] == reason
