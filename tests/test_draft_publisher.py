from __future__ import annotations

import hashlib
import gc
import json
import os
import sqlite3
import sys
import weakref
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import event_intelligence.backtest as backtest_module
import live_betting.draft_publisher as draft_publisher_module
import live_betting.profiles.draft_curve as draft_curve_profile
from event_intelligence.backtest import HORIZONS, draft_dependency_fingerprint
from event_intelligence.deployment import (
    FrozenDraftDeployment,
    build_frozen_draft_deployment,
    load_prospective_history,
)
from event_intelligence.draft_artifacts import (
    CalibrationSample,
    build_calibration_artifact,
    canonical_hash,
    canonical_json_bytes,
    model_artifact_from_payload,
)
from event_intelligence.draft_features import (
    DRAFT_FEATURE_ARTIFACT_VERSION,
    DraftMapEvidence,
    build_draft_feature_artifact,
)
from event_intelligence.draft_model import (
    FeatureSchema,
    fit_draft_model,
    predict_draft,
)
from event_intelligence.ingest_adapters import SQLiteIngestAdapter
from event_intelligence.raw_archive import RawArchive
from event_intelligence.registry import EventRegistry
from event_intelligence.roles import (
    PROSPECTIVE_ASSIGNMENT_VERSION,
    RECONSTRUCTED_ASSIGNMENT_VERSION,
)
from event_intelligence.storage import IntelligenceStorage
from live_betting.database_protocol import prepare_database
from live_betting.draft_authority import (
    authority_from_curve,
    draft_landmark_authority_matches,
)
from live_betting.draft_evidence import prospective_outcome_authority
from live_betting.draft_publisher import (
    DraftAnchor,
    ProspectiveHistorySnapshot,
    _curve_key,
    _deployment_identity,
    _existing_curve,
    _latest_patch,
    build_live_draft_target,
    build_prospective_calibration_deployment,
    draft_anchor_frames_are_authoritative,
    load_frozen_deployment,
    load_latest_frozen_deployment,
    load_pinned_frozen_deployment,
    persist_frozen_deployment,
    publisher_singleton_lock,
    publish_anchor_curve,
    publish_cycle,
    ready_draft_anchors,
)
from live_betting.profiles.draft_curve import (
    _verify_prospective_calibration_evidence,
    build_draft_curve,
)
from live_betting.raybet import parse_raybet_map_final
from live_betting.storage import LiveBettingStore
from shared.sqlite import connect
from tests.draft_authority_fixture import make_test_vision_observation


UTC = timezone.utc
CUTOFF = datetime(2026, 7, 17, 8, 0, tzinfo=UTC)


def _deployment(
    database: Path,
    *,
    min_samples: int = 20,
) -> FrozenDraftDeployment:
    connection = connect(database, row_factory=sqlite3.Row)
    try:
        _insert_event(connection)
        _insert_prospective_history(connection)
        return build_frozen_draft_deployment(
            connection,
            training_cutoff=CUTOFF,
            min_samples=min_samples,
        )
    finally:
        connection.close()


def _runtime_history(
    dependency_revision: int,
    dependency_fingerprint: str,
    maps: tuple[DraftMapEvidence, ...],
) -> ProspectiveHistorySnapshot:
    return draft_publisher_module._bind_runtime_history(
        ProspectiveHistorySnapshot(
            dependency_revision,
            dependency_fingerprint,
            maps,
        )
    )


def test_runtime_history_capability_is_identity_bound_and_weak() -> None:
    history = _runtime_history(1, "a" * 64, ())
    equal_but_unbound = ProspectiveHistorySnapshot(1, "a" * 64, ())
    history_id = id(history)
    reference = weakref.ref(history)

    assert draft_publisher_module._runtime_history_is_bound(history)
    assert not draft_publisher_module._runtime_history_is_bound(equal_but_unbound)

    del history
    gc.collect()
    assert reference() is None
    assert history_id not in draft_publisher_module._BOUND_RUNTIME_HISTORIES


def _forged_corpus_deployment(
    deployment: FrozenDraftDeployment,
    *,
    horizon_minutes: int,
) -> FrozenDraftDeployment:
    original = deployment.model(horizon_minutes)
    corpus = list(original.training_corpus)
    first = corpus[0]
    features = dict(first.features)
    feature_name = next(
        name for name in original.feature_names if features[name] is not None
    )
    features[feature_name] = float(features[feature_name]) + 0.125
    corpus[0] = replace(
        first,
        features=tuple((name, features[name]) for name in original.feature_names),
    )
    forged_model = fit_draft_model(
        tuple(row.to_training_row() for row in corpus),
        FeatureSchema.from_names(original.feature_names),
        original.training_cutoff,
        original.horizon_minutes,
        min_samples=original.min_samples,
        model_kind=original.model_kind,
        l2_regularization=original.l2_regularization,
    )
    assert forged_model.model_hash != original.model_hash
    original_calibration = deployment.calibration(horizon_minutes)
    forged_calibration = build_calibration_artifact(
        forged_model,
        evidence_mode=original_calibration.evidence_mode,
        source_ref=original_calibration.source_ref,
        fit_samples=original_calibration.fit_samples,
        evaluation_samples=original_calibration.evaluation_samples,
    )
    models = tuple(
        forged_model if row.horizon_minutes == horizon_minutes else row
        for row in deployment.models
    )
    calibrations = tuple(
        forged_calibration if row.horizon_minutes == horizon_minutes else row
        for row in deployment.calibrations
    )
    identity = _deployment_identity(
        training_cutoff=deployment.training_cutoff,
        dependency_fingerprint=deployment.dependency_fingerprint,
        dependency_revision=deployment.dependency_revision,
        models=models,
        calibrations=calibrations,
        evidence_mode=deployment.evidence_mode,
    )
    return FrozenDraftDeployment(
        deployment_key=canonical_hash(identity),
        training_cutoff=deployment.training_cutoff,
        dependency_fingerprint=deployment.dependency_fingerprint,
        dependency_revision=deployment.dependency_revision,
        models=models,
        calibrations=calibrations,
    )


def _legacy_audit_only_deployment(
    deployment: FrozenDraftDeployment,
    *,
    horizon_minutes: int = 10,
) -> FrozenDraftDeployment:
    payload = deployment.model(horizon_minutes).to_payload()
    payload.pop("artifact_version")
    payload.pop("trainer_runtime")
    payload.pop("training_corpus")
    unsigned = dict(payload)
    unsigned.pop("model_hash")
    payload["model_hash"] = canonical_hash(unsigned)
    legacy_model = model_artifact_from_payload(payload)
    original_calibration = deployment.calibration(horizon_minutes)
    legacy_calibration = build_calibration_artifact(
        legacy_model,
        evidence_mode=original_calibration.evidence_mode,
        source_ref=original_calibration.source_ref,
        fit_samples=original_calibration.fit_samples,
        evaluation_samples=original_calibration.evaluation_samples,
    )
    models = tuple(
        legacy_model if row.horizon_minutes == horizon_minutes else row
        for row in deployment.models
    )
    calibrations = tuple(
        legacy_calibration if row.horizon_minutes == horizon_minutes else row
        for row in deployment.calibrations
    )
    identity = _deployment_identity(
        training_cutoff=deployment.training_cutoff,
        dependency_fingerprint=deployment.dependency_fingerprint,
        dependency_revision=deployment.dependency_revision,
        models=models,
        calibrations=calibrations,
        evidence_mode=deployment.evidence_mode,
    )
    return FrozenDraftDeployment(
        deployment_key=canonical_hash(identity),
        training_cutoff=deployment.training_cutoff,
        dependency_fingerprint=deployment.dependency_fingerprint,
        dependency_revision=deployment.dependency_revision,
        models=models,
        calibrations=calibrations,
    )


def _insert_deployment_without_replay(
    connection: sqlite3.Connection,
    deployment: FrozenDraftDeployment,
    *,
    created_at: datetime,
) -> None:
    timestamp = created_at.isoformat()
    for model in deployment.models:
        connection.execute(
            """INSERT OR IGNORE INTO draft_model_artifacts
               (model_hash, model_version, model_kind, horizon_minutes,
                training_cutoff, feature_schema_hash, training_input_hash,
                artifact_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                model.model_hash,
                model.model_version,
                model.model_kind,
                model.horizon_minutes,
                model.training_cutoff.isoformat(),
                model.feature_schema_hash,
                model.training_input_hash,
                canonical_json_bytes(model.to_payload()).decode(),
                timestamp,
            ),
        )
    for calibration in deployment.calibrations:
        connection.execute(
            """INSERT OR IGNORE INTO draft_calibration_artifacts
               (calibration_hash, model_hash, calibration_version,
                horizon_minutes, evidence_mode, support, artifact_json,
                created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                calibration.calibration_hash,
                calibration.model_hash,
                calibration.calibration_version,
                calibration.horizon_minutes,
                calibration.evidence_mode,
                calibration.support,
                canonical_json_bytes(calibration.to_payload()).decode(),
                timestamp,
            ),
        )
    connection.execute(
        """INSERT INTO draft_deployment_bundles
           (deployment_key, model_hashes_json, calibration_hashes_json,
            training_cutoff, dependency_fingerprint, dependency_revision,
            evidence_mode, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            deployment.deployment_key,
            canonical_json_bytes(
                {
                    str(row.horizon_minutes): row.model_hash
                    for row in deployment.models
                }
            ).decode(),
            canonical_json_bytes(
                {
                    str(row.horizon_minutes): row.calibration_hash
                    for row in deployment.calibrations
                }
            ).decode(),
            deployment.training_cutoff.isoformat(),
            deployment.dependency_fingerprint,
            deployment.dependency_revision,
            deployment.evidence_mode,
            timestamp,
        ),
    )
    connection.commit()


def _record_dependency_change(
    connection: sqlite3.Connection,
    *,
    affected_from: datetime,
) -> int:
    current = int(
        connection.execute(
            """SELECT dependency_revision FROM draft_lineage_revisions
                WHERE singleton=1"""
        ).fetchone()[0]
    )
    revision = current + 1
    connection.execute(
        """UPDATE draft_lineage_revisions
              SET dependency_revision=?, updated_at=?
            WHERE singleton=1""",
        (revision, affected_from.isoformat()),
    )
    connection.execute(
        """INSERT INTO draft_lineage_changes
           (dependency_revision, affected_from_unix, source_relation,
            operation, changed_at)
           VALUES (?, ?, 'test_dependency', 'INSERT', ?)""",
        (revision, int(affected_from.timestamp()), affected_from.isoformat()),
    )
    connection.commit()
    return revision


@pytest.fixture
def prepared_database(tmp_path: Path) -> Path:
    database = tmp_path / "publisher.db"
    prepare_database(database, tmp_path / "backups")
    return database


def _strict_result():
    mapping = SimpleNamespace(
        mapping_id=7,
        event_id="event-1",
        canonical_team_one_id=101,
        canonical_team_two_id=202,
    )
    return SimpleNamespace(eligible=True, reason="eligible", mapping=mapping)


def test_publisher_singleton_lock_fences_competing_processes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "publisher.db"
    with publisher_singleton_lock(database):
        with pytest.raises(RuntimeError, match="already running"):
            with publisher_singleton_lock(database):
                raise AssertionError("competing publisher acquired the lock")
    with publisher_singleton_lock(database):
        pass


def test_schema_prepared_direct_start_requires_manager_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "publisher.db"
    database.touch()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "draft_publisher.py",
            "--database",
            str(database),
            "--schema-prepared",
            "--deployment-key",
            "a" * 64,
            "--once",
        ],
    )
    monkeypatch.setattr(
        draft_publisher_module,
        "run_publisher",
        lambda *_args, **_kwargs: pytest.fail(
            "direct prepared publisher reached runtime"
        ),
    )

    with pytest.raises(RuntimeError, match="managed child authority is required"):
        draft_publisher_module.main()


def test_schema_prepared_managed_start_skips_full_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "publisher.db"
    database.touch()
    authority_calls: list[tuple[Path, bool]] = []

    @contextmanager
    def authority(path: Path, *, require_manager_child: bool = False):
        authority_calls.append((path, require_manager_child))
        yield

    class UnexpectedStore:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pytest.fail("prepared publisher repeated schema preparation")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "draft_publisher.py",
            "--database",
            str(database),
            "--schema-prepared",
            "--deployment-key",
            "a" * 64,
            "--once",
        ],
    )
    monkeypatch.setattr(
        draft_publisher_module,
        "database_writer_authority",
        authority,
    )
    monkeypatch.setattr(draft_publisher_module, "LiveBettingStore", UnexpectedStore)
    monkeypatch.setattr(
        draft_publisher_module,
        "run_publisher",
        lambda *_args, **_kwargs: 0,
    )

    assert draft_publisher_module.main() == 0
    assert authority_calls == [(database.resolve(), True)]


def test_publisher_singleton_lock_fences_hard_link_alias(tmp_path: Path) -> None:
    database = tmp_path / "publisher.db"
    alias = tmp_path / "publisher-alias.db"
    database.touch()
    os.link(database, alias)

    with publisher_singleton_lock(database):
        with pytest.raises(RuntimeError, match="already running"):
            with publisher_singleton_lock(alias):
                raise AssertionError("hard-link alias acquired a second lock")


def test_runtime_publisher_fails_closed_without_frozen_deployment(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("runtime publisher attempted to build or recalibrate")

    monkeypatch.setattr(draft_publisher_module, "_build_and_persist", forbidden)
    monkeypatch.setattr(
        draft_publisher_module,
        "build_prospective_calibration_deployment",
        forbidden,
    )

    result = draft_publisher_module._run_publisher_locked(
        prepared_database,
        once=True,
        interval_seconds=0.01,
        rebuild_artifacts=False,
        deployment_key="0" * 64,
        history_timeout_seconds=0.1,
    )

    assert result == 2
    with LiveBettingStore(prepared_database) as store:
        health = store.connection.execute(
            "SELECT status, last_error, details_json FROM service_health "
            "WHERE component=?",
            (draft_publisher_module.PUBLISHER_COMPONENT,),
        ).fetchone()
    assert health is not None
    assert tuple(health[:2]) == ("unhealthy", "frozen_deployment_missing")
    assert json.loads(str(health[2]))["phase"] == "loading_deployment"


def test_runtime_publisher_history_timeout_is_bounded_and_recorded(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = SimpleNamespace(dependency_revision=1)
    monkeypatch.setattr(
        draft_publisher_module,
        "load_pinned_frozen_deployment",
        lambda _connection, *, deployment_key: deployment,
    )
    monkeypatch.setattr(
        draft_publisher_module,
        "_load_history_with_timeout",
        lambda _database, *, timeout_seconds: (_ for _ in ()).throw(
            draft_publisher_module._HistoryLoadTimeoutError(
                "history_load_timeout"
            )
        ),
    )
    monkeypatch.setattr(
        draft_publisher_module,
        "build_prospective_calibration_deployment",
        lambda *_args, **_kwargs: pytest.fail(
            "runtime publisher attempted to recalibrate"
        ),
    )

    result = draft_publisher_module._run_publisher_locked(
        prepared_database,
        once=True,
        interval_seconds=0.01,
        rebuild_artifacts=False,
        deployment_key="0" * 64,
        history_timeout_seconds=0.05,
    )

    assert result == 2
    with LiveBettingStore(prepared_database) as store:
        health = store.connection.execute(
            "SELECT status, last_error, details_json FROM service_health "
            "WHERE component=?",
            (draft_publisher_module.PUBLISHER_COMPONENT,),
        ).fetchone()
    assert health is not None
    assert tuple(health[:2]) == ("unhealthy", "history_load_timeout")
    assert json.loads(str(health[2]))["phase"] == "loading_history"


def test_history_timeout_interrupts_sqlite_and_closes_connection(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_connect = draft_publisher_module.connect
    closed: list[bool] = []

    class TrackingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def __getattr__(self, name: str):
            return getattr(self.connection, name)

        def close(self) -> None:
            self.connection.close()
            closed.append(True)

    def tracked_connect(*args: object, **kwargs: object) -> TrackingConnection:
        return TrackingConnection(original_connect(*args, **kwargs))

    def slow_history(
        connection: sqlite3.Connection,
        **_limits: object,
    ) -> tuple[str, tuple[()]]:
        connection.execute(
            "WITH RECURSIVE counter(value) AS ("
            "SELECT 1 UNION ALL SELECT value + 1 FROM counter "
            "WHERE value < 1000000000) SELECT sum(value) FROM counter"
        ).fetchone()
        return "fingerprint", ()

    monkeypatch.setattr(draft_publisher_module, "connect", tracked_connect)
    monkeypatch.setattr(
        draft_publisher_module,
        "load_bounded_prospective_history",
        slow_history,
    )

    with pytest.raises(
        draft_publisher_module._HistoryLoadTimeoutError,
        match="history_load_timeout",
    ):
        draft_publisher_module._load_history_with_timeout(
            prepared_database,
            timeout_seconds=0.01,
        )

    assert closed == [True]


@pytest.mark.parametrize(
    ("limit_name", "expected"),
    [
        ("MAX_RUNTIME_HISTORY_ROWS", "row limit"),
        ("MAX_RUNTIME_HISTORY_BYTES", "byte limit"),
    ],
)
def test_runtime_history_limits_reject_before_full_materialization(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    expected: str,
) -> None:
    _deployment(prepared_database)
    monkeypatch.setattr(draft_publisher_module, "MAX_RUNTIME_HISTORY_ROWS", 10**9)
    monkeypatch.setattr(draft_publisher_module, "MAX_RUNTIME_HISTORY_BYTES", 10**9)
    monkeypatch.setattr(draft_publisher_module, limit_name, 1)
    monkeypatch.setattr(
        backtest_module,
        "_dependency_fingerprint",
        lambda *_args, **_kwargs: pytest.fail(
            "oversized runtime history reached fingerprint materialization"
        ),
    )
    monkeypatch.setattr(
        backtest_module,
        "_load_draft_corpus",
        lambda *_args, **_kwargs: pytest.fail(
            "oversized runtime history reached corpus materialization"
        ),
    )

    with pytest.raises(
        draft_publisher_module._HistoryLoadLimitError,
        match="history_load_limit_exceeded",
    ):
        draft_publisher_module._load_history_with_timeout(
            prepared_database,
            timeout_seconds=10,
        )


def test_bounded_runtime_history_matches_full_small_fixture(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _deployment(prepared_database)
    connection = connect(prepared_database, row_factory=sqlite3.Row)
    try:
        expected_revision = int(
            connection.execute(
                """SELECT dependency_revision FROM draft_lineage_revisions
                    WHERE singleton=1"""
            ).fetchone()[0]
        )
        expected_fingerprint = draft_dependency_fingerprint(connection)
        expected_maps = load_prospective_history(connection)
    finally:
        connection.close()

    monkeypatch.setattr(
        backtest_module,
        "draft_dependency_fingerprint",
        lambda *_args, **_kwargs: pytest.fail(
            "runtime startup called the public full fingerprint API"
        ),
    )
    monkeypatch.setattr(
        backtest_module,
        "load_draft_corpus",
        lambda *_args, **_kwargs: pytest.fail(
            "runtime startup called the public full corpus API"
        ),
    )

    actual = draft_publisher_module._load_history_with_timeout(
        prepared_database,
        timeout_seconds=10,
    )

    assert actual.dependency_revision == expected_revision
    assert actual.dependency_fingerprint == expected_fingerprint
    assert actual.maps == expected_maps


def test_runtime_history_row_limit_finishes_count_phase_before_byte_phase(
    prepared_database: Path,
) -> None:
    _deployment(prepared_database)
    statements: list[str] = []
    connection = connect(prepared_database, row_factory=sqlite3.Row)
    connection.set_trace_callback(statements.append)
    try:
        with pytest.raises(backtest_module.DraftDependencyLimitError):
            backtest_module.load_bounded_draft_snapshot(
                connection,
                availability_mode=backtest_module.AvailabilityMode.PROSPECTIVE,
                assignment_version=PROSPECTIVE_ASSIGNMENT_VERSION,
                max_rows=1,
                max_bytes=10**9,
                max_value_bytes=10**6,
            )
    finally:
        connection.close()

    assert any("COUNT(*)" in statement for statement in statements)
    assert not any("SUM(" in statement for statement in statements)


def test_runtime_history_counts_corpus_only_json_before_materialization(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _deployment(prepared_database)
    payload = json.dumps(["x" * 4096])
    connection = connect(prepared_database, row_factory=sqlite3.Row)
    try:
        fact_count = int(
            connection.execute("SELECT COUNT(*) FROM player_map_facts").fetchone()[0]
        )
        assert fact_count > 1
        connection.execute(
            "UPDATE player_map_facts SET missing_fields_json=?",
            (payload,),
        )
        connection.commit()
        monkeypatch.setattr(
            backtest_module,
            "_dependency_fingerprint",
            lambda *_args, **_kwargs: pytest.fail(
                "oversized corpus-only JSON reached fingerprint materialization"
            ),
        )
        monkeypatch.setattr(
            backtest_module,
            "_load_draft_corpus",
            lambda *_args, **_kwargs: pytest.fail(
                "oversized corpus-only JSON reached corpus materialization"
            ),
        )

        with pytest.raises(
            backtest_module.DraftDependencyLimitError,
            match="byte limit",
        ):
            backtest_module.load_bounded_draft_snapshot(
                connection,
                availability_mode=backtest_module.AvailabilityMode.PROSPECTIVE,
                assignment_version=PROSPECTIVE_ASSIGNMENT_VERSION,
                max_rows=10**9,
                max_bytes=len(payload.encode("utf-8")) * fact_count - 1,
                max_value_bytes=1024 * 1024,
            )
    finally:
        connection.close()


def test_runtime_history_counts_unused_match_player_columns(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _deployment(prepared_database)
    payload = "x" * 4096
    connection = connect(prepared_database, row_factory=sqlite3.Row)
    try:
        player_count = int(
            connection.execute("SELECT COUNT(*) FROM match_players").fetchone()[0]
        )
        assert player_count > 1
        connection.execute("UPDATE match_players SET kills=?", (payload,))
        connection.commit()
        monkeypatch.setattr(
            backtest_module,
            "_dependency_fingerprint",
            lambda *_args, **_kwargs: pytest.fail(
                "oversized unused player columns reached fingerprint materialization"
            ),
        )
        monkeypatch.setattr(
            backtest_module,
            "_load_draft_corpus",
            lambda *_args, **_kwargs: pytest.fail(
                "oversized unused player columns reached corpus materialization"
            ),
        )

        with pytest.raises(
            backtest_module.DraftDependencyLimitError,
            match="byte limit",
        ):
            backtest_module.load_bounded_draft_snapshot(
                connection,
                availability_mode=backtest_module.AvailabilityMode.PROSPECTIVE,
                assignment_version=PROSPECTIVE_ASSIGNMENT_VERSION,
                max_rows=10**9,
                max_bytes=len(payload.encode("utf-8")) * player_count - 1,
                max_value_bytes=1024 * 1024,
            )
    finally:
        connection.close()


def test_explicit_offline_rebuild_is_the_only_runtime_build_path(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    built: list[Path] = []
    deployment = SimpleNamespace(deployment_key="offline-deployment")

    def build(database: Path, _now: datetime) -> SimpleNamespace:
        built.append(database)
        return deployment

    monkeypatch.setattr(draft_publisher_module, "_build_and_persist", build)
    monkeypatch.setattr(
        draft_publisher_module,
        "load_latest_frozen_deployment",
        lambda _connection: pytest.fail("offline rebuild entered runtime load"),
    )
    monkeypatch.setattr(
        draft_publisher_module,
        "_load_history_with_timeout",
        lambda *_args, **_kwargs: pytest.fail("offline rebuild loaded history"),
    )
    monkeypatch.setattr(
        draft_publisher_module,
        "build_prospective_calibration_deployment",
        lambda *_args, **_kwargs: pytest.fail("offline rebuild recalibrated"),
    )

    result = draft_publisher_module._run_publisher_locked(
        prepared_database,
        once=True,
        interval_seconds=0.01,
        rebuild_artifacts=True,
        history_timeout_seconds=0.1,
    )

    assert result == 0
    assert built == [prepared_database]
    assert json.loads(capsys.readouterr().out) == {
        "status": "ok",
        "phase": "offline_rebuild_complete",
        "deployment_key": "offline-deployment",
        "supervisor_argument": (
            "--draft-deployment-key offline-deployment"
        ),
    }
    with LiveBettingStore(prepared_database) as store:
        health = store.connection.execute(
            "SELECT status, last_error, details_json FROM service_health "
            "WHERE component=?",
            (draft_publisher_module.PUBLISHER_COMPONENT,),
        ).fetchone()
    assert health is not None
    assert tuple(health[:2]) == ("healthy", None)
    assert json.loads(str(health[2]))["phase"] == "offline_rebuild_complete"


def test_offline_rebuild_requires_once(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        draft_publisher_module,
        "_build_and_persist",
        lambda *_args, **_kwargs: pytest.fail(
            "non-once offline rebuild attempted to build"
        ),
    )

    result = draft_publisher_module._run_publisher_locked(
        prepared_database,
        once=False,
        interval_seconds=0.01,
        rebuild_artifacts=True,
        history_timeout_seconds=0.1,
    )

    assert result == 2
    with LiveBettingStore(prepared_database) as store:
        health = store.connection.execute(
            "SELECT status, last_error FROM service_health WHERE component=?",
            (draft_publisher_module.PUBLISHER_COMPONENT,),
        ).fetchone()
    assert health is not None
    assert tuple(health) == ("unhealthy", "offline_rebuild_requires_once")


@pytest.mark.parametrize(
    ("failure", "forbidden", "expected", "expected_truncated"),
    [
        (
            RuntimeError(
                "artifact source C:\\private\\model.db "
                "https://user:pass@example.test/model?token=url-secret "
                "password=hunter2 api_key=key-secret "
                + "diagnostic " * 100
            ),
            (
                "C:\\private\\model.db",
                "user:pass",
                "url-secret",
                "hunter2",
                "key-secret",
            ),
            ("[path]", "[url]", "[credential]"),
            True,
        ),
        (
            ValueError(
                "SELECT password FROM credentials WHERE token='sql-secret'"
            ),
            ("SELECT", "credentials", "sql-secret"),
            ("sensitive SQL diagnostic removed",),
            False,
        ),
        (
            RuntimeError(
                'paths "C:\\Program Files\\Private Models\\model.bin" '
                "'\\\\server\\secret share\\draft model.bin' "
                "'/srv/private models/draft model.bin' ordinary text remains"
            ),
            ("Program Files", "secret share", "private models", "model.bin"),
            ("paths [path] [path] [path] ordinary text remains",),
            False,
        ),
        (
            RuntimeError(
                "paths C:\\Program Files\\Private Models\\model.bin; "
                "\\\\server\\secret share\\draft.bin, "
                "/srv/private models/draft.bin ordinary text remains"
            ),
            ("Program Files", "secret share", "/srv/private", "draft.bin"),
            ("paths [path]; [path], [path] ordinary text remains",),
            False,
        ),
        (
            RuntimeError(
                "request rejected Authorization: Basic "
                "QWxhZGRpbjpvcGVuIHNlc2FtZQ=="
            ),
            ("Basic", "QWxhZGRpbjpvcGVuIHNlc2FtZQ=="),
            ("request rejected [credential]",),
            False,
        ),
        (
            RuntimeError(
                'request rejected AUTHORIZATION Digest username="private", '
                'realm="secret", nonce="bm9uY2U="'
            ),
            ("Digest", "private", "secret", "bm9uY2U="),
            ("request rejected [credential]",),
            False,
        ),
        (
            RuntimeError(
                "request rejected authorization=CustomScheme "
                "Y3VzdG9tLXNlY3JldA=="
            ),
            ("CustomScheme", "Y3VzdG9tLXNlY3JldA=="),
            ("request rejected [credential]",),
            False,
        ),
        (
            RuntimeError(
                "keep client_secret=s1 X-API-Key: QWxhZGRpbjpvcGVu "
                "OAUTH_TOKEN s3 db-password/s4 "
                "MixedCase-Secret='s five' keep-tail"
            ),
            (
                "client_secret",
                "s1",
                "X-API-Key",
                "QWxhZGRpbjpvcGVu",
                "OAUTH_TOKEN",
                "s3",
                "db-password",
                "s4",
                "MixedCase-Secret",
                "s five",
            ),
            (
                "keep [credential] [credential] [credential] "
                "[credential] [credential] keep-tail",
            ),
            False,
        ),
        (
            RuntimeError(
                "route /api returned ordinary text and relative "
                "./not/absolute remains"
            ),
            ("/api",),
            (
                "route [path] returned ordinary text and relative "
                "./not/absolute remains",
            ),
            False,
        ),
        (
            RuntimeError(
                "paths C:\\Program Files\\Private Models denied; "
                "\\\\server\\secret share\\draft model not found, "
                "/srv/private models/draft model permission denied "
                "ordinary text remains"
            ),
            (
                "Program Files",
                "Private Models",
                "secret share",
                "draft model",
                "/srv/private",
            ),
            (
                "paths [path] denied; [path] not found, [path] "
                "permission denied ordinary text remains",
            ),
            False,
        ),
        (
            RuntimeError(
                "windows C:\\Program Files\\Private Models; "
                "unc \\\\server\\secret share\\draft model; "
                "posix /srv/private models/draft model"
            ),
            ("Program Files", "secret share", "/srv/private", "draft model"),
            ("windows [path]; unc [path]; posix [path]",),
            False,
        ),
        (
            RuntimeError(
                "authorization failed because token expired while password "
                "validation continued"
            ),
            (),
            (
                "authorization failed because token expired while password "
                "validation continued",
            ),
            False,
        ),
        (
            RuntimeError(
                "keep token abc123 password hunter2 client_secret alpha "
                "keep-tail"
            ),
            ("abc123", "hunter2", "alpha"),
            (
                "keep [credential] [credential] [credential] keep-tail",
            ),
            False,
        ),
    ],
)
def test_offline_rebuild_failure_uses_one_bounded_redacted_summary(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
    forbidden: tuple[str, ...],
    expected: tuple[str, ...],
    expected_truncated: bool,
) -> None:
    def fail_build(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(draft_publisher_module, "_build_and_persist", fail_build)

    result = draft_publisher_module._run_publisher_locked(
        prepared_database,
        once=True,
        interval_seconds=0.01,
        rebuild_artifacts=True,
        history_timeout_seconds=0.1,
    )

    assert result == 2
    stderr_payload = json.loads(capsys.readouterr().err)
    with LiveBettingStore(prepared_database) as store:
        health = store.connection.execute(
            "SELECT status, last_error, details_json FROM service_health "
            "WHERE component=?",
            (draft_publisher_module.PUBLISHER_COMPONENT,),
        ).fetchone()
    assert health is not None
    assert tuple(health[:2]) == ("unhealthy", "offline_rebuild_failed")
    details = json.loads(str(health[2]))
    summary = details["failure"]
    assert stderr_payload == {
        "error": "offline_rebuild_failed",
        "failure": summary,
        "status": "error",
    }
    assert summary["exception_type"] == type(failure).__name__
    assert len(summary["message"]) <= 240
    assert summary["message_truncated"] is expected_truncated
    serialized = json.dumps(
        {"health": dict(health), "stderr": stderr_payload},
        ensure_ascii=False,
    )
    for sensitive in forbidden:
        assert sensitive not in serialized
    for fragment in expected:
        assert fragment in summary["message"]


@pytest.mark.parametrize(
    ("load_error", "error_code"),
    [
        (
            draft_publisher_module._FrozenDeploymentLineageError("stale"),
            "frozen_deployment_lineage_invalid",
        ),
        (ValueError("invalid artifact"), "frozen_deployment_invalid"),
    ],
)
def test_runtime_publisher_records_stable_deployment_failures(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
    load_error: Exception,
    error_code: str,
) -> None:
    monkeypatch.setattr(
        draft_publisher_module,
        "load_pinned_frozen_deployment",
        lambda _connection, *, deployment_key: (_ for _ in ()).throw(load_error),
    )
    monkeypatch.setattr(
        draft_publisher_module,
        "_build_and_persist",
        lambda *_args, **_kwargs: pytest.fail(
            "runtime publisher attempted to rebuild a deployment"
        ),
    )

    result = draft_publisher_module._run_publisher_locked(
        prepared_database,
        once=True,
        interval_seconds=0.01,
        rebuild_artifacts=False,
        deployment_key="0" * 64,
        history_timeout_seconds=0.1,
    )

    assert result == 2
    with LiveBettingStore(prepared_database) as store:
        health = store.connection.execute(
            "SELECT status, last_error FROM service_health WHERE component=?",
            (draft_publisher_module.PUBLISHER_COMPONENT,),
        ).fetchone()
    assert health is not None
    assert tuple(health) == ("unhealthy", error_code)


def test_runtime_publisher_success_never_recalibrates(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = SimpleNamespace(dependency_revision=1)
    history = SimpleNamespace(dependency_revision=1)
    report = SimpleNamespace(
        deployment_key="frozen-deployment",
        candidates=0,
        inserted=0,
        unchanged=0,
        skipped=0,
        outcomes_inserted=0,
    )
    monkeypatch.setattr(
        draft_publisher_module,
        "load_pinned_frozen_deployment",
        lambda _connection, *, deployment_key: deployment,
    )
    monkeypatch.setattr(
        draft_publisher_module,
        "_load_history_with_timeout",
        lambda _database, *, timeout_seconds: history,
    )
    monkeypatch.setattr(
        draft_publisher_module,
        "_refresh_dependency_inputs",
        lambda _database, current, current_history, *, now: (
            current,
            current_history,
        ),
    )
    monkeypatch.setattr(
        draft_publisher_module,
        "publish_cycle",
        lambda *_args, **_kwargs: report,
    )
    monkeypatch.setattr(
        draft_publisher_module,
        "build_prospective_calibration_deployment",
        lambda *_args, **_kwargs: pytest.fail(
            "runtime publisher attempted to recalibrate"
        ),
    )

    result = draft_publisher_module._run_publisher_locked(
        prepared_database,
        once=True,
        interval_seconds=0.01,
        rebuild_artifacts=False,
        deployment_key="0" * 64,
        history_timeout_seconds=0.1,
    )

    assert result == 0
    with LiveBettingStore(prepared_database) as store:
        health = store.connection.execute(
            "SELECT status, last_error, details_json FROM service_health "
            "WHERE component=?",
            (draft_publisher_module.PUBLISHER_COMPONENT,),
        ).fetchone()
    assert health is not None
    assert tuple(health[:2]) == ("healthy", None)
    details = json.loads(str(health[2]))
    assert details["phase"] == "healthy"
    assert details["process_pid"] == os.getpid()
    assert details["process_created_at"] > 0
    assert details["process_generation"] == (
        draft_publisher_module._publisher_process_generation(
            details["process_pid"],
            details["process_created_at"],
        )
    )
    assert details["history_dependency_revision"] == 1
    assert details["history_refreshed"] is False


def test_runtime_refresh_reloads_post_cutoff_history_and_keeps_pin(
    prepared_database: Path,
) -> None:
    deployment = _deployment(prepared_database)
    history = draft_publisher_module._load_history_with_timeout(
        prepared_database,
        timeout_seconds=10,
    )
    with LiveBettingStore(prepared_database) as store:
        next_revision = _record_dependency_change(
            store.connection,
            affected_from=CUTOFF + timedelta(seconds=1),
        )

    loaded, refreshed = draft_publisher_module._refresh_dependency_inputs(
        prepared_database,
        deployment,
        history,
        now=CUTOFF + timedelta(seconds=2),
    )

    assert loaded is deployment
    assert loaded.deployment_key == deployment.deployment_key
    assert refreshed is not history
    assert refreshed.dependency_revision == next_revision


def test_runtime_refresh_rejects_pre_cutoff_revision(
    prepared_database: Path,
) -> None:
    deployment = _deployment(prepared_database)
    history = draft_publisher_module._load_history_with_timeout(
        prepared_database,
        timeout_seconds=10,
    )
    with LiveBettingStore(prepared_database) as store:
        _record_dependency_change(
            store.connection,
            affected_from=CUTOFF - timedelta(seconds=1),
        )

    with pytest.raises(ValueError, match="changed_before_cutoff"):
        draft_publisher_module._refresh_dependency_inputs(
            prepared_database,
            deployment,
            history,
            now=CUTOFF + timedelta(seconds=2),
        )


def test_runtime_refresh_rejects_revision_race(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = _deployment(prepared_database)
    history = draft_publisher_module._load_history_with_timeout(
        prepared_database,
        timeout_seconds=10,
    )
    with LiveBettingStore(prepared_database) as store:
        _record_dependency_change(
            store.connection,
            affected_from=CUTOFF + timedelta(seconds=1),
        )
    real_load = draft_publisher_module._load_history_with_timeout

    def race(database: Path, *, timeout_seconds: float):
        snapshot = real_load(database, timeout_seconds=timeout_seconds)
        with LiveBettingStore(database) as store:
            _record_dependency_change(
                store.connection,
                affected_from=CUTOFF + timedelta(seconds=2),
            )
        return snapshot

    monkeypatch.setattr(
        draft_publisher_module,
        "_load_history_with_timeout",
        race,
    )
    with pytest.raises(ValueError, match="changed during runtime refresh"):
        draft_publisher_module._refresh_dependency_inputs(
            prepared_database,
            deployment,
            history,
            now=CUTOFF + timedelta(seconds=3),
        )


def _insert_anchor(store: LiveBettingStore, *, captured_at: datetime = CUTOFF) -> None:
    assert store.insert_vision_observation(
        make_test_vision_observation(
            raybet_match_id="match-1",
            map_number=1,
            captured_at=captured_at,
            game_clock_seconds=120,
            radiant_hero_ids=(1, 2, 3, 4, 5),
            dire_hero_ids=(6, 7, 8, 9, 10),
            clock_confidence=0.99,
            draft_confidence=0.99,
            radiant_team_side="team_one",
            label="draft-publisher-anchor",
        )
    )


def _insert_event(connection: sqlite3.Connection) -> None:
    if connection.execute(
        "SELECT 1 FROM event_registry WHERE event_id='event-1'"
    ).fetchone() is not None:
        return
    connection.execute(
        """INSERT INTO event_registry
           (event_id, canonical_name, tier, prize_pool_usd,
            main_event_start_at, main_event_end_at, opendota_league_id,
            secondary_provider_ids_json, official_evidence_urls_json,
            evidence_status, scope_policy_version, scope, approval_status,
            approved_by, approved_at, reconciliation_status,
            expected_map_count, observed_map_count, public_map_count,
            reconciliation_note, included_stages_json,
            excluded_categories_json, include_internal_lcq,
            excludes_qualifiers, excludes_division_2, excludes_exhibitions,
            excludes_forfeits, excludes_void_remakes, created_at, updated_at)
           VALUES ('event-1', 'Event 1', 'tier_1', 1000000, ?, ?, 999,
                   '{}', '["https://example.invalid/event"]',
                   'manually_audited', 'scope-v1', 'formal_main_event',
                   'approved', 'tester', ?, 'not_required', NULL, NULL, NULL,
                   NULL, '["main_event"]', '[]', 0, 1, 1, 1, 1, 1, ?, ?)""",
        (
            (CUTOFF - timedelta(days=1)).isoformat(),
            (CUTOFF + timedelta(days=1)).isoformat(),
            (CUTOFF - timedelta(days=2)).isoformat(),
            (CUTOFF - timedelta(days=2)).isoformat(),
            (CUTOFF - timedelta(days=2)).isoformat(),
        ),
    )


def _insert_future_event(connection: sqlite3.Connection) -> None:
    source = connection.execute(
        "SELECT * FROM event_registry WHERE event_id='event-1'"
    ).fetchone()
    assert source is not None
    payload = dict(source)
    payload.update(
        {
            "event_id": "event-2",
            "canonical_name": "Future Event",
            "main_event_start_at": (CUTOFF + timedelta(days=10)).isoformat(),
            "main_event_end_at": (CUTOFF + timedelta(days=11)).isoformat(),
            "opendota_league_id": 1000,
            "created_at": (CUTOFF + timedelta(days=2)).isoformat(),
            "updated_at": (CUTOFF + timedelta(days=2)).isoformat(),
        }
    )
    columns = tuple(payload)
    connection.execute(
        "INSERT INTO event_registry ("
        + ",".join(columns)
        + ") VALUES ("
        + ",".join("?" for _ in columns)
        + ")",
        tuple(payload[column] for column in columns),
    )
    connection.commit()


def _insert_prospective_history(connection: sqlite3.Connection) -> None:
    if connection.execute(
        "SELECT 1 FROM match_ingest_status WHERE match_id=7000000"
    ).fetchone() is not None:
        return
    league_id = int(
        connection.execute(
            "SELECT opendota_league_id FROM event_registry WHERE event_id='event-1'"
        ).fetchone()[0]
    )
    for group in range(5):
        for offset in range(4):
            match_id = 7_000_000 + group * 10 + offset
            started = CUTOFF - timedelta(days=30 - group, hours=offset * 2)
            completed = started + timedelta(hours=1)
            usable = completed + timedelta(minutes=1)
            content_hash = hashlib.sha256(f"history:{match_id}".encode()).hexdigest()
            artifact_id = f"opendota:{content_hash}"
            timestamp = usable.isoformat()
            connection.execute(
                """INSERT INTO raw_source_artifacts
                   (artifact_id, content_hash, source, artifact_use, endpoint,
                    sanitized_request_identity, storage_path, uncompressed_bytes,
                    compressed_bytes, received_at, first_usable_at,
                    schema_fingerprint, event_id, match_id, created_at)
                   VALUES (?, ?, 'opendota', 'primary', ?, ?, ?, 1, 1, ?, ?,
                           'test-schema', 'event-1', ?, ?)""",
                (
                    artifact_id,
                    content_hash,
                    f"/api/matches/{match_id}",
                    f"GET /api/matches/{match_id}",
                    f"raw/{match_id}.json.gz",
                    timestamp,
                    timestamp,
                    match_id,
                    timestamp,
                ),
            )
            connection.execute(
                """INSERT INTO match_ingest_status
                   (match_id, event_id, start_time, series_id, map_number,
                    stage_scope, stage_in_scope, has_valid_result,
                    is_exhibition, is_forfeit, is_void_remake, ingest_state,
                    basic_result_state, detailed_parse_state, player_readiness,
                    state_readiness, draft_readiness, latest_raw_artifact_id,
                    latest_raw_content_hash, normalizer_version, first_usable_at,
                    discovered_at, updated_at)
                   VALUES (?, 'event-1', ?, ?, ?, 'main_event', 1, 1, 0, 0, 0,
                           'complete', 'ready', 'ready', 'ready', 'ready',
                           'ready', ?, ?, 'opendota-exact-v1', ?, ?, ?)""",
                (
                    match_id,
                    int(started.timestamp()),
                    8_000_000 + group,
                    offset + 1,
                    artifact_id,
                    content_hash,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """INSERT INTO raw_source_observations
                   (observation_id, artifact_id, content_hash, source,
                    artifact_use, endpoint, sanitized_request_identity,
                    source_at, received_at, first_usable_at,
                    schema_fingerprint, event_id, match_id, http_status,
                    created_at)
                   VALUES (?, ?, ?, 'opendota', 'primary', ?, ?, ?, ?, ?,
                           'test-schema', 'event-1', ?, 200, ?)""",
                (
                    f"observation:{content_hash}",
                    artifact_id,
                    content_hash,
                    f"/api/matches/{match_id}",
                    f"GET /api/matches/{match_id}",
                    timestamp,
                    timestamp,
                    timestamp,
                    match_id,
                    timestamp,
                ),
            )
            connection.execute(
                """INSERT INTO matches
                   (match_id, radiant_team_id, dire_team_id, radiant_win,
                    duration, start_time, leagueid, series_id, patch)
                   VALUES (?, 101, 202, ?, 3600, ?, ?, ?, 59)""",
                (
                    match_id,
                    int(offset < group),
                    int(started.timestamp()),
                    league_id,
                    8_000_000 + group,
                ),
            )
            heroes = tuple(range(group * 10 + 1, group * 10 + 11))
            connection.executemany(
                "INSERT OR IGNORE INTO heroes(hero_id) VALUES (?)",
                ((hero_id,) for hero_id in heroes),
            )
            for index, hero_id in enumerate(heroes):
                radiant = index < 5
                player_slot = index if radiant else 128 + index - 5
                team_id = 101 if radiant else 202
                account_id = 100_000 + group * 100 + index
                connection.execute(
                    """INSERT INTO match_players
                       (match_id, account_id, player_slot, hero_id,
                        is_radiant, team_id)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        match_id,
                        account_id,
                        player_slot,
                        hero_id,
                        int(radiant),
                        team_id,
                    ),
                )
                connection.execute(
                    """INSERT INTO picks_bans
                       (match_id, hero_id, is_pick, team, ord)
                       VALUES (?, ?, 1, ?, ?)""",
                    (match_id, hero_id, int(not radiant), index),
                )
                facts = {
                    "hero_id": hero_id,
                    "stuns": 12.0,
                    "hero_healing": 100,
                    "last_hits": 200,
                    "tower_damage": 2_000,
                    "net_worth": 20_000,
                    "buyback_log": [],
                }
                connection.execute(
                    """INSERT INTO player_map_facts
                       (match_id, player_slot, account_id, team_id, hero_id,
                        is_radiant, facts_json, missing_fields_json, coverage,
                        source_artifact_id, source_content_hash, fact_version,
                        first_usable_at, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, '[]', 1.0, ?, ?, ?, ?, ?)""",
                    (
                        match_id,
                        player_slot,
                        account_id,
                        team_id,
                        hero_id,
                        int(radiant),
                        json.dumps(facts, sort_keys=True),
                        artifact_id,
                        content_hash,
                        f"opendota-exact-v1:{content_hash}",
                        timestamp,
                        timestamp,
                    ),
                )
                role_cutoff = (started - timedelta(minutes=1)).isoformat()
                role_hash = hashlib.sha256(
                    f"role:{match_id}:{player_slot}".encode()
                ).hexdigest()
                for assignment_version in (
                    RECONSTRUCTED_ASSIGNMENT_VERSION,
                    PROSPECTIVE_ASSIGNMENT_VERSION,
                ):
                    connection.execute(
                        """INSERT INTO player_role_assignments
                           (match_id, player_slot, account_id, team_id, purpose,
                            position, assignment_source, confidence, input_cutoff,
                            input_hash, assignment_version, created_at)
                           VALUES (?, ?, ?, ?, 'expected_position', ?,
                                   'historical_pattern', 0.9, ?, ?, ?, ?)""",
                        (
                            match_id,
                            player_slot,
                            account_id,
                            team_id,
                            index % 5 + 1,
                            role_cutoff,
                            role_hash,
                            assignment_version,
                            role_cutoff,
                        ),
                    )
    connection.commit()


def _insert_mapping(
    connection: sqlite3.Connection,
    *,
    mapping_id: int,
    match_id: str,
    observed_at: datetime,
) -> None:
    identity_json = "{}"
    identity_hash = hashlib.sha256(identity_json.encode()).hexdigest()
    connection.execute(
        """INSERT INTO strict_live_map_mappings
           (mapping_id, raybet_match_id, map_number, event_id,
            team_one_id, team_two_id, canonical_team_one_id,
            canonical_team_one_name, canonical_team_two_id,
            canonical_team_two_name, canonical_identity_json,
            canonical_identity_hash, crosswalk_evidence_json,
            crosswalk_evidence_hash, stage_scope, scheduled_at_utc,
            raybet_best_of, raybet_identity_json, raybet_identity_hash,
            raybet_metadata_updated_at, source, evidence_json, evidence_hash,
            mapping_version, acceptance_mode, automatic_approval_id,
            accepted_by, accepted_at, recorded_at, created_at)
           VALUES (?, ?, 1, 'event-1', 501, 502, 101, 'Alpha', 202, 'Beta',
                   ?, ?, ?, ?, 'main_event', ?, 1, ?, ?, ?, 'test', ?, ?,
                   'strict-live-map-v3', 'manual_exact', NULL, 'tester', ?, ?, ?)""",
        (
            mapping_id,
            match_id,
            identity_json,
            identity_hash,
            identity_json,
            identity_hash,
            observed_at.isoformat(),
            identity_json,
            identity_hash,
            (observed_at - timedelta(minutes=1)).isoformat(),
            identity_json,
            identity_hash,
            (observed_at - timedelta(minutes=1)).isoformat(),
            (observed_at - timedelta(minutes=1)).isoformat(),
            (observed_at - timedelta(minutes=1)).isoformat(),
        ),
    )


def _insert_prospective_sample(
    store: LiveBettingStore,
    *,
    opendota_archive: RawArchive,
    deployment: FrozenDraftDeployment,
    history: tuple[DraftMapEvidence, ...],
    index: int,
    outcome: int,
) -> dict[int, CalibrationSample]:
    connection = store.connection
    observed_at = CUTOFF + timedelta(minutes=index + 1)
    settled_at = observed_at + timedelta(hours=1)
    match_id = str(8_000_000 + index)
    mapping_id = 1_000 + index
    dota_match_id = 9_000_000 + index
    curve_key = canonical_hash({"sample": index})
    group = index // 20
    radiant = list(range(group * 10 + 1, group * 10 + 6))
    dire = list(range(group * 10 + 6, group * 10 + 11))
    anchor_hash = hashlib.sha256(
        json.dumps(
            {"radiant": radiant, "dire": dire},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    feature_dependency_fingerprint = draft_dependency_fingerprint(connection)
    feature_dependency_revision = int(
        connection.execute(
            "SELECT dependency_revision FROM draft_lineage_revisions"
        ).fetchone()[0]
    )
    observation = make_test_vision_observation(
        raybet_match_id=match_id,
        map_number=1,
        captured_at=observed_at,
        game_clock_seconds=120,
        radiant_hero_ids=tuple(radiant),
        dire_hero_ids=tuple(dire),
        clock_confidence=0.99,
        draft_confidence=0.99,
        radiant_team_side="team_one",
        label=f"prospective-sample-{index}",
    )
    assert store.insert_vision_observation(observation)
    _insert_mapping(
        connection,
        mapping_id=mapping_id,
        match_id=match_id,
        observed_at=observed_at,
    )
    anchor = DraftAnchor(
        raybet_match_id=match_id,
        map_number=1,
        draft_hash=anchor_hash,
        radiant_heroes=tuple(radiant),
        dire_heroes=tuple(dire),
        radiant_team_side="team_one",
        anchored_at=observed_at,
        source_frame_ref=observation.source_frame_ref,
        team_side_anchored_at=observed_at,
        team_side_source_frame_ref=observation.source_frame_ref,
    )
    mapping = SimpleNamespace(
        mapping_id=mapping_id,
        event_id="event-1",
        canonical_team_one_id=101,
        canonical_team_two_id=202,
    )
    target = build_live_draft_target(connection, anchor, mapping, observed_at)
    snapshot, feature_artifact = build_draft_feature_artifact(target, history)
    target_hash = snapshot.input_hash
    values = snapshot.pure_values()
    connection.execute(
        """INSERT INTO prospective_draft_curves
           (curve_key, raybet_match_id, map_number, strict_mapping_id,
            lineup_hash, radiant_hero_ids_json, dire_hero_ids_json,
            prediction_cutoff, first_usable_at, availability_mode, created_at,
            radiant_team_side, anchor_draft_hash, anchor_source_frame_ref,
            anchor_anchored_at, anchor_team_side_source_frame_ref,
            anchor_team_side_anchored_at, deployment_key, target_snapshot_hash,
            feature_snapshot_json, feature_dependency_fingerprint,
           feature_dependency_revision)
           VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, 'prospective', ?, 'team_one',
                   ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            curve_key,
            match_id,
            mapping_id,
            canonical_hash({"dire": dire, "radiant": radiant}),
            json.dumps(radiant),
            json.dumps(dire),
            observed_at.isoformat(),
            observed_at.isoformat(),
            observed_at.isoformat(),
            anchor_hash,
            observation.source_frame_ref,
            observed_at.isoformat(),
            observation.source_frame_ref,
            observed_at.isoformat(),
            deployment.deployment_key,
            target_hash,
            canonical_json_bytes(feature_artifact).decode(),
            feature_dependency_fingerprint,
            feature_dependency_revision,
        ),
    )
    samples: dict[int, CalibrationSample] = {}
    for horizon in HORIZONS:
        model = deployment.model(horizon)
        calibration = deployment.calibration(horizon)
        prediction = predict_draft(model, values)
        assert prediction.probability is not None
        assert prediction.uncertainty is not None
        probability = calibration.apply(prediction.probability)
        connection.execute(
            """INSERT INTO prospective_draft_landmarks
               (landmark_key, curve_key, horizon_minutes, radiant_probability,
                scaling_edge, synergy_edge, quality, validation_status,
                support, calibration_ref, input_refs_json, uncertainty,
                validation_reason, feature_hash, model_hash, calibration_hash,
                global_calibration_passed, global_gate_ref, model_version,
                model_kind, availability_mode, input_snapshot_hash, created_at,
                raw_radiant_probability, deployment_key, model_input_hash,
                raw_uncertainty)
               VALUES (?, ?, ?, ?, 0.0, 0.0, 1.0, 'failed', 0, ?, '["test"]',
                       ?, 'prospective_evidence_required', ?, ?, ?, 0, ?, ?,
                       'pure_draft', 'prospective', ?, ?, ?, ?, ?, ?)""",
            (
                canonical_hash({"curve": curve_key, "horizon": horizon}),
                curve_key,
                horizon,
                probability,
                f"draft-calibration:{calibration.calibration_hash}",
                prediction.uncertainty,
                model.feature_schema_hash,
                model.model_hash,
                calibration.calibration_hash,
                f"draft-calibration:{calibration.calibration_hash}",
                model.model_version,
                target_hash,
                observed_at.isoformat(),
                prediction.probability,
                deployment.deployment_key,
                prediction.input_snapshot_hash,
                prediction.uncertainty,
            ),
        )
        samples[horizon] = CalibrationSample(
            sample_id=f"{curve_key}:{horizon}",
            probability=prediction.probability,
            outcome=outcome,
            observed_at=observed_at,
            settled_at=settled_at,
            cluster_id=f"raybet:{match_id}",
            event_id="event-1",
        )
    connection.commit()
    winner = "team_one" if outcome else "team_two"
    team_one_kills = 30 if winner == "team_one" else 20
    team_two_kills = 20 if winner == "team_one" else 30
    raybet_payload = {
        "id": match_id,
        "game_id": 151,
        "team": [
            {"pos": 1, "team_id": 501},
            {"pos": 2, "team_id": 502},
        ],
        "odds": [
            {
                "odds_id": f"final-{index}-one",
                "odds_group_id": f"final-{index}",
                "match_stage": "r1",
                "group_short_name": "Winner",
                "tag": "win",
                "team_id": 501,
                "status": 5,
                "win": int(winner == "team_one"),
            },
            {
                "odds_id": f"final-{index}-two",
                "odds_group_id": f"final-{index}",
                "match_stage": "r1",
                "group_short_name": "Winner",
                "tag": "win",
                "team_id": 502,
                "status": 5,
                "win": int(winner == "team_two"),
            },
        ],
    }
    raybet_artifact = store.archive_response_payload(
        {"result": raybet_payload},
        observed_at=settled_at,
        match_id=match_id,
        response_kind="final_odds",
    )
    raybet_audit_key = store.record_direct_response_audit(
        raybet_artifact,
        response_kind="final_odds",
        claimed_raybet_match_id=match_id,
        observed_raybet_match_id=match_id,
        disposition="audit_only",
        reason="final_result_evidence",
    )
    raybet_final = parse_raybet_map_final(
        raybet_payload,
        1,
        observed_at=settled_at,
        expected_match_id=match_id,
        expected_team_ids=(501, 502),
    )
    opendota_payload = {
        "match_id": dota_match_id,
        "radiant_team_id": 101,
        "dire_team_id": 202,
        "radiant_win": winner == "team_one",
        "radiant_score": team_one_kills,
        "dire_score": team_two_kills,
        "duration": 2400,
    }
    opendota_receipt = opendota_archive.archive_json(
        source="opendota",
        endpoint=f"/api/matches/{dota_match_id}",
        request_identity=f"/api/matches/{dota_match_id}",
        payload_bytes=canonical_json_bytes(opendota_payload),
        observed_at=settled_at,
        match_id=dota_match_id,
        status_code=200,
        first_usable_at=settled_at,
    )
    identity = {
        "raybet_match_id": match_id,
        "map_number": 1,
        "strict_mapping_id": mapping_id,
        "dota_match_id": dota_match_id,
        "winner_side": winner,
    }
    opendota_ref = (
        f"opendota:{dota_match_id}:sha256:{opendota_receipt.content_sha256}"
    )
    reconciliation = store.record_settlement_reconciliation(
        raybet_match_id=match_id,
        map_number=1,
        strict_mapping_id=mapping_id,
        dota_match_id=dota_match_id,
        raybet_status="confirmed",
        raybet_winner_side=winner,
        opendota_winner_side=winner,
        raybet_evidence_ref=raybet_final.evidence_ref,
        opendota_evidence_ref=opendota_ref,
        raybet_facts={**identity, **raybet_final.facts()},
        opendota_facts={
            **identity,
            "team_one_kills": team_one_kills,
            "team_two_kills": team_two_kills,
            "duration_seconds": 2400,
        },
        status="confirmed",
        reason="sources_consistent",
        raybet_observed_at=settled_at,
        opendota_observed_at=settled_at,
        opendota_first_usable_at=settled_at,
        raybet_audit_key=raybet_audit_key,
        raybet_transport_key=None,
        raybet_response_state_hash=None,
        raybet_response_artifact_hash=raybet_artifact.content_sha256,
        opendota_artifact_id=f"opendota:{opendota_receipt.content_sha256}",
        opendota_observation_id=opendota_receipt.observation_id,
        opendota_content_hash=opendota_receipt.content_sha256,
    )
    assert str(reconciliation["status"]) == "confirmed", (
        reconciliation["status"],
        reconciliation["reason"],
    )
    map_result_ref = str(reconciliation["evidence_ref"])
    assert store.insert_map_result(
        SimpleNamespace(
            raybet_match_id=match_id,
            map_number=1,
            dota_match_id=dota_match_id,
            winner_side=winner,
            team_one_kills=team_one_kills,
            team_two_kills=team_two_kills,
            duration_seconds=2400,
            evidence_ref=map_result_ref,
            settled_at=settled_at,
        ),
        strict_mapping_id=mapping_id,
    )
    evidence_rows = connection.execute(
        """SELECT source, status, winner_side, evidence_ref, facts_json,
                  observed_at
             FROM settlement_result_evidence
            WHERE raybet_match_id=? AND map_number=1 AND dota_match_id=?
            ORDER BY source""",
        (match_id, dota_match_id),
    ).fetchall()
    assert len(evidence_rows) == 2
    radiant_win, evidence_hash = prospective_outcome_authority(
        curve_key=curve_key,
        dota_match_id=dota_match_id,
        winner_side=winner,
        radiant_team_side="team_one",
        map_result_ref=map_result_ref,
        reconciliation_observed_at=str(reconciliation["first_observed_at"]),
        evidence_rows=tuple(
            tuple(str(value) for value in row) for row in evidence_rows
        ),
    )
    connection.execute(
        """INSERT INTO prospective_draft_outcomes
           (curve_key, strict_mapping_id, dota_match_id, radiant_win,
            winner_side, evidence_ref, evidence_hash, settled_at,
            first_usable_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            curve_key,
            mapping_id,
            dota_match_id,
            radiant_win,
            winner,
            map_result_ref,
            evidence_hash,
            settled_at.isoformat(),
            settled_at.isoformat(),
            settled_at.isoformat(),
        ),
    )
    connection.commit()
    return samples


def test_frozen_deployment_round_trip_is_complete_and_immutable(
    prepared_database: Path,
) -> None:
    deployment = _deployment(prepared_database)
    with LiveBettingStore(prepared_database) as store:
        assert persist_frozen_deployment(
            store.connection,
            deployment,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        assert not persist_frozen_deployment(
            store.connection,
            deployment,
            created_at=CUTOFF + timedelta(seconds=2),
        )
        loaded = load_latest_frozen_deployment(store.connection)
        assert loaded == deployment
        assert store.connection.execute(
            "SELECT COUNT(*) FROM draft_model_artifacts"
        ).fetchone()[0] == 5
        assert store.connection.execute(
            "SELECT COUNT(*) FROM draft_calibration_artifacts"
        ).fetchone()[0] == 5
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store.connection.execute(
                "UPDATE draft_model_artifacts SET model_version='tampered'"
            )
        bundle = store.connection.execute(
            """SELECT deployment_key, model_hashes_json,
                      calibration_hashes_json, training_cutoff,
                      dependency_fingerprint, dependency_revision,
                      evidence_mode
                 FROM draft_deployment_bundles"""
        ).fetchone()
        assert bundle is not None
        store.connection.execute(
            "DROP TRIGGER draft_deployment_bundles_immutable_delete"
        )
        store.connection.execute("DELETE FROM draft_deployment_bundles")
        store.connection.execute(
            """INSERT INTO draft_deployment_bundles
               (deployment_key, model_hashes_json, calibration_hashes_json,
                training_cutoff, dependency_fingerprint,
                dependency_revision, evidence_mode, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (*tuple(bundle), (CUTOFF - timedelta(seconds=1)).isoformat()),
        )
        with pytest.raises(ValueError, match="created before"):
            load_latest_frozen_deployment(store.connection)


@pytest.mark.parametrize("old_key", ("", "A" * 64, "a" * 63))
def test_audited_rebase_requires_exact_lowercase_old_key_without_writes(
    prepared_database: Path,
    old_key: str,
) -> None:
    with LiveBettingStore(prepared_database) as store:
        before = store.connection.execute(
            "SELECT COUNT(*) FROM draft_deployment_bundles"
        ).fetchone()[0]

        with pytest.raises(ValueError, match="lowercase SHA-256"):
            draft_publisher_module.audited_rebase_frozen_deployment(
                store.connection,
                old_deployment_key=old_key,
                created_at=CUTOFF + timedelta(seconds=1),
            )

        assert not store.connection.in_transaction
        assert store.connection.execute(
            "SELECT COUNT(*) FROM draft_deployment_bundles"
        ).fetchone()[0] == before


def test_audited_rebase_rejects_model_replay_mismatch_without_writes(
    prepared_database: Path,
) -> None:
    original = _deployment(prepared_database)
    forged = _forged_corpus_deployment(original, horizon_minutes=10)
    with LiveBettingStore(prepared_database) as store:
        persist_frozen_deployment(
            store.connection,
            original,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        _insert_deployment_without_replay(
            store.connection,
            forged,
            created_at=CUTOFF + timedelta(seconds=2),
        )
        before = (
            store.connection.execute(
                "SELECT COUNT(*) FROM draft_deployment_bundles"
            ).fetchone()[0],
            store.connection.execute(
                "SELECT COUNT(*) FROM draft_model_artifacts"
            ).fetchone()[0],
            store.connection.execute(
                "SELECT COUNT(*) FROM draft_calibration_artifacts"
            ).fetchone()[0],
            store.connection.execute(
                "SELECT artifact_revision FROM draft_deployment_revisions "
                "WHERE singleton=1"
            ).fetchone()[0],
        )

        with pytest.raises(ValueError, match="authoritative database corpus"):
            draft_publisher_module.audited_rebase_frozen_deployment(
                store.connection,
                old_deployment_key=forged.deployment_key,
                created_at=CUTOFF + timedelta(seconds=3),
            )

        after = (
            store.connection.execute(
                "SELECT COUNT(*) FROM draft_deployment_bundles"
            ).fetchone()[0],
            store.connection.execute(
                "SELECT COUNT(*) FROM draft_model_artifacts"
            ).fetchone()[0],
            store.connection.execute(
                "SELECT COUNT(*) FROM draft_calibration_artifacts"
            ).fetchone()[0],
            store.connection.execute(
                "SELECT artifact_revision FROM draft_deployment_revisions "
                "WHERE singleton=1"
            ).fetchone()[0],
        )
        assert after == before
        assert not store.connection.in_transaction


def test_audited_rebase_reuses_exact_artifacts_for_stale_old_lineage(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _deployment(prepared_database)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("audited rebase entered a deployment build path")

    monkeypatch.setattr(
        draft_publisher_module,
        "build_frozen_draft_deployment",
        forbidden,
    )
    monkeypatch.setattr(draft_publisher_module, "_build_and_persist", forbidden)
    with LiveBettingStore(prepared_database) as store:
        persist_frozen_deployment(
            store.connection,
            original,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        old_fingerprint = original.dependency_fingerprint
        old_revision = original.dependency_revision
        _insert_future_event(store.connection)
        _record_dependency_change(
            store.connection,
            affected_from=CUTOFF - timedelta(seconds=1),
        )
        current_fingerprint = draft_dependency_fingerprint(store.connection)
        current_revision = int(
            store.connection.execute(
                "SELECT dependency_revision FROM draft_lineage_revisions "
                "WHERE singleton=1"
            ).fetchone()[0]
        )
        assert current_fingerprint != old_fingerprint
        assert current_revision > old_revision
        with pytest.raises(
            ValueError,
            match="draft_dependencies_changed_before_cutoff",
        ):
            load_frozen_deployment(
                store.connection,
                deployment_key=original.deployment_key,
            )

        rebased, inserted = (
            draft_publisher_module.audited_rebase_frozen_deployment(
                store.connection,
                old_deployment_key=original.deployment_key,
                created_at=CUTOFF + timedelta(days=20),
            )
        )

        assert inserted
        assert rebased.deployment_key != original.deployment_key
        assert rebased.training_cutoff == original.training_cutoff
        assert rebased.dependency_fingerprint == current_fingerprint
        assert rebased.dependency_revision == current_revision
        assert {
            row.horizon_minutes: row.model_hash for row in rebased.models
        } == {
            row.horizon_minutes: row.model_hash for row in original.models
        }
        assert {
            row.horizon_minutes: row.calibration_hash
            for row in rebased.calibrations
        } == {
            row.horizon_minutes: row.calibration_hash
            for row in original.calibrations
        }
        assert load_pinned_frozen_deployment(
            store.connection,
            deployment_key=rebased.deployment_key,
        ) == rebased

        before = (
            store.connection.execute(
                "SELECT COUNT(*) FROM draft_deployment_bundles"
            ).fetchone()[0],
            store.connection.execute(
                "SELECT COUNT(*) FROM draft_model_artifacts"
            ).fetchone()[0],
            store.connection.execute(
                "SELECT COUNT(*) FROM draft_calibration_artifacts"
            ).fetchone()[0],
            store.connection.execute(
                "SELECT artifact_revision FROM draft_deployment_revisions "
                "WHERE singleton=1"
            ).fetchone()[0],
        )
        repeated, repeated_inserted = (
            draft_publisher_module.audited_rebase_frozen_deployment(
                store.connection,
                old_deployment_key=original.deployment_key,
                created_at=CUTOFF + timedelta(days=20, seconds=1),
            )
        )
        after = (
            store.connection.execute(
                "SELECT COUNT(*) FROM draft_deployment_bundles"
            ).fetchone()[0],
            store.connection.execute(
                "SELECT COUNT(*) FROM draft_model_artifacts"
            ).fetchone()[0],
            store.connection.execute(
                "SELECT COUNT(*) FROM draft_calibration_artifacts"
            ).fetchone()[0],
            store.connection.execute(
                "SELECT artifact_revision FROM draft_deployment_revisions "
                "WHERE singleton=1"
            ).fetchone()[0],
        )
        assert repeated == rebased
        assert not repeated_inserted
        assert after == before


def test_audited_rebase_cli_prints_exact_keys_and_supervisor_pin(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    old_key = "a" * 64
    new_key = "b" * 64
    calls: list[tuple[str, datetime]] = []

    def rebase(
        _connection: sqlite3.Connection,
        *,
        old_deployment_key: str,
        created_at: datetime,
    ) -> tuple[SimpleNamespace, bool]:
        calls.append((old_deployment_key, created_at))
        return SimpleNamespace(deployment_key=new_key), True

    monkeypatch.setattr(
        draft_publisher_module,
        "audited_rebase_frozen_deployment",
        rebase,
    )

    result = draft_publisher_module._run_publisher_locked(
        prepared_database,
        once=True,
        interval_seconds=0.01,
        rebuild_artifacts=False,
        rebase_deployment_key=old_key,
    )

    assert result == 0
    assert len(calls) == 1
    assert calls[0][0] == old_key
    assert calls[0][1].tzinfo is not None
    assert json.loads(capsys.readouterr().out) == {
        "status": "ok",
        "phase": "audited_rebase_complete",
        "old_deployment_key": old_key,
        "new_deployment_key": new_key,
        "inserted": True,
        "supervisor_argument": "--draft-deployment-key " + new_key,
    }


@pytest.mark.parametrize("old_key", ("invalid", "A" * 64))
def test_audited_rebase_cli_rejects_invalid_old_key_before_database_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    old_key: str,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "draft_publisher.py",
            "--database",
            str(tmp_path / "missing.db"),
            "--once",
            "--rebase-deployment-key",
            old_key,
        ],
    )

    with pytest.raises(SystemExit):
        draft_publisher_module.main()


@pytest.mark.parametrize("horizon_minutes", (40, 50))
def test_persist_rejects_resigned_forged_training_corpus_for_long_horizon(
    prepared_database: Path,
    horizon_minutes: int,
) -> None:
    forged = _forged_corpus_deployment(
        _deployment(prepared_database),
        horizon_minutes=horizon_minutes,
    )
    with LiveBettingStore(prepared_database) as store:
        with pytest.raises(ValueError, match="authoritative database corpus"):
            persist_frozen_deployment(
                store.connection,
                forged,
                created_at=CUTOFF + timedelta(seconds=1),
            )
        assert store.connection.execute(
            "SELECT COUNT(*) FROM draft_deployment_bundles"
        ).fetchone()[0] == 0


def test_specific_loader_rejects_self_consistent_forged_corpus_bundle(
    prepared_database: Path,
) -> None:
    deployment = _deployment(prepared_database)
    forged = _forged_corpus_deployment(deployment, horizon_minutes=10)
    with LiveBettingStore(prepared_database) as store:
        persist_frozen_deployment(
            store.connection,
            deployment,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        _insert_deployment_without_replay(
            store.connection,
            forged,
            created_at=CUTOFF + timedelta(seconds=2),
        )

        assert load_frozen_deployment(
            store.connection,
            deployment_key=deployment.deployment_key,
        ) == deployment
        with pytest.raises(ValueError, match="authoritative database corpus"):
            load_frozen_deployment(
                store.connection,
                deployment_key=forged.deployment_key,
            )


def test_runtime_pinned_loader_ignores_forged_latest_without_database_replay(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = _deployment(prepared_database)
    forged = _forged_corpus_deployment(deployment, horizon_minutes=10)
    with LiveBettingStore(prepared_database) as store:
        persist_frozen_deployment(
            store.connection,
            deployment,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        _insert_deployment_without_replay(
            store.connection,
            forged,
            created_at=CUTOFF + timedelta(seconds=2),
        )
        monkeypatch.setattr(
            draft_publisher_module,
            "assert_draft_models_match_database",
            lambda *_args, **_kwargs: pytest.fail(
                "runtime pinned loader replayed the database corpus"
            ),
        )

        loaded = load_pinned_frozen_deployment(
            store.connection,
            deployment_key=deployment.deployment_key,
        )

    assert loaded == deployment


def test_runtime_pinned_loader_rejects_missing_or_invalid_pin(
    prepared_database: Path,
) -> None:
    deployment = _deployment(prepared_database)
    with LiveBettingStore(prepared_database) as store:
        persist_frozen_deployment(
            store.connection,
            deployment,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        assert load_pinned_frozen_deployment(
            store.connection,
            deployment_key="0" * 64,
        ) is None
        with pytest.raises(ValueError, match="lowercase SHA-256"):
            load_pinned_frozen_deployment(
                store.connection,
                deployment_key=deployment.deployment_key.upper(),
            )


@pytest.mark.parametrize(
    ("limit_name", "expected"),
    [
        ("MAX_RUNTIME_ARTIFACT_BYTES", "runtime size limit"),
        ("MAX_RUNTIME_ARTIFACT_ROWS", "runtime row limit"),
    ],
)
def test_runtime_pinned_loader_enforces_artifact_limits(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    expected: str,
) -> None:
    deployment = _deployment(prepared_database)
    with LiveBettingStore(prepared_database) as store:
        persist_frozen_deployment(
            store.connection,
            deployment,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        monkeypatch.setattr(draft_publisher_module, limit_name, 1)
        with pytest.raises(ValueError, match=expected):
            load_pinned_frozen_deployment(
                store.connection,
                deployment_key=deployment.deployment_key,
            )


def test_runtime_pinned_loader_rejects_pre_cutoff_lineage_change(
    prepared_database: Path,
) -> None:
    deployment = _deployment(prepared_database)
    with LiveBettingStore(prepared_database) as store:
        persist_frozen_deployment(
            store.connection,
            deployment,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        _record_dependency_change(
            store.connection,
            affected_from=CUTOFF - timedelta(seconds=1),
        )
        with pytest.raises(
            ValueError,
            match="draft_dependencies_changed_before_cutoff",
        ):
            load_pinned_frozen_deployment(
                store.connection,
                deployment_key=deployment.deployment_key,
            )


@pytest.mark.parametrize(
    "changed_query",
    (
        "SELECT artifact_revision FROM draft_deployment_revisions",
        "SELECT dependency_revision FROM draft_lineage_revisions",
    ),
)
def test_runtime_pinned_loader_rejects_generation_race(
    prepared_database: Path,
    changed_query: str,
) -> None:
    deployment = _deployment(prepared_database)
    with LiveBettingStore(prepared_database) as store:
        persist_frozen_deployment(
            store.connection,
            deployment,
            created_at=CUTOFF + timedelta(seconds=1),
        )

        class RacingConnection:
            def __init__(self, connection: sqlite3.Connection) -> None:
                self.connection = connection

            def __getattr__(self, name: str):
                return getattr(self.connection, name)

            @property
            def in_transaction(self) -> bool:
                return self.connection.in_transaction

            def execute(self, sql: str, params: tuple[object, ...] = ()):
                if (
                    changed_query in sql
                    and not self.connection.in_transaction
                ):
                    return SimpleNamespace(fetchone=lambda: (999,))
                return self.connection.execute(sql, params)

        with pytest.raises(ValueError, match="changed during runtime load"):
            load_pinned_frozen_deployment(
                RacingConnection(store.connection),  # type: ignore[arg-type]
                deployment_key=deployment.deployment_key,
            )


def test_runtime_pinned_loader_accepts_unrelated_database_commit(
    prepared_database: Path,
) -> None:
    deployment = _deployment(prepared_database)
    with LiveBettingStore(prepared_database) as store:
        persist_frozen_deployment(
            store.connection,
            deployment,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        store.connection.execute(
            "CREATE TABLE runtime_loader_unrelated_write (marker INTEGER)"
        )
        store.connection.commit()

        class RacingConnection:
            def __init__(self, connection: sqlite3.Connection) -> None:
                self.connection = connection
                self.committed_unrelated_write = False

            def __getattr__(self, name: str):
                return getattr(self.connection, name)

            @property
            def in_transaction(self) -> bool:
                return self.connection.in_transaction

            def commit(self) -> None:
                self.connection.commit()
                if self.committed_unrelated_write:
                    return
                self.committed_unrelated_write = True
                writer = sqlite3.connect(prepared_database)
                try:
                    writer.execute(
                        "INSERT INTO runtime_loader_unrelated_write VALUES (1)"
                    )
                    writer.commit()
                finally:
                    writer.close()

        assert (
            load_pinned_frozen_deployment(
                RacingConnection(store.connection),  # type: ignore[arg-type]
                deployment_key=deployment.deployment_key,
            )
            == deployment
        )
        assert store.connection.execute(
            "SELECT COUNT(*) FROM runtime_loader_unrelated_write"
        ).fetchone()[0] == 1


def test_live_curve_rejects_self_consistent_forged_corpus_bundle(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = _deployment(prepared_database)
    forged = _forged_corpus_deployment(deployment, horizon_minutes=10)
    monkeypatch.setattr(
        "live_betting.draft_publisher.query_strict_live_eligibility",
        lambda *args, **kwargs: _strict_result(),
    )
    monkeypatch.setattr(
        "live_betting.profiles.draft_curve.query_strict_live_eligibility",
        lambda *args, **kwargs: _strict_result(),
    )
    draft_curve_profile._cached_live_frozen_deployment.cache_clear()
    with LiveBettingStore(prepared_database) as store:
        persist_frozen_deployment(
            store.connection,
            deployment,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        _insert_deployment_without_replay(
            store.connection,
            forged,
            created_at=CUTOFF + timedelta(seconds=2),
        )
        _insert_anchor(store, captured_at=CUTOFF + timedelta(seconds=3))
        history = load_prospective_history(store.connection)
        report = publish_cycle(
            store.connection,
            deployment=forged,
            history=_runtime_history(
                forged.dependency_revision,
                forged.dependency_fingerprint,
                history,
            ),
            now=CUTOFF + timedelta(seconds=4),
        )
        assert report.inserted == 1

        curve = build_draft_curve(
            store.connection,
            (1, 2, 3, 4, 5),
            (6, 7, 8, 9, 10),
            int((CUTOFF + timedelta(seconds=5)).timestamp()),
            raybet_match_id="match-1",
            map_number=1,
            strict_mapping_id=7,
        )
        assert curve.points == ()
        assert curve.unavailable_reason == "prospective_draft_artifact_invalid"


def test_legacy_model_is_rejected_by_persist_sql_load_and_live_curve(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = _deployment(prepared_database)
    legacy = _legacy_audit_only_deployment(current)
    monkeypatch.setattr(
        "live_betting.draft_publisher.query_strict_live_eligibility",
        lambda *args, **kwargs: _strict_result(),
    )
    monkeypatch.setattr(
        "live_betting.profiles.draft_curve.query_strict_live_eligibility",
        lambda *args, **kwargs: _strict_result(),
    )
    draft_curve_profile._cached_live_frozen_deployment.cache_clear()
    with LiveBettingStore(prepared_database) as store:
        with pytest.raises(ValueError, match="audit-only"):
            persist_frozen_deployment(
                store.connection,
                legacy,
                created_at=CUTOFF + timedelta(seconds=1),
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="draft deployment bundle authority is required",
        ):
            _insert_deployment_without_replay(
                store.connection,
                legacy,
                created_at=CUTOFF + timedelta(seconds=1),
            )
        store.connection.rollback()
        store.connection.execute(
            "DROP TRIGGER draft_deployment_bundle_authority_insert"
        )
        _insert_deployment_without_replay(
            store.connection,
            legacy,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        with pytest.raises(ValueError, match="audit-only"):
            load_frozen_deployment(
                store.connection,
                deployment_key=legacy.deployment_key,
            )

        _insert_anchor(store, captured_at=CUTOFF + timedelta(seconds=2))
        history = load_prospective_history(store.connection)
        report = publish_cycle(
            store.connection,
            deployment=legacy,
            history=_runtime_history(
                legacy.dependency_revision,
                legacy.dependency_fingerprint,
                history,
            ),
            now=CUTOFF + timedelta(seconds=3),
        )
        assert report.inserted == 1
        curve = build_draft_curve(
            store.connection,
            (1, 2, 3, 4, 5),
            (6, 7, 8, 9, 10),
            int((CUTOFF + timedelta(seconds=4)).timestamp()),
            raybet_match_id="match-1",
            map_number=1,
            strict_mapping_id=7,
        )
        assert curve.points == ()
        assert curve.unavailable_reason == "prospective_draft_artifact_invalid"


def test_live_deployment_cache_ignores_vision_and_future_dependency_changes(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = _deployment(prepared_database)
    with LiveBettingStore(prepared_database) as store:
        persist_frozen_deployment(
            store.connection,
            deployment,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        draft_curve_profile._cached_live_frozen_deployment.cache_clear()
        real_loader = draft_curve_profile.load_frozen_deployment
        calls = 0

        def counted_loader(*args, **kwargs):
            nonlocal calls
            calls += 1
            return real_loader(*args, **kwargs)

        monkeypatch.setattr(
            draft_curve_profile,
            "load_frozen_deployment",
            counted_loader,
        )
        assert draft_curve_profile._live_frozen_deployment(
            store.connection,
            deployment.deployment_key,
        ) == deployment
        assert calls == 1

        _insert_anchor(store, captured_at=CUTOFF + timedelta(seconds=2))
        assert draft_curve_profile._live_frozen_deployment(
            store.connection,
            deployment.deployment_key,
        ) == deployment
        assert calls == 1

        _record_dependency_change(
            store.connection,
            affected_from=CUTOFF + timedelta(days=1),
        )
        assert draft_curve_profile._live_frozen_deployment(
            store.connection,
            deployment.deployment_key,
        ) == deployment
        assert calls == 1

        _record_dependency_change(
            store.connection,
            affected_from=CUTOFF - timedelta(seconds=1),
        )
        with pytest.raises(ValueError, match="changed_before_cutoff"):
            draft_curve_profile._live_frozen_deployment(
                store.connection,
                deployment.deployment_key,
            )
        assert calls == 2


def test_live_deployment_cache_does_not_hide_artifact_tamper(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = _deployment(prepared_database)
    with LiveBettingStore(prepared_database) as store:
        persist_frozen_deployment(
            store.connection,
            deployment,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        draft_curve_profile._cached_live_frozen_deployment.cache_clear()
        real_loader = draft_curve_profile.load_frozen_deployment
        calls = 0

        def counted_loader(*args, **kwargs):
            nonlocal calls
            calls += 1
            return real_loader(*args, **kwargs)

        monkeypatch.setattr(
            draft_curve_profile,
            "load_frozen_deployment",
            counted_loader,
        )
        assert draft_curve_profile._live_frozen_deployment(
            store.connection,
            deployment.deployment_key,
        ) == deployment
        assert calls == 1
        store.connection.execute(
            "DROP TRIGGER draft_model_artifacts_immutable_update"
        )
        store.connection.execute(
            """UPDATE draft_model_artifacts
                  SET artifact_json=artifact_json || ' '
                WHERE horizon_minutes=40"""
        )
        store.connection.commit()

        with pytest.raises(ValueError, match="canonical JSON"):
            draft_curve_profile._live_frozen_deployment(
                store.connection,
                deployment.deployment_key,
            )
        assert calls == 2


def test_live_deployment_loader_detects_toctou_artifact_change(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = _deployment(prepared_database)
    with LiveBettingStore(prepared_database) as store:
        persist_frozen_deployment(
            store.connection,
            deployment,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        store.connection.execute(
            "DROP TRIGGER draft_model_artifacts_immutable_update"
        )
        store.connection.commit()
        draft_curve_profile._cached_live_frozen_deployment.cache_clear()
        real_loader = draft_curve_profile.load_frozen_deployment
        raced = False

        def racing_loader(*args, **kwargs):
            nonlocal raced
            loaded = real_loader(*args, **kwargs)
            if not raced:
                raced = True
                writer = connect(prepared_database)
                try:
                    writer.execute(
                        """UPDATE draft_model_artifacts
                              SET artifact_json=artifact_json || ' '
                            WHERE horizon_minutes=50"""
                    )
                    writer.commit()
                finally:
                    writer.close()
            return loaded

        monkeypatch.setattr(
            draft_curve_profile,
            "load_frozen_deployment",
            racing_loader,
        )
        with pytest.raises(ValueError, match="authority changed"):
            draft_curve_profile._live_frozen_deployment(
                store.connection,
                deployment.deployment_key,
            )
        assert raced


def test_bundle_loader_rejects_duplicate_keys_and_nonfinite_json(
    prepared_database: Path,
) -> None:
    deployment = _deployment(prepared_database)
    with LiveBettingStore(prepared_database) as store:
        persist_frozen_deployment(
            store.connection,
            deployment,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        raw = str(
            store.connection.execute(
                """SELECT model_hashes_json FROM draft_deployment_bundles
                    WHERE deployment_key=?""",
                (deployment.deployment_key,),
            ).fetchone()[0]
        )
        duplicate = '{"10":"' + deployment.model(10).model_hash + '",' + raw[1:]
        store.connection.execute(
            "DROP TRIGGER draft_deployment_bundles_immutable_update"
        )
        store.connection.execute(
            """UPDATE draft_deployment_bundles SET model_hashes_json=?
                WHERE deployment_key=?""",
            (duplicate, deployment.deployment_key),
        )
        store.connection.commit()

        with pytest.raises(ValueError, match="duplicate JSON key"):
            load_frozen_deployment(
                store.connection,
                deployment_key=deployment.deployment_key,
            )
    with pytest.raises(ValueError, match="invalid JSON constant"):
        draft_publisher_module._hash_map(
            '{"10":NaN,"20":"' + "a" * 64 + '"}',
            "model_hashes_json",
        )


def test_publisher_rejects_caller_supplied_history_subset(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = _deployment(prepared_database)
    monkeypatch.setattr(
        "live_betting.draft_publisher.query_strict_live_eligibility",
        lambda *args, **kwargs: _strict_result(),
    )
    with LiveBettingStore(prepared_database) as store:
        persist_frozen_deployment(
            store.connection,
            deployment,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        _insert_anchor(store, captured_at=CUTOFF + timedelta(seconds=2))
        result = publish_cycle(
            store.connection,
            deployment=deployment,
            history=ProspectiveHistorySnapshot(
                deployment.dependency_revision,
                deployment.dependency_fingerprint,
                (),
            ),
            now=CUTOFF + timedelta(seconds=3),
        )
        assert result.inserted == 0
        assert result.results[0].reason == "draft_history:runtime_snapshot_untrusted"
        assert store.connection.execute(
            "SELECT COUNT(*) FROM prospective_draft_curves"
        ).fetchone()[0] == 0


def test_failed_reconstructed_gate_publishes_research_curve_but_no_order(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = _deployment(prepared_database)
    monkeypatch.setattr(
        "live_betting.draft_publisher.query_strict_live_eligibility",
        lambda *args, **kwargs: _strict_result(),
    )
    monkeypatch.setattr(
        "live_betting.profiles.draft_curve.query_strict_live_eligibility",
        lambda *args, **kwargs: _strict_result(),
    )
    with LiveBettingStore(prepared_database) as store:
        persist_frozen_deployment(
            store.connection,
            deployment,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        _insert_anchor(store, captured_at=CUTOFF + timedelta(seconds=2))
        history = _runtime_history(
            dependency_revision=deployment.dependency_revision,
            dependency_fingerprint=deployment.dependency_fingerprint,
            maps=load_prospective_history(store.connection),
        )
        with monkeypatch.context() as cycle_guard:
            cycle_guard.setattr(
                backtest_module,
                "draft_dependency_fingerprint",
                lambda *_args, **_kwargs: pytest.fail(
                    "runtime cycle recalculated the full dependency fingerprint"
                ),
            )
            cycle_guard.setattr(
                backtest_module,
                "load_draft_corpus",
                lambda *_args, **_kwargs: pytest.fail(
                    "runtime cycle reloaded the full draft corpus"
                ),
            )
            first = publish_cycle(
                store.connection,
                deployment=deployment,
                history=history,
                now=CUTOFF + timedelta(seconds=3),
            )
            second = publish_cycle(
                store.connection,
                deployment=deployment,
                history=history,
                now=CUTOFF + timedelta(seconds=4),
            )

        assert (first.inserted, first.unchanged, first.skipped) == (1, 0, 0)
        assert (second.inserted, second.unchanged, second.skipped) == (0, 1, 0)
        curve = store.connection.execute(
            "SELECT * FROM prospective_draft_curves"
        ).fetchone()
        assert curve["deployment_key"] == deployment.deployment_key
        assert curve["radiant_team_side"] == "team_one"
        landmarks = store.connection.execute(
            """SELECT horizon_minutes, validation_status,
                      global_calibration_passed,
                      raw_radiant_probability, radiant_probability,
                      validation_reason
                 FROM prospective_draft_landmarks
                ORDER BY horizon_minutes"""
        ).fetchall()
        assert [int(row[0]) for row in landmarks] == list(HORIZONS)
        assert all(row[1] == "failed" and int(row[2]) == 0 for row in landmarks)
        assert all(row[3] is not None and row[4] is not None for row in landmarks)
        assert all("prospective_evidence_required" in row[5] for row in landmarks)
        assert store.connection.execute(
            "SELECT COUNT(*) FROM shadow_orders"
        ).fetchone()[0] == 0
        loaded_curve = build_draft_curve(
            store.connection,
            (1, 2, 3, 4, 5),
            (6, 7, 8, 9, 10),
            int((CUTOFF + timedelta(seconds=5)).timestamp()),
            raybet_match_id="match-1",
            map_number=1,
            strict_mapping_id=7,
        )
        assert len(loaded_curve.points) == 5
        assert loaded_curve.at(10 * 60) is None
        assert (
            loaded_curve.unavailable_reason
            == "prospective_draft_calibration_gate_not_passed"
        )

        store.connection.execute(
            "DROP TRIGGER prospective_draft_landmarks_immutable_update"
        )
        original_raw_probability = float(
            store.connection.execute(
                """SELECT raw_radiant_probability
                     FROM prospective_draft_landmarks
                    WHERE horizon_minutes=10"""
            ).fetchone()[0]
        )
        store.connection.execute(
            """UPDATE prospective_draft_landmarks
                  SET raw_radiant_probability=raw_radiant_probability + 0.01
                WHERE horizon_minutes=10"""
        )
        corrupted = build_draft_curve(
            store.connection,
            (1, 2, 3, 4, 5),
            (6, 7, 8, 9, 10),
            int((CUTOFF + timedelta(seconds=5)).timestamp()),
            raybet_match_id="match-1",
            map_number=1,
            strict_mapping_id=7,
        )
        assert corrupted.unavailable_reason == "prospective_draft_artifact_invalid"
        store.connection.execute(
            """UPDATE prospective_draft_landmarks
                  SET raw_radiant_probability=?
                WHERE horizon_minutes=10""",
            (original_raw_probability,),
        )
        restored = build_draft_curve(
            store.connection,
            (1, 2, 3, 4, 5),
            (6, 7, 8, 9, 10),
            int((CUTOFF + timedelta(seconds=5)).timestamp()),
            raybet_match_id="match-1",
            map_number=1,
            strict_mapping_id=7,
        )
        assert len(restored.points) == 5

        store.connection.execute(
            "DROP TRIGGER prospective_draft_curves_immutable_update"
        )
        feature_payload = json.loads(str(curve["feature_snapshot_json"]))
        assert feature_payload["artifact_version"] == DRAFT_FEATURE_ARTIFACT_VERSION
        assert "authority" not in feature_payload
        assert "eligible_history" not in feature_payload

        legacy_payload = dict(feature_payload)
        legacy_payload["artifact_version"] = "draft-feature-artifact-v1"
        store.connection.execute(
            """UPDATE prospective_draft_curves
                  SET feature_snapshot_json=?
                WHERE curve_key=?""",
            (
                canonical_json_bytes(legacy_payload).decode(),
                str(curve["curve_key"]),
            ),
        )
        legacy = build_draft_curve(
            store.connection,
            (1, 2, 3, 4, 5),
            (6, 7, 8, 9, 10),
            int((CUTOFF + timedelta(seconds=5)).timestamp()),
            raybet_match_id="match-1",
            map_number=1,
            strict_mapping_id=7,
        )
        assert legacy.unavailable_reason == "prospective_draft_artifact_invalid"

        anchor = ready_draft_anchors(store.connection)[0]
        mapping = _strict_result().mapping
        target = build_live_draft_target(
            store.connection,
            anchor,
            mapping,
            datetime.fromisoformat(str(curve["prediction_cutoff"])),
        )
        forged_snapshot, forged_payload = build_draft_feature_artifact(
            replace(target, event_id="forged-event"),
            load_prospective_history(store.connection),
        )
        store.connection.execute(
            """UPDATE prospective_draft_curves
                  SET feature_snapshot_json=?, target_snapshot_hash=?
                WHERE curve_key=?""",
            (
                canonical_json_bytes(forged_payload).decode(),
                forged_snapshot.input_hash,
                str(curve["curve_key"]),
            ),
        )
        store.connection.execute(
            """UPDATE prospective_draft_landmarks
                  SET input_snapshot_hash=?
                WHERE curve_key=?""",
            (forged_snapshot.input_hash, str(curve["curve_key"])),
        )
        forged = build_draft_curve(
            store.connection,
            (1, 2, 3, 4, 5),
            (6, 7, 8, 9, 10),
            int((CUTOFF + timedelta(seconds=5)).timestamp()),
            raybet_match_id="match-1",
            map_number=1,
            strict_mapping_id=7,
        )
        assert forged.unavailable_reason == "prospective_draft_artifact_invalid"
        store.connection.execute(
            """UPDATE prospective_draft_curves
                  SET feature_snapshot_json=?, target_snapshot_hash=?
                WHERE curve_key=?""",
            (
                str(curve["feature_snapshot_json"]),
                str(curve["target_snapshot_hash"]),
                str(curve["curve_key"]),
            ),
        )
        store.connection.execute(
            """UPDATE prospective_draft_landmarks
                  SET input_snapshot_hash=?
                WHERE curve_key=?""",
            (str(curve["target_snapshot_hash"]), str(curve["curve_key"])),
        )
        restored = build_draft_curve(
            store.connection,
            (1, 2, 3, 4, 5),
            (6, 7, 8, 9, 10),
            int((CUTOFF + timedelta(seconds=5)).timestamp()),
            raybet_match_id="match-1",
            map_number=1,
            strict_mapping_id=7,
        )
        assert len(restored.points) == 5

        store.connection.execute(
            """INSERT INTO vision_observation_invalidations
               (raybet_match_id, captured_at, source_frame_ref,
                invalidated_at, reason)
               VALUES ('match-1', ?, ?, ?, 'test_invalidation')""",
            (
                anchor.anchored_at.isoformat(),
                anchor.source_frame_ref,
                (CUTOFF + timedelta(seconds=6)).isoformat(),
            ),
        )
        invalidated = build_draft_curve(
            store.connection,
            (1, 2, 3, 4, 5),
            (6, 7, 8, 9, 10),
            int((CUTOFF + timedelta(seconds=7)).timestamp()),
            raybet_match_id="match-1",
            map_number=1,
            strict_mapping_id=7,
        )
        assert invalidated.unavailable_reason == "prospective_draft_artifact_invalid"


def test_delayed_publication_keeps_anchor_event_cutoff(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor_at = CUTOFF + timedelta(seconds=2)
    monkeypatch.setattr(
        "live_betting.draft_publisher.query_strict_live_eligibility",
        lambda *args, **kwargs: _strict_result(),
    )
    with LiveBettingStore(prepared_database) as store:
        _insert_event(store.connection)
        _insert_prospective_history(store.connection)
        delayed_usable = anchor_at + timedelta(milliseconds=500)
        store.connection.execute(
            """UPDATE raw_source_artifacts SET first_usable_at=?
                WHERE match_id=7000000""",
            (delayed_usable.isoformat(),),
        )
        store.connection.execute(
            """UPDATE player_map_facts SET first_usable_at=?
                WHERE match_id=7000000""",
            (delayed_usable.isoformat(),),
        )
        store.connection.commit()
        deployment = _deployment(prepared_database, min_samples=19)
        persist_frozen_deployment(
            store.connection,
            deployment,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        _insert_anchor(store, captured_at=anchor_at)
        history = load_prospective_history(store.connection)
        assert any(row.first_usable_at == delayed_usable for row in history)

        report = publish_cycle(
            store.connection,
            deployment=deployment,
            history=_runtime_history(
                deployment.dependency_revision,
                deployment.dependency_fingerprint,
                history,
            ),
            now=anchor_at + timedelta(seconds=1),
        )

        assert report.inserted == 1
        row = store.connection.execute(
            """SELECT prediction_cutoff, first_usable_at, feature_snapshot_json
                 FROM prospective_draft_curves"""
        ).fetchone()
        payload = json.loads(str(row[2]))
        assert row[0] == anchor_at.isoformat()
        assert row[1] == (anchor_at + timedelta(seconds=1)).isoformat()
        assert payload["support"] == len(history) - 1


def test_deployment_created_after_anchor_cutoff_is_rejected(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = _deployment(prepared_database)
    anchor_at = CUTOFF + timedelta(seconds=2)
    monkeypatch.setattr(
        "live_betting.draft_publisher.query_strict_live_eligibility",
        lambda *args, **kwargs: _strict_result(),
    )
    with LiveBettingStore(prepared_database) as store:
        persist_frozen_deployment(
            store.connection,
            deployment,
            created_at=anchor_at + timedelta(seconds=1),
        )
        _insert_anchor(store, captured_at=anchor_at)
        anchor = ready_draft_anchors(store.connection)[0]

        result = publish_anchor_curve(
            store.connection,
            anchor=anchor,
            deployment=deployment,
            history=_runtime_history(
                deployment.dependency_revision,
                deployment.dependency_fingerprint,
                load_prospective_history(store.connection),
            ),
            published_at=anchor_at + timedelta(seconds=2),
        )

        assert result.status == "skipped"
        assert result.reason == (
            "draft_deployment:deployment_not_available_at_cutoff"
        )
        assert store.connection.execute(
            "SELECT COUNT(*) FROM prospective_draft_curves"
        ).fetchone()[0] == 0


def test_mapping_accepted_after_anchor_cutoff_is_not_backfilled(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = _deployment(prepared_database)
    anchor_at = CUTOFF + timedelta(seconds=2)
    mapping_available_at = anchor_at + timedelta(seconds=1)
    observed: list[datetime] = []

    def strict(*args, transport_observed_at: datetime, **kwargs):
        observed.append(transport_observed_at)
        if transport_observed_at < mapping_available_at:
            return SimpleNamespace(
                eligible=False,
                reason="mapping_not_yet_recorded",
                mapping=None,
            )
        return _strict_result()

    monkeypatch.setattr(
        "live_betting.draft_publisher.query_strict_live_eligibility", strict
    )
    with LiveBettingStore(prepared_database) as store:
        persist_frozen_deployment(
            store.connection,
            deployment,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        _insert_anchor(store, captured_at=anchor_at)
        anchor = ready_draft_anchors(store.connection)[0]

        result = publish_anchor_curve(
            store.connection,
            anchor=anchor,
            deployment=deployment,
            history=_runtime_history(
                deployment.dependency_revision,
                deployment.dependency_fingerprint,
                load_prospective_history(store.connection),
            ),
            published_at=anchor_at + timedelta(seconds=2),
        )

        assert result.status == "skipped"
        assert result.reason == "strict_mapping:mapping_not_yet_recorded"
        assert observed == [anchor_at]


def test_patch_requires_pre_cutoff_ingest_authority() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(
            """CREATE TABLE matches (
                   match_id INTEGER PRIMARY KEY, patch INTEGER, start_time INTEGER
               );
               CREATE TABLE match_ingest_status (
                   match_id INTEGER PRIMARY KEY, latest_raw_artifact_id TEXT,
                   latest_raw_content_hash TEXT, first_usable_at TEXT
               );
               CREATE TABLE raw_source_artifacts (
                   artifact_id TEXT PRIMARY KEY, match_id INTEGER,
                   content_hash TEXT, first_usable_at TEXT
               );"""
        )
        connection.execute(
            "INSERT INTO matches VALUES (1, 99, ?)",
            (int(CUTOFF.timestamp()) - 60,),
        )
        assert _latest_patch(connection, CUTOFF) is None
        after = (CUTOFF + timedelta(seconds=1)).isoformat()
        connection.execute(
            "INSERT INTO match_ingest_status VALUES (1, 'artifact', ?, ?)",
            ("a" * 64, after),
        )
        connection.execute(
            "INSERT INTO raw_source_artifacts VALUES ('artifact', 1, ?, ?)",
            ("a" * 64, after),
        )
        assert _latest_patch(connection, CUTOFF) is None
        before = (CUTOFF - timedelta(seconds=1)).isoformat()
        connection.execute(
            "UPDATE match_ingest_status SET first_usable_at=?", (before,)
        )
        connection.execute(
            "UPDATE raw_source_artifacts SET first_usable_at=?", (before,)
        )
        assert _latest_patch(connection, CUTOFF) == 99
    finally:
        connection.close()


def test_anchor_rebase_changes_curve_identity(
    prepared_database: Path,
) -> None:
    deployment = _deployment(prepared_database)
    original = DraftAnchor(
        "match-1", 1, "a" * 64,
        (1, 2, 3, 4, 5), (6, 7, 8, 9, 10), "team_one",
        CUTOFF, "original.jpg", CUTOFF, "side-original.jpg",
    )
    rebased = replace(
        original,
        anchored_at=CUTOFF + timedelta(seconds=1),
        source_frame_ref="rebased.jpg",
        team_side_anchored_at=CUTOFF + timedelta(seconds=1),
        team_side_source_frame_ref="side-rebased.jpg",
    )
    identity = {
        "mapping_id": 7,
        "deployment_key": deployment.deployment_key,
        "input_snapshot_hash": "b" * 64,
        "feature_dependency_revision": deployment.dependency_revision,
        "feature_dependency_fingerprint": deployment.dependency_fingerprint,
    }
    assert _curve_key(anchor=original, **identity) != _curve_key(
        anchor=rebased, **identity
    )

    with LiveBettingStore(prepared_database) as store:
        persist_frozen_deployment(
            store.connection, deployment, created_at=CUTOFF
        )
        store.connection.execute(
            """INSERT INTO prospective_draft_curves
               (curve_key, raybet_match_id, map_number, strict_mapping_id,
                lineup_hash, radiant_hero_ids_json, dire_hero_ids_json,
                prediction_cutoff, first_usable_at, availability_mode, created_at,
                radiant_team_side, anchor_draft_hash, anchor_source_frame_ref,
                anchor_anchored_at, anchor_team_side_source_frame_ref,
                anchor_team_side_anchored_at, deployment_key, target_snapshot_hash,
                feature_snapshot_json, feature_dependency_fingerprint,
                feature_dependency_revision)
               VALUES (?, 'match-1', 1, 7, ?, '[1,2,3,4,5]', '[6,7,8,9,10]',
                       ?, ?, 'prospective', ?, 'team_one', ?, ?, ?, ?, ?, ?, ?,
                       '{}', ?, ?)""",
            (
                "c" * 64,
                canonical_hash({
                    "dire": [6, 7, 8, 9, 10],
                    "radiant": [1, 2, 3, 4, 5],
                }),
                CUTOFF.isoformat(), CUTOFF.isoformat(), CUTOFF.isoformat(),
                original.draft_hash, original.source_frame_ref,
                original.anchored_at.isoformat(),
                original.team_side_source_frame_ref,
                original.team_side_anchored_at.isoformat(),
                deployment.deployment_key, "b" * 64,
                deployment.dependency_fingerprint,
                deployment.dependency_revision,
            ),
        )
        assert _existing_curve(
            store.connection,
            rebased,
            7,
            deployment.deployment_key,
            deployment.dependency_revision,
            deployment.dependency_fingerprint,
        ) is None


def test_conflicted_anchor_is_not_a_publication_candidate(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = _deployment(prepared_database)
    monkeypatch.setattr(
        "live_betting.draft_publisher.query_strict_live_eligibility",
        lambda *args, **kwargs: _strict_result(),
    )
    with LiveBettingStore(prepared_database) as store:
        persist_frozen_deployment(
            store.connection,
            deployment,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        _insert_anchor(store, captured_at=CUTOFF + timedelta(seconds=2))
        store.insert_vision_observation(
            make_test_vision_observation(
                raybet_match_id="match-1",
                map_number=1,
                captured_at=CUTOFF + timedelta(seconds=3),
                game_clock_seconds=180,
                radiant_hero_ids=(1, 2, 3, 4, 11),
                dire_hero_ids=(6, 7, 8, 9, 10),
                clock_confidence=0.99,
                draft_confidence=0.99,
                radiant_team_side="team_one",
                label="draft-publisher-conflict",
            )
        )

        report = publish_cycle(
            store.connection,
            deployment=deployment,
            history=(),
            now=CUTOFF + timedelta(seconds=4),
        )

        assert report.candidates == 0
        assert store.connection.execute(
            "SELECT COUNT(*) FROM prospective_draft_curves"
        ).fetchone()[0] == 0


def test_forged_confirmed_low_confidence_anchor_is_rejected(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = _deployment(prepared_database)
    monkeypatch.setattr(
        "live_betting.draft_publisher.query_strict_live_eligibility",
        lambda *args, **kwargs: _strict_result(),
    )
    captured_at = CUTOFF + timedelta(seconds=2)
    radiant = (1, 2, 3, 4, 5)
    dire = (6, 7, 8, 9, 10)
    draft_payload = json.dumps(
        {"radiant": list(radiant), "dire": list(dire)},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    draft_hash = hashlib.sha256(draft_payload.encode()).hexdigest()
    with LiveBettingStore(prepared_database) as store:
        persist_frozen_deployment(
            store.connection,
            deployment,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        store.connection.execute(
            """INSERT INTO vision_observations
               (raybet_match_id, map_number, captured_at,
                game_clock_seconds, is_paused, radiant_hero_ids,
                dire_hero_ids, radiant_team_side, clock_confidence,
                draft_confidence, source_frame_ref, screen_state, confirmed)
               VALUES ('match-1', 1, ?, 120, 0, ?, ?, 'team_one',
                       0.10, 0.20, 'forged-frame.jpg', 'game', 1)""",
            (captured_at.isoformat(), json.dumps(radiant), json.dumps(dire)),
        )
        store.connection.execute(
            """INSERT INTO vision_draft_anchors
               (raybet_match_id, map_number, draft_hash, radiant_hero_ids,
                dire_hero_ids, radiant_team_side, team_side_anchored_at,
                team_side_source_frame_ref, anchored_at, source_frame_ref,
                status, conflict_at)
               VALUES ('match-1', 1, ?, ?, ?, 'team_one', ?,
                       'forged-frame.jpg', ?, 'forged-frame.jpg',
                       'anchored', NULL)""",
            (
                draft_hash,
                json.dumps(radiant),
                json.dumps(dire),
                captured_at.isoformat(),
                captured_at.isoformat(),
            ),
        )
        anchor = ready_draft_anchors(store.connection)[0]

        assert not draft_anchor_frames_are_authoritative(
            store.connection, anchor
        )
        report = publish_cycle(
            store.connection,
            deployment=deployment,
            history=_runtime_history(
                dependency_revision=deployment.dependency_revision,
                dependency_fingerprint=deployment.dependency_fingerprint,
                maps=load_prospective_history(store.connection),
            ),
            now=CUTOFF + timedelta(seconds=3),
        )

        assert report.candidates == 1
        assert report.skipped == 1
        assert report.results[0].reason == "vision_anchor_evidence_invalid"
        assert store.connection.execute(
            "SELECT COUNT(*) FROM prospective_draft_curves"
        ).fetchone()[0] == 0


def test_prospective_outcome_refresh_remains_failed_below_support(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = _deployment(prepared_database)

    def one_sample(*args, horizon_minutes: int, **kwargs):
        return (
            CalibrationSample(
                sample_id=f"{'a' * 64}:{horizon_minutes}",
                probability=0.55,
                outcome=1,
                observed_at=CUTOFF + timedelta(minutes=1),
                settled_at=CUTOFF + timedelta(hours=1),
                cluster_id="raybet:match-1",
                event_id="event-1",
            ),
        )

    monkeypatch.setattr(
        "live_betting.draft_publisher._prospective_calibration_samples",
        one_sample,
    )
    with LiveBettingStore(prepared_database) as store:
        persist_frozen_deployment(
            store.connection,
            current,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        refreshed = build_prospective_calibration_deployment(
            store.connection,
            current,
        )
        assert refreshed is not None
        assert refreshed.evidence_mode == "prospective"
        assert all(row.support == 1 for row in refreshed.calibrations)
        assert all(not row.passes_live_gate for row in refreshed.calibrations)
        assert persist_frozen_deployment(
            store.connection,
            refreshed,
            created_at=CUTOFF + timedelta(hours=2),
        )
        assert load_latest_frozen_deployment(store.connection) == refreshed


def test_landmark_insert_without_authoritative_artifacts_is_rejected(
    prepared_database: Path,
) -> None:
    with LiveBettingStore(prepared_database) as store:
        with pytest.raises(sqlite3.IntegrityError, match="authority"):
            store.connection.execute(
                """INSERT INTO prospective_draft_curves
                   (curve_key, raybet_match_id, map_number, strict_mapping_id,
                    lineup_hash, radiant_hero_ids_json, dire_hero_ids_json,
                    prediction_cutoff, first_usable_at, availability_mode,
                    created_at, radiant_team_side, anchor_draft_hash,
                    anchor_source_frame_ref, anchor_anchored_at, deployment_key,
                    target_snapshot_hash)
                   VALUES (?, 'match-1', 1, 7, ?, '[1,2,3,4,5]',
                           '[6,7,8,9,10]', ?, ?, 'prospective', ?, 'team_one',
                           ?, 'frame.jpg', ?, ?, ?)""",
                (
                    "a" * 64,
                    "b" * 64,
                    CUTOFF.isoformat(),
                    CUTOFF.isoformat(),
                    CUTOFF.isoformat(),
                    "c" * 64,
                    CUTOFF.isoformat(),
                    "d" * 64,
                    "e" * 64,
                ),
            )


def test_authoritative_prospective_artifact_can_enter_decision_gate(
    prepared_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    samples = {horizon: [] for horizon in HORIZONS}
    with LiveBettingStore(prepared_database) as store:
        intelligence = IntelligenceStorage(
            prepared_database, connection=store.connection
        )
        ingest = SQLiteIngestAdapter(
            intelligence,
            EventRegistry(intelligence),
        )
        opendota_archive = RawArchive(
            prepared_database.parent / "opendota-raw",
            observation_sink=ingest.record_raw_artifact,
        )
        _insert_event(store.connection)
        _insert_prospective_history(store.connection)
        current = _deployment(prepared_database)
        persist_frozen_deployment(
            store.connection,
            current,
            created_at=CUTOFF + timedelta(seconds=1),
        )
        history = load_prospective_history(store.connection)
        assert len(history) == 20
        for group in range(5):
            first = _insert_prospective_sample(
                store,
                opendota_archive=opendota_archive,
                deployment=current,
                history=history,
                index=group * 20,
                outcome=0,
            )
            probability = first[10].probability
            wins = min(19, round(probability * 20))
            for horizon, sample in first.items():
                samples[horizon].append(sample)
            for offset in range(1, 20):
                index = group * 20 + offset
                inserted = _insert_prospective_sample(
                    store,
                    opendota_archive=opendota_archive,
                    deployment=current,
                    history=history,
                    index=index,
                    outcome=int(offset <= wins),
                )
                for horizon, sample in inserted.items():
                    samples[horizon].append(sample)
        extra = _insert_prospective_sample(
            store,
            opendota_archive=opendota_archive,
            deployment=current,
            history=history,
            index=100,
            outcome=0,
        )
        for horizon, sample in extra.items():
            samples[horizon].append(sample)
        store.connection.commit()

        calibrations = tuple(
            build_calibration_artifact(
                current.model(horizon),
                evidence_mode="prospective",
                source_ref="prospective-draft-outcomes-v1",
                fit_samples=(),
                evaluation_samples=samples[horizon],
            )
            for horizon in HORIZONS
        )
        assert all(row.passes_live_gate for row in calibrations), [
            (row.horizon_minutes, row.gate.reasons, row.metrics)
            for row in calibrations
        ]
        fingerprint = draft_dependency_fingerprint(store.connection)
        revision = int(
            store.connection.execute(
                "SELECT dependency_revision FROM draft_lineage_revisions"
            ).fetchone()[0]
        )
        identity = _deployment_identity(
            training_cutoff=current.training_cutoff,
            dependency_fingerprint=fingerprint,
            dependency_revision=revision,
            models=current.models,
            calibrations=calibrations,
            evidence_mode="prospective",
        )
        prospective = FrozenDraftDeployment(
            deployment_key=canonical_hash(identity),
            training_cutoff=current.training_cutoff,
            dependency_fingerprint=fingerprint,
            dependency_revision=revision,
            models=current.models,
            calibrations=calibrations,
        )
        persist_frozen_deployment(
            store.connection,
            prospective,
            created_at=CUTOFF + timedelta(hours=4, minutes=30),
        )
        _insert_anchor(store, captured_at=CUTOFF + timedelta(hours=5))

        def strict(connection, *, raybet_match_id, map_number, **kwargs):
            if raybet_match_id == "match-1":
                return _strict_result()
            row = connection.execute(
                """SELECT mapping_id FROM strict_live_map_mappings
                    WHERE raybet_match_id=? AND map_number=?""",
                (raybet_match_id, map_number),
            ).fetchone()
            mapping = (
                None
                if row is None
                else SimpleNamespace(
                    mapping_id=int(row[0]),
                    event_id="event-1",
                    canonical_team_one_id=101,
                    canonical_team_two_id=202,
                )
            )
            return SimpleNamespace(
                eligible=mapping is not None,
                reason="eligible" if mapping is not None else "missing",
                mapping=mapping,
            )

        monkeypatch.setattr(
            "live_betting.draft_publisher.query_strict_live_eligibility",
            strict,
        )
        monkeypatch.setattr(
            "live_betting.profiles.draft_curve.query_strict_live_eligibility",
            strict,
        )
        cherry_picked = build_calibration_artifact(
            current.model(10),
            evidence_mode="prospective",
            source_ref="prospective-draft-outcomes-v1",
            fit_samples=(),
            evaluation_samples=samples[10][:100],
        )
        assert cherry_picked.passes_live_gate
        assert not _verify_prospective_calibration_evidence(
            store.connection,
            cherry_picked,
        )
        anchor = next(
            row
            for row in ready_draft_anchors(store.connection)
            if row.raybet_match_id == "match-1"
        )
        result = publish_anchor_curve(
            store.connection,
            anchor=anchor,
            deployment=prospective,
            history=_runtime_history(
                dependency_revision=prospective.dependency_revision,
                dependency_fingerprint=prospective.dependency_fingerprint,
                maps=history,
            ),
            published_at=CUTOFF + timedelta(hours=5, seconds=1),
        )
        assert result.status == "inserted"

        curve = build_draft_curve(
            store.connection,
            (1, 2, 3, 4, 5),
            (6, 7, 8, 9, 10),
            int((CUTOFF + timedelta(hours=5, seconds=2)).timestamp()),
            raybet_match_id="match-1",
            map_number=1,
            strict_mapping_id=7,
        )
        assert curve.unavailable_reason is None
        point = curve.at(10 * 60)
        assert point is not None
        authority = authority_from_curve(
            curve, point, radiant_team_side="team_one"
        )
        assert authority is not None
        assert draft_landmark_authority_matches(
            store.connection,
            authority,
            raybet_match_id="match-1",
            map_number=1,
            strict_mapping_id=7,
            radiant_hero_ids=(1, 2, 3, 4, 5),
            dire_hero_ids=(6, 7, 8, 9, 10),
            observed_at=CUTOFF + timedelta(hours=5, seconds=2),
            require_current_revisions=True,
        )
