from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from event_intelligence.prospective_rosh_collector import (
    FROZEN_CANDIDATE_HASH,
    FROZEN_FORMULA_VERSION,
    FROZEN_PROFILE_ID,
    ProspectiveRoshLineup,
    collect_match,
    load_operational_candidate,
)
from event_intelligence.prospective_rosh_shadow import TeamRatingAuthority
from event_intelligence.prospective_team_rating import ProspectiveTarget
from event_intelligence.raw_archive import canonical_json_bytes
from event_intelligence.team_rating import TeamRatingTarget
from live_betting.rosh_parity import ExactByteArtifactStore
from live_betting.stratz_rosh_client import FetchedLegacyRoshBatch, StratzRoshError
from prematch.stratz_rosh import build_rosh_query_requests


UTC = timezone.utc
FIXTURE = Path(__file__).parent / "fixtures" / "stratz-rosh.json"


def _digest(value: int) -> str:
    return f"{value:064x}"


def _target(cutoff: datetime, match_id: int = 9000000001) -> ProspectiveTarget:
    return ProspectiveTarget(
        target=TeamRatingTarget(
            match_id=match_id,
            series_id=1200001,
            event_id="formal-event",
            started_at=cutoff,
            radiant_team_id=10,
            dire_team_id=20,
            radiant_roster=(),
            dire_roster=(),
        ),
        prediction_cutoff=cutoff,
    )


def _team(cutoff: datetime) -> TeamRatingAuthority:
    return TeamRatingAuthority(
        prediction_id=7,
        run_id=_digest(1),
        prediction_cutoff=cutoff,
        probability=0.55,
        rating_version="team-rating-elo-v1",
        artifact_version="prospective-team-rating-artifact-v1",
        artifact_hash=_digest(2),
        input_hash=_digest(3),
        training_input_hash=_digest(4),
    )


class FakeTeamRatingRepository:
    def __init__(self, cutoff: datetime, *, has_result: bool = False) -> None:
        self.target = _target(cutoff)
        self.authority = _team(cutoff)
        self.has_result = has_result

    def load_target(self, _match_id: int) -> tuple[ProspectiveTarget, bool]:
        return self.target, self.has_result

    def load_rosh_team_rating_authority(self, _match_id: int) -> TeamRatingAuthority:
        return self.authority

    def resolve_rosh_team_rating_authority(
        self,
        _match_id: int,
        *,
        observed_at: datetime,
    ) -> TeamRatingAuthority:
        assert observed_at < self.target.prediction_cutoff
        return self.authority


class FakeRepository:
    def __init__(
        self,
        cutoff: datetime,
        lineup: ProspectiveRoshLineup | None,
        lineup_reason: str | None = None,
    ) -> None:
        self.team_rating = FakeTeamRatingRepository(cutoff)
        self.lineup = lineup
        self.lineup_reason = lineup_reason
        self.predictions: list[Any] = []
        self.attempts: list[dict[str, Any]] = []

    def existing_prediction_hash(self, _candidate_hash: str, _match_id: int) -> None:
        return None

    def load_lineup(self, *_args: Any, **_kwargs: Any):
        return self.lineup, self.lineup_reason

    def count_network_attempts(self, _candidate_hash: str, _match_id: int) -> int:
        return 0

    def store_prediction_verified(self, record: Any) -> tuple[bool, bool]:
        self.predictions.append(record)
        return True, True

    def record_attempt(self, _candidate: Any, **kwargs: Any) -> str:
        self.attempts.append(kwargs)
        return _digest(len(self.attempts))


class FixtureTransport:
    def __init__(self, collected_at: datetime) -> None:
        self.collected_at = collected_at
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.calls = 0

    def fetch_legacy_lineup_batch(
        self,
        radiant_heroes: Any,
        dire_heroes: Any,
        *,
        statistics_cutoff: datetime,
    ) -> FetchedLegacyRoshBatch:
        self.calls += 1
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
            collected_at=self.collected_at,
        )


def _lineup() -> ProspectiveRoshLineup:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return ProspectiveRoshLineup(
        match_id=9000000001,
        series_id=1200001,
        radiant_heroes=tuple(fixture["radiant_heroes"]),
        dire_heroes=tuple(fixture["dire_heroes"]),
    )


def test_operational_identity_is_exact_and_not_official_v2() -> None:
    candidate = load_operational_candidate()

    assert candidate.artifact_hash == FROZEN_CANDIDATE_HASH
    assert candidate.prospective_profile_id == FROZEN_PROFILE_ID
    assert candidate.retrospective_formula_version == FROZEN_FORMULA_VERSION


def test_valid_p0_and_lineup_archive_exact_bytes_and_store_paired(tmp_path: Path) -> None:
    cutoff = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    observed = cutoff - timedelta(minutes=10)
    repository = FakeRepository(cutoff, _lineup())
    transport = FixtureTransport(observed + timedelta(seconds=5))

    result = collect_match(
        repository,  # type: ignore[arg-type]
        transport,
        ExactByteArtifactStore(tmp_path / "artifacts"),
        load_operational_candidate(),
        9000000001,
        now=observed,
    )

    assert result.status == "stored"
    assert result.record_status == "paired"
    assert result.exact_replay is True
    assert result.idempotency == "unchanged"
    assert transport.calls == 1
    assert repository.predictions[0].p1_probability is not None
    assert [row["status"] for row in repository.attempts] == [
        "paired_stored",
        "idempotency_unchanged",
    ]
    assert len(tuple((tmp_path / "artifacts").rglob("*.json.gz"))) == 6


def test_incomplete_positions_wait_then_finalize_p0_only(tmp_path: Path) -> None:
    cutoff = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    candidate = load_operational_candidate()
    early = FakeRepository(cutoff, None, "expected_positions_incomplete")
    transport = SimpleNamespace(fetch_legacy_lineup_batch=lambda *_a, **_k: None)

    retry = collect_match(
        early,  # type: ignore[arg-type]
        transport,
        ExactByteArtifactStore(tmp_path / "early"),
        candidate,
        9000000001,
        now=cutoff - timedelta(minutes=10),
    )
    assert retry.status == "retry_scheduled"
    assert retry.missing_reason == "expected_positions_incomplete"

    late = FakeRepository(cutoff, None, "expected_positions_incomplete")
    p0_only = collect_match(
        late,  # type: ignore[arg-type]
        transport,
        ExactByteArtifactStore(tmp_path / "late"),
        candidate,
        9000000001,
        now=cutoff - timedelta(minutes=1),
    )
    assert p0_only.record_status == "p0_only"
    assert p0_only.missing_reason == "expected_positions_incomplete"
    assert late.predictions[0].p1_probability is None


def test_retryable_transport_failure_is_bounded_before_cutoff(tmp_path: Path) -> None:
    cutoff = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    repository = FakeRepository(cutoff, _lineup())

    class FailedTransport:
        def fetch_legacy_lineup_batch(self, *_args: Any, **_kwargs: Any) -> None:
            raise StratzRoshError(
                "temporary",
                retryable=True,
                category="network_failure",
            )

    result = collect_match(
        repository,  # type: ignore[arg-type]
        FailedTransport(),
        ExactByteArtifactStore(tmp_path / "artifacts"),
        load_operational_candidate(),
        9000000001,
        now=cutoff - timedelta(minutes=10),
    )

    assert result.status == "retry_scheduled"
    assert result.missing_reason == "stratz_network_failure"
    assert repository.predictions == []
    assert repository.attempts[0]["retry_at"] < cutoff


def test_cutoff_elapsed_never_calls_transport_or_writes_prediction(tmp_path: Path) -> None:
    cutoff = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    repository = FakeRepository(cutoff, _lineup())
    transport = FixtureTransport(cutoff + timedelta(seconds=1))

    result = collect_match(
        repository,  # type: ignore[arg-type]
        transport,
        ExactByteArtifactStore(tmp_path / "artifacts"),
        load_operational_candidate(),
        9000000001,
        now=cutoff,
    )

    assert result.status == "terminal_failure"
    assert result.missing_reason == "cutoff_elapsed"
    assert transport.calls == 0
    assert repository.predictions == []


def test_batch_completing_after_cutoff_is_archived_but_never_backfilled(
    tmp_path: Path,
) -> None:
    cutoff = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    repository = FakeRepository(cutoff, _lineup())
    transport = FixtureTransport(cutoff + timedelta(seconds=1))

    result = collect_match(
        repository,  # type: ignore[arg-type]
        transport,
        ExactByteArtifactStore(tmp_path / "artifacts"),
        load_operational_candidate(),
        9000000001,
        now=cutoff - timedelta(minutes=10),
    )

    assert result.status == "terminal_failure"
    assert result.missing_reason == "request_completed_after_cutoff"
    assert repository.predictions == []
    assert repository.attempts[0]["request_artifacts"] is not None
    assert repository.attempts[0]["response_artifacts"] is not None
    assert len(tuple((tmp_path / "artifacts").rglob("*.json.gz"))) == 6
