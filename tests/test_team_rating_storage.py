from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from database.session import DatabaseResult, DatabaseRow
from event_intelligence.draft_features import AvailabilityMode
from event_intelligence.team_rating import RatingMapInput
from event_intelligence.team_rating_backtest import (
    LoadedTeamRatingMap,
    TeamRatingCorpus,
    TeamRatingParameters,
    TeamRatingSourceAuthority,
    build_team_rating_walk_forward_runs,
)
from event_intelligence.team_rating_storage import (
    build_team_rating_storage_records,
    persist_team_rating_runs,
)


UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
CREATED_AT = datetime(2026, 8, 5, tzinfo=UTC)
PARAMETERS = TeamRatingParameters(400.0, 16.0, 180.0, 1.0)


def _map(match_id: int, *, radiant_win: bool) -> RatingMapInput:
    started_at = START + timedelta(hours=2 * match_id)
    completed_at = started_at + timedelta(minutes=40)
    return RatingMapInput(
        match_id=match_id,
        series_id=100,
        event_id="event-a",
        started_at=started_at,
        completed_at=completed_at,
        result_usable_at=completed_at + timedelta(minutes=1),
        radiant_team_id=10,
        dire_team_id=20,
        radiant_roster=(1, 2, 3, 4, 5),
        dire_roster=(6, 7, 8, 9, 10),
        radiant_win=radiant_win,
    )


def _runs():
    rows = (_map(1, radiant_win=True), _map(2, radiant_win=False))
    corpus = TeamRatingCorpus(
        availability_mode=AvailabilityMode.RECONSTRUCTED.value,
        formal_maps=len(rows),
        maps=tuple(
            LoadedTeamRatingMap(
                row,
                row.started_at,
                "reconstructed_map_start",
                _source_authority(row),
            )
            for row in rows
        ),
    )
    return build_team_rating_walk_forward_runs(
        corpus,
        candidates=(PARAMETERS,),
    )


def _source_authority(row: RatingMapInput) -> TeamRatingSourceAuthority:
    content_hash = hashlib.sha256(f"source:{row.match_id}".encode()).hexdigest()
    return TeamRatingSourceAuthority(
        match_id=row.match_id,
        artifact_id=f"opendota:{content_hash}",
        content_hash=content_hash,
        artifact_usable_at=row.result_usable_at,
        observation_usable_at=row.result_usable_at,
    )


def _unchecked_run(run: object, **changes: object) -> SimpleNamespace:
    return SimpleNamespace(**({**vars(run), **changes}))


def _result(columns: Sequence[str], values: tuple[object, ...] | None) -> DatabaseResult:
    rows = () if values is None else (DatabaseRow(columns, values),)
    return DatabaseResult(rows, len(rows), tuple(columns))


class _MemoryConnection:
    def __init__(self) -> None:
        self.runs: dict[str, tuple[object, ...]] = {}
        self.predictions: dict[tuple[str, int], tuple[object, ...]] = {}
        self.snapshots: dict[tuple[str, str, int], tuple[object, ...]] = {}

    @contextmanager
    def transaction(self) -> Iterator[None]:
        before = deepcopy((self.runs, self.predictions, self.snapshots))
        try:
            yield
        except BaseException:
            self.runs, self.predictions, self.snapshots = before
            raise

    def execute(
        self,
        statement: str,
        parameters: Sequence[object] = (),
    ) -> DatabaseResult:
        sql = " ".join(statement.split())
        values = tuple(parameters)
        if sql.startswith("SELECT rating_version"):
            return _result(
                (
                    "rating_version",
                    "artifact_version",
                    "availability_mode",
                    "training_cutoff",
                    "configuration_json",
                    "training_input_hash",
                    "metrics_json",
                    "status",
                ),
                self.runs.get(str(values[0])),
            )
        if sql.startswith("INSERT INTO team_rating_runs"):
            key = str(values[0])
            if key in self.runs:
                return _result(("run_id",), None)
            self.runs[str(values[0])] = values[1:-1]
            return _result(("run_id",), (key,))
        if sql.startswith("SELECT match_id"):
            return _result(
                (
                    "match_id",
                    "prediction_cutoff",
                    "cutoff_source",
                    "radiant_team_id",
                    "dire_team_id",
                    "radiant_rating",
                    "dire_rating",
                    "rating_diff",
                    "raw_probability",
                    "radiant_roster_continuity",
                    "dire_roster_continuity",
                    "support",
                    "input_hash",
                    "eventual_radiant_win",
                    "status",
                ),
                self.predictions.get((str(values[0]), int(values[1]))),
            )
        if sql.startswith("INSERT INTO team_rating_predictions"):
            key = (str(values[0]), int(values[1]))
            if key in self.predictions:
                return _result(("prediction_id",), None)
            self.predictions[key] = values[1:-1]
            return _result(("prediction_id",), (len(self.predictions),))
        if sql.startswith("SELECT snapshot_key"):
            return _result(
                (
                    "snapshot_key",
                    "rating",
                    "maps_seen",
                    "roster_json",
                    "last_observed_at",
                    "state_hash",
                ),
                self.snapshots.get((str(values[0]), str(values[1]), int(values[2]))),
            )
        if sql.startswith("INSERT INTO team_rating_state_snapshots"):
            key = (str(values[1]), str(values[2]), int(values[3]))
            if key in self.snapshots:
                return _result(("snapshot_key",), None)
            self.snapshots[key] = (
                values[0],
                values[4],
                values[5],
                values[6],
                values[7],
                values[8],
            )
            return _result(("snapshot_key",), (str(values[0]),))
        raise AssertionError(sql)


def test_storage_records_are_canonical_and_bind_artifact_identity() -> None:
    run = _runs()[-1]

    run_record, prediction, snapshots = build_team_rating_storage_records(run)
    repeated = build_team_rating_storage_records(run)

    assert repeated == (run_record, prediction, snapshots)
    assert run_record.run_id == run.run_id
    assert run_record.training_input_hash == run.combined_training_input_hash
    assert run.artifact.artifact_hash in run_record.configuration_json
    assert run.authority_fingerprint in run_record.configuration_json
    assert run.target_source_authority.content_hash in run_record.configuration_json
    assert prediction.input_hash == run.artifact.prediction.input_hash
    assert prediction.radiant_roster_continuity == 1.0
    assert prediction.dire_roster_continuity == 1.0
    assert prediction.status == "settled"
    assert {row.team_id for row in snapshots} == {10, 20}
    assert all(len(row.snapshot_key) == len(row.state_hash) == 64 for row in snapshots)


def test_insufficient_run_persists_an_unsettled_null_prediction() -> None:
    run = _runs()[0]

    run_record, prediction, _snapshots = build_team_rating_storage_records(run)

    assert run_record.status == "insufficient_evidence"
    assert prediction.status == "insufficient_evidence"
    assert prediction.raw_probability is None
    assert prediction.eventual_radiant_win is None


def test_default_persistence_is_idempotent_and_does_not_write_checkpoints() -> None:
    connection = _MemoryConnection()
    runs = _runs()

    first = persist_team_rating_runs(
        connection,  # type: ignore[arg-type]
        runs,
        created_at=CREATED_AT,
    )
    second = persist_team_rating_runs(
        connection,  # type: ignore[arg-type]
        tuple(reversed(runs)),
        created_at=CREATED_AT + timedelta(days=1),
    )

    assert (first.inserted_runs, first.inserted_predictions) == (2, 2)
    assert (second.unchanged_runs, second.unchanged_predictions) == (2, 2)
    assert first.inserted_snapshots == second.unchanged_snapshots == 0
    assert connection.snapshots == {}


def test_checkpoint_persistence_is_explicit_and_idempotent() -> None:
    connection = _MemoryConnection()
    runs = _runs()
    latest = runs[-1]

    first = persist_team_rating_runs(
        connection,  # type: ignore[arg-type]
        runs,
        checkpoint_run_ids=(latest.run_id,),
        created_at=CREATED_AT,
    )
    second = persist_team_rating_runs(
        connection,  # type: ignore[arg-type]
        runs,
        checkpoint_run_ids=(latest.run_id,),
        created_at=CREATED_AT + timedelta(hours=1),
    )

    assert first.inserted_snapshots == 2
    assert second.unchanged_snapshots == 2
    assert {key[0] for key in connection.snapshots} == {latest.run_id}


def test_identity_conflict_rolls_back_the_entire_batch() -> None:
    connection = _MemoryConnection()
    first, second = _runs()
    persist_team_rating_runs(
        connection,  # type: ignore[arg-type]
        (second,),
        created_at=CREATED_AT,
    )
    conflicting = replace(second, status="failed")

    with pytest.raises(ValueError, match="immutable Team Rating run conflict"):
        persist_team_rating_runs(
            connection,  # type: ignore[arg-type]
            (first, conflicting),
            created_at=CREATED_AT,
        )

    assert first.run_id not in connection.runs
    assert set(connection.runs) == {second.run_id}
    assert set(run_id for run_id, _match_id in connection.predictions) == {
        second.run_id
    }


def test_batch_duplicates_normalize_and_dry_run_conflicts_fail_preflight() -> None:
    run = _runs()[-1]
    connection = _MemoryConnection()

    counts = persist_team_rating_runs(
        connection,  # type: ignore[arg-type]
        (run, run),
        created_at=CREATED_AT,
    )

    assert (counts.inserted_runs, counts.unchanged_runs) == (1, 0)
    assert (counts.inserted_predictions, counts.unchanged_predictions) == (1, 0)

    empty = _MemoryConnection()
    conflicting = _unchecked_run(
        run,
        eventual_radiant_win=not run.eventual_radiant_win,
    )
    with pytest.raises(
        ValueError,
        match="immutable Team Rating prediction conflict",
    ):
        persist_team_rating_runs(
            empty,  # type: ignore[arg-type]
            (run, conflicting),  # type: ignore[arg-type]
            dry_run=True,
            created_at=CREATED_AT,
        )
    assert empty.runs == empty.predictions == empty.snapshots == {}


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"run_id": "f" * 64}, "run_id does not recompute"),
        (
            {"authority_fingerprint": "f" * 64},
            "authority fingerprint does not recompute",
        ),
        (
            {"combined_training_input_hash": "f" * 64},
            "training input hash does not recompute",
        ),
        (
            {"availability_mode": AvailabilityMode.PROSPECTIVE.value},
            "requires reconstructed map-start authority",
        ),
        (
            {"cutoff_source": "prospective_draft_complete"},
            "requires reconstructed map-start authority",
        ),
    ),
)
def test_storage_boundary_recomputes_identity_and_mode_binding(
    changes: dict[str, object],
    message: str,
) -> None:
    run = _runs()[-1]

    with pytest.raises(ValueError, match=message):
        build_team_rating_storage_records(  # type: ignore[arg-type]
            _unchecked_run(run, **changes)
        )


def test_storage_boundary_rejects_authority_manifest_mismatch() -> None:
    run = _runs()[-1]
    mismatched = _unchecked_run(
        run,
        ordered_training_sources=(run.target_source_authority,),
    )

    with pytest.raises(ValueError, match="manifest does not match"):
        build_team_rating_storage_records(mismatched)  # type: ignore[arg-type]


def test_invalid_checkpoint_time_and_artifact_fail_before_writes() -> None:
    connection = _MemoryConnection()
    runs = _runs()

    with pytest.raises(ValueError, match="not present"):
        persist_team_rating_runs(
            connection,  # type: ignore[arg-type]
            runs,
            checkpoint_run_ids=("f" * 64,),
            created_at=CREATED_AT,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        persist_team_rating_runs(
            connection,  # type: ignore[arg-type]
            runs,
            created_at=CREATED_AT.replace(tzinfo=None),
        )

    run = runs[-1]
    tampered_prediction = replace(
        run.artifact.prediction,
        input_hash="f" * 64,
    )
    tampered = replace(
        run,
        artifact=replace(run.artifact, prediction=tampered_prediction),
    )
    with pytest.raises(ValueError, match="replay|hash"):
        persist_team_rating_runs(
            connection,  # type: ignore[arg-type]
            (tampered,),
            created_at=CREATED_AT,
        )
    assert connection.runs == {}
