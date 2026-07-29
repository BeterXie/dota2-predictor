from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import live_betting.rosh_parity as parity
from live_betting.rosh_parity import (
    ArtifactError,
    ExactByteArtifactStore,
    RoshAnalysisError,
    RoshParityOrchestrator,
)
from live_betting.rosh_parity_storage import RoshRunRepository
from live_betting.storage import LiveBettingStore
from live_betting.stratz_rosh_client import OfficialRoshBatch, StratzRoshError
from prematch.stratz_official_profile import (
    V1_PROFILE,
    build_official_request_plan,
)
from prematch.stratz_official_score import (
    HeroScore,
    MinutePoint,
    MinuteSlot,
    OfficialRoshResult,
)


STARTED_AT = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
COLLECTED_AT = datetime(2026, 7, 28, 12, 0, 5, tzinfo=timezone.utc)
FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "stratz_official_rosh"
    / "8904419709"
)
REQUEST_SHA = "280f11b38a29c87751c4f36c74d95d4b89bf087f00b766331fbbe379f551971f"
RESPONSE_SHA = "2afbe95c420676d34b87737138133443673a8d8c9e7d2bf10069712e799e70e7"


def historical_input() -> dict[str, Any]:
    return {
        "mode": "historical_match",
        "match_id": 8904419709,
        "date_time": 1784485548,
        "bracket_ids": ["IMMORTAL"],
    }


def explicit_input() -> dict[str, Any]:
    return {
        "mode": "explicit_draft",
        "date_time": 1784485548,
        "bracket_ids": ["IMMORTAL"],
        "radiant": [
            {"hero_id": hero_id, "position_id": position}
            for position, hero_id in enumerate((54, 120, 28, 90, 123), 1)
        ],
        "dire": [
            {"hero_id": hero_id, "position_id": position}
            for position, hero_id in enumerate((145, 74, 96, 79, 87), 1)
        ],
    }


def exact_request(mode: str = "historical_match") -> bytes:
    if mode == "historical_match":
        return (FIXTURE / "requests.json").read_bytes()
    plan = build_official_request_plan(
        explicit_input(), request_started_at=STARTED_AT
    )
    return json.dumps(
        [
            {
                "operationName": operation.operation_name,
                "variables": parity._json_value(operation.variables),
                "query": operation.query,
            }
            for operation in plan.operations
        ],
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()


def golden_batch(collected_at: datetime = COLLECTED_AT) -> OfficialRoshBatch:
    response_body = (FIXTURE / "responses.sanitized.json").read_bytes()
    return OfficialRoshBatch(
        request_body=exact_request(),
        response_body=response_body,
        responses=tuple(json.loads(response_body)),
        collected_at=collected_at,
        diagnostics={},
    )


def draft_match() -> dict[str, Any]:
    radiant = (54, 120, 28, 90, 123)
    dire = (145, 74, 96, 79, 87)
    picks = [
        {"heroId": hero_id, "isPick": True, "isRadiant": side == "RADIANT"}
        for side, heroes in (("RADIANT", radiant), ("DIRE", dire))
        for hero_id in heroes
    ]
    players = [
        {"heroId": hero_id, "position": f"POSITION_{position}"}
        for heroes in (radiant, dire)
        for position, hero_id in enumerate(heroes, 1)
    ]
    return {
        "id": 8904419709,
        "endDateTime": 1784485548,
        "pickBans": picks,
        "players": players,
    }


def small_historical_batch(
    match: Mapping[str, Any] | None,
    *,
    collected_at: datetime = COLLECTED_AT,
) -> OfficialRoshBatch:
    values = [
        {"data": {"match": match}},
        {"data": {}},
        {"data": {}},
        {"data": {}},
        {"data": {}},
        {"data": {}},
    ]
    response = json.dumps(values, separators=(",", ":")).encode()
    return OfficialRoshBatch(
        exact_request(), response, tuple(values), collected_at, {}
    )


def small_explicit_batch() -> OfficialRoshBatch:
    values = tuple({"data": {}} for _ in range(5))
    response = json.dumps(values, separators=(",", ":")).encode()
    return OfficialRoshBatch(
        exact_request("explicit_draft"), response, values, COLLECTED_AT, {}
    )


class FakeTransport:
    def __init__(self, outcome: OfficialRoshBatch | BaseException) -> None:
        self.outcome = outcome
        self.calls = 0

    def fetch_official_batch(self, _plan: Any) -> OfficialRoshBatch:
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class CountingRepository(RoshRunRepository):
    def __init__(self, store: LiveBettingStore) -> None:
        super().__init__(store.connection)
        self.calls: list[str] = []

    def get_by_evidence_hash(self, evidence_hash: str) -> Any:
        self.calls.append("get_by_evidence_hash")
        return super().get_by_evidence_hash(evidence_hash)

    def write_succeeded(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append("write_succeeded")
        return super().write_succeeded(*args, **kwargs)

    def write_failed(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append("write_failed")
        return super().write_failed(*args, **kwargs)


@pytest.fixture
def database() -> Any:
    store = LiveBettingStore(":memory:")
    store.init_schema()
    repository = CountingRepository(store)
    yield store, repository
    store.close()


def counts(store: LiveBettingStore) -> tuple[int, int, int]:
    return tuple(
        store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "rosh_analysis_runs",
            "rosh_hero_scores",
            "rosh_minute_points",
        )
    )


def make_runner(
    tmp_path: Path,
    repository: RoshRunRepository,
    outcome: OfficialRoshBatch | BaseException,
    *,
    hook: Any = None,
) -> tuple[RoshParityOrchestrator, FakeTransport, ExactByteArtifactStore]:
    transport = FakeTransport(outcome)
    artifacts = ExactByteArtifactStore(tmp_path / "artifacts")
    runner = RoshParityOrchestrator(
        transport=transport,
        artifacts=artifacts,
        repository=repository,
        event_hook=hook,
        clock=lambda: COLLECTED_AT,
    )
    return runner, transport, artifacts


def test_golden_historical_e2e_persists_exact_evidence_and_all_scores(
    tmp_path: Path,
    database: Any,
) -> None:
    store, repository = database
    runner, transport, artifacts = make_runner(
        tmp_path, repository, golden_batch()
    )

    stored = runner.execute(historical_input(), request_started_at=STARTED_AT)

    assert transport.calls == 1
    assert stored.run.status == "succeeded"
    assert stored.run.mode == "historical_match"
    assert stored.run.draft == {
        "radiant": explicit_input()["radiant"],
        "dire": explicit_input()["dire"],
    }
    assert len(stored.hero_scores) == 10
    assert len(stored.minute_points) == 41
    assert stored.run.radiant_team_score == -4.9
    assert stored.run.dire_team_score == -10.7
    assert stored.run.relative_advantage == 5.8
    assert [hero.display_score for hero in stored.hero_scores] == [
        12.4,
        -11.1,
        -1.2,
        -1.6,
        -3.4,
        -10.9,
        -3.4,
        5.1,
        -3.5,
        2.0,
    ]
    points = {point.minute: point.display_score for point in stored.minute_points}
    assert {minute: points[minute] for minute in (20, 30, 36, 37, 40, 50, 60)} == {
        20: -7.0,
        30: -5.7,
        36: -5.5,
        37: -5.5,
        40: -5.6,
        50: -5.8,
        60: -6.0,
    }
    assert counts(store) == (1, 10, 41)
    request_artifact = stored.run.request_manifest["request_artifact"]
    response_artifact = stored.run.response_manifest[0]
    assert request_artifact["content_sha256"] == REQUEST_SHA
    assert response_artifact["response_artifact_hash"] == RESPONSE_SHA
    request_path = artifacts.root / request_artifact["relative_path"]
    response_path = artifacts.root / response_artifact["relative_path"]
    assert gzip.decompress(request_path.read_bytes()) == exact_request()
    assert gzip.decompress(response_path.read_bytes()) == (
        FIXTURE / "responses.sanitized.json"
    ).read_bytes()


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("network_failure", "upstream_unavailable"),
        ("graphql_failure", "upstream_unavailable"),
        ("invalid_json", "upstream_unavailable"),
        ("http_429", "upstream_rate_limited"),
    ],
)
def test_historical_transport_failure_before_batch_never_crosses_run_boundary(
    tmp_path: Path,
    database: Any,
    category: str,
    expected: str,
) -> None:
    store, repository = database
    runner, _, artifacts = make_runner(
        tmp_path,
        repository,
        StratzRoshError(
            "Bearer private-value upstream-body",
            category=category,
        ),
    )

    with pytest.raises(RoshAnalysisError) as raised:
        runner.execute(historical_input(), request_started_at=STARTED_AT)

    assert raised.value.error_code == expected
    assert raised.value.run_id is None
    assert repository.calls == []
    assert counts(store) == (0, 0, 0)
    assert not artifacts.root.exists()
    assert "private-value" not in str(raised.value)


@pytest.mark.parametrize("failure", ["missing_match", "incomplete", "duplicate"])
def test_http_200_invalid_historical_draft_does_not_write_artifacts_or_rows(
    tmp_path: Path,
    database: Any,
    failure: str,
) -> None:
    store, repository = database
    match: Mapping[str, Any] | None = draft_match()
    if failure == "missing_match":
        match = None
    elif failure == "incomplete":
        match = {**draft_match(), "players": draft_match()["players"][:-1]}
    else:
        players = list(draft_match()["players"])
        players[-1] = players[0]
        match = {**draft_match(), "players": players}
    runner, _, artifacts = make_runner(
        tmp_path, repository, small_historical_batch(match)
    )

    with pytest.raises(RoshAnalysisError) as raised:
        runner.execute(historical_input(), request_started_at=STARTED_AT)

    assert raised.value.error_code in {
        "source_match_not_found",
        "source_data_incomplete",
    }
    assert repository.calls == []
    assert counts(store) == (0, 0, 0)
    assert not artifacts.root.exists()


def test_http_200_graphql_error_batch_is_pre_draft_even_if_match_item_is_valid(
    tmp_path: Path,
    database: Any,
) -> None:
    store, repository = database
    valid = small_historical_batch(draft_match())
    values = list(json.loads(valid.response_body))
    values[1] = {
        "data": {},
        "errors": [{"message": "Authorization Bearer upstream-body"}],
    }
    response = json.dumps(values, separators=(",", ":")).encode()
    batch = replace(valid, response_body=response, responses=tuple(values))
    runner, _, artifacts = make_runner(tmp_path, repository, batch)

    with pytest.raises(RoshAnalysisError) as raised:
        runner.execute(historical_input(), request_started_at=STARTED_AT)

    assert raised.value.error_code == "upstream_unavailable"
    assert repository.calls == []
    assert counts(store) == (0, 0, 0)
    assert not artifacts.root.exists()


def test_valid_historical_draft_then_normalizer_failure_writes_one_failed_run(
    tmp_path: Path,
    database: Any,
) -> None:
    store, repository = database
    runner, _, _ = make_runner(
        tmp_path, repository, small_historical_batch(draft_match())
    )

    with pytest.raises(RoshAnalysisError) as raised:
        runner.execute(historical_input(), request_started_at=STARTED_AT)

    assert raised.value.error_code == "source_data_incomplete"
    assert raised.value.run_id is not None
    assert raised.value.__suppress_context__ is True
    assert counts(store) == (1, 0, 0)
    row = store.connection.execute(
        "SELECT status, error_code, draft_json, result_json FROM rosh_analysis_runs"
    ).fetchone()
    assert tuple(row[:2]) == ("failed", "source_data_incomplete")
    assert json.loads(row[2]) == {
        "radiant": explicit_input()["radiant"],
        "dire": explicit_input()["dire"],
    }
    assert row[3] is None


@pytest.mark.parametrize("failing_component", ["normalizer", "scorer"])
def test_post_draft_runtime_error_is_allowlisted_and_fully_redacted(
    tmp_path: Path,
    database: Any,
    monkeypatch: pytest.MonkeyPatch,
    failing_component: str,
) -> None:
    store, repository = database
    secret = "Authorization Bearer raw-secret-p2c"
    events: list[Mapping[str, Any]] = []

    def fail(*_args: Any) -> Any:
        raise RuntimeError(secret)

    if failing_component == "normalizer":
        monkeypatch.setattr(parity, "normalize_official_responses", fail)
    else:
        monkeypatch.setattr(
            parity, "normalize_official_responses", lambda *_: object()
        )
        monkeypatch.setattr(parity, "score_official_rosh", fail)
    runner, _, artifacts = make_runner(
        tmp_path,
        repository,
        small_historical_batch(draft_match()),
        hook=events.append,
    )

    with pytest.raises(RoshAnalysisError) as raised:
        runner.execute(historical_input(), request_started_at=STARTED_AT)

    assert raised.value.error_code == "source_data_incomplete"
    assert raised.value.run_id is not None
    assert counts(store) == (1, 0, 0)
    row = store.connection.execute(
        """SELECT status, error_code, request_manifest_json,
                  response_manifest_json, result_json
           FROM rosh_analysis_runs"""
    ).fetchone()
    assert tuple(row[:2]) == ("failed", "source_data_incomplete")
    assert row[4] is None
    surfaces = {
        "str": str(raised.value),
        "repr": repr(raised.value),
        "event": json.dumps(events),
        "manifest": json.dumps([json.loads(row[2]), json.loads(row[3])]),
        "database": "\n".join(store.connection.iterdump()),
        "artifact": b"\n".join(
            gzip.decompress(path.read_bytes())
            for path in artifacts.root.rglob("*.gz")
        ).decode(),
    }
    for surface in surfaces.values():
        for token in ("Authorization", "Bearer", "raw-secret-p2c"):
            assert token not in surface


def test_explicit_draft_transport_failure_writes_exactly_one_failed_run(
    tmp_path: Path,
    database: Any,
) -> None:
    store, repository = database
    runner, _, _ = make_runner(
        tmp_path,
        repository,
        StratzRoshError("Authorization: Bearer hidden", category="http_5xx"),
    )

    for _ in range(2):
        with pytest.raises(RoshAnalysisError) as raised:
            runner.execute(explicit_input(), request_started_at=STARTED_AT)
        assert raised.value.error_code == "upstream_unavailable"
        assert raised.value.run_id is not None

    assert counts(store) == (1, 0, 0)
    assert store.connection.execute(
        "SELECT error_code FROM rosh_analysis_runs"
    ).fetchone()[0] == "upstream_unavailable"


def test_repeated_pre_draft_failures_leave_no_orphan_artifacts_or_rows(
    tmp_path: Path,
    database: Any,
) -> None:
    store, repository = database
    runner, transport, artifacts = make_runner(
        tmp_path,
        repository,
        StratzRoshError("cookie=private", category="invalid_response"),
    )

    for _ in range(3):
        with pytest.raises(RoshAnalysisError):
            runner.execute(historical_input(), request_started_at=STARTED_AT)

    assert transport.calls == 3
    assert repository.calls == []
    assert counts(store) == (0, 0, 0)
    assert not list(artifacts.root.rglob("*")) if artifacts.root.exists() else True


def test_public_error_and_hook_are_allowlisted_and_redacted(
    tmp_path: Path,
    database: Any,
) -> None:
    _, repository = database
    events: list[Mapping[str, Any]] = []
    runner, _, _ = make_runner(
        tmp_path,
        repository,
        RuntimeError(
            "Authorization Bearer top-secret cookie upstream-body raw-exception"
        ),
        hook=events.append,
    )

    with pytest.raises(RoshAnalysisError) as raised:
        runner.execute(historical_input(), request_started_at=STARTED_AT)

    assert raised.value.error_code == "upstream_unavailable"
    serialized = json.dumps(events)
    assert set(events[0]) == {
        "event",
        "stage",
        "error_code",
        "mode",
        "profile_id",
        "request_hash_prefix",
        "run_id_prefix",
    }
    for secret in ("top-secret", "Authorization", "Bearer", "cookie", "upstream-body", "raw-exception"):
        assert secret not in str(raised.value)
        assert secret not in serialized


def test_invalid_explicit_draft_and_unactivated_v1_never_call_dependencies(
    tmp_path: Path,
    database: Any,
) -> None:
    store, repository = database
    runner, transport, artifacts = make_runner(
        tmp_path,
        repository,
        RuntimeError("transport must not be called"),
    )
    duplicate = explicit_input()
    duplicate["dire"][0]["hero_id"] = duplicate["radiant"][0]["hero_id"]

    with pytest.raises(RoshAnalysisError) as invalid:
        runner.execute(duplicate, request_started_at=STARTED_AT)
    with pytest.raises(RoshAnalysisError) as inactive:
        runner.execute(
            historical_input(),
            V1_PROFILE,
            request_started_at=STARTED_AT,
        )

    assert invalid.value.error_code == "invalid_request"
    assert inactive.value.error_code == "profile_drift"
    assert transport.calls == 0
    assert repository.calls == []
    assert counts(store) == (0, 0, 0)
    assert not artifacts.root.exists()


def test_post_draft_secret_response_is_not_persisted_and_becomes_failed_run(
    tmp_path: Path,
    database: Any,
) -> None:
    store, repository = database
    valid = small_historical_batch(draft_match())
    values = list(json.loads(valid.response_body))
    values[1] = {"data": {"Authorization": "Bearer should-never-land"}}
    response = json.dumps(values, separators=(",", ":")).encode()
    batch = replace(valid, response_body=response, responses=tuple(values))
    runner, _, artifacts = make_runner(tmp_path, repository, batch)

    with pytest.raises(RoshAnalysisError) as raised:
        runner.execute(historical_input(), request_started_at=STARTED_AT)

    assert raised.value.error_code == "source_data_incomplete"
    assert counts(store) == (1, 0, 0)
    assert store.connection.execute(
        "SELECT response_manifest_json FROM rosh_analysis_runs"
    ).fetchone()[0] == "[]"
    retained = [
        gzip.decompress(path.read_bytes())
        for path in artifacts.root.rglob("*.gz")
    ]
    assert retained == [exact_request()]
    assert all(b"should-never-land" not in body for body in retained)


def test_same_exact_evidence_with_later_collection_time_reuses_immutable_run(
    tmp_path: Path,
    database: Any,
) -> None:
    store, repository = database
    batches = iter(
        (
            golden_batch(COLLECTED_AT),
            golden_batch(COLLECTED_AT + timedelta(hours=1)),
        )
    )

    class ChangingTransport:
        def fetch_official_batch(self, _plan: Any) -> OfficialRoshBatch:
            return next(batches)

    runner = RoshParityOrchestrator(
        transport=ChangingTransport(),
        artifacts=ExactByteArtifactStore(tmp_path / "artifacts"),
        repository=repository,
    )

    first = runner.execute(historical_input(), request_started_at=STARTED_AT)
    second = runner.execute(historical_input(), request_started_at=STARTED_AT)

    assert second == first
    assert second.run.collected_at == first.run.collected_at
    assert counts(store) == (1, 10, 41)


def synthetic_result() -> OfficialRoshResult:
    heroes = tuple(
        HeroScore(
            side,
            hero_id,
            position,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
        )
        for side, values in (
            ("RADIANT", (54, 120, 28, 90, 123)),
            ("DIRE", (145, 74, 96, 79, 87)),
        )
        for position, hero_id in enumerate(values, 1)
    )
    slots = tuple(
        MinuteSlot(side, hero_id, position, "DIVINE_IMMORTAL", 1000, 0.0)
        for side, values in (
            ("RADIANT", (54, 120, 28, 90, 123)),
            ("DIRE", (145, 74, 96, 79, 87)),
        )
        for position, hero_id in enumerate(values, 1)
    )
    minute = MinutePoint(
        20,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        {"DIVINE_IMMORTAL": 10, "ALL_RANK_FALLBACK": 0},
        slots,
    )
    return OfficialRoshResult(
        "stratz-official-rosh/2026-07-28-v2",
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        heroes,
        (minute,),
        hashlib.sha256(b"synthetic-result").hexdigest(),
    )


def test_concurrent_same_request_uses_singleflight_not_a_result_cache_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    store = LiveBettingStore(":memory:", connection=connection)
    store.init_schema()
    repository = CountingRepository(store)
    entered = threading.Event()

    class SlowTransport:
        calls = 0

        def fetch_official_batch(self, _plan: Any) -> OfficialRoshBatch:
            self.calls += 1
            entered.set()
            time.sleep(0.2)
            return small_explicit_batch()

    transport = SlowTransport()
    monkeypatch.setattr(parity, "normalize_official_responses", lambda *_: object())
    monkeypatch.setattr(parity, "score_official_rosh", lambda *_: synthetic_result())
    runner = RoshParityOrchestrator(
        transport=transport,
        artifacts=ExactByteArtifactStore(tmp_path / "artifacts"),
        repository=repository,
    )
    results: list[Any] = []
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            results.append(
                runner.execute(explicit_input(), request_started_at=STARTED_AT)
            )
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=invoke)
    second = threading.Thread(target=invoke)
    first.start()
    entered.wait(timeout=2)
    second.start()
    first.join(timeout=5)
    second.join(timeout=5)

    assert errors == []
    assert len(results) == 2
    assert results[0].run.run_id == results[1].run.run_id
    assert transport.calls == 1
    assert counts(store) == (1, 10, 1)
    connection.close()


def test_artifact_secret_scan_and_repository_fault_fail_closed_atomically(
    tmp_path: Path,
    database: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, repository = database
    with pytest.raises(ArtifactError):
        ExactByteArtifactStore(tmp_path / "secrets").persist(
            b'{"Authorization":"Bearer private"}'
        )
    assert not (tmp_path / "secrets").exists()

    monkeypatch.setattr(parity, "normalize_official_responses", lambda *_: object())
    monkeypatch.setattr(parity, "score_official_rosh", lambda *_: synthetic_result())

    def fail(stage: str, _index: int | None = None) -> None:
        if stage == "hero":
            raise RuntimeError("database detail should not escape")

    monkeypatch.setattr(repository, "_checkpoint", fail)
    runner, _, _ = make_runner(
        tmp_path, repository, small_explicit_batch()
    )

    with pytest.raises(RoshAnalysisError) as raised:
        runner.execute(explicit_input(), request_started_at=STARTED_AT)

    assert raised.value.error_code == "source_data_incomplete"
    assert "database detail" not in str(raised.value)
    assert counts(store) == (0, 0, 0)


def test_existing_noncanonical_gzip_artifact_fails_closed(tmp_path: Path) -> None:
    body = b'{"safe":"exact-evidence"}'
    content_hash = hashlib.sha256(body).hexdigest()
    store = ExactByteArtifactStore(tmp_path / "artifacts")
    path = (
        store.root
        / "sha256"
        / content_hash[:2]
        / f"{content_hash}.json.gz"
    )
    path.parent.mkdir(parents=True)
    noncanonical = gzip.compress(body, compresslevel=9, mtime=1)
    canonical = gzip.compress(body, compresslevel=9, mtime=0)
    assert noncanonical != canonical
    assert gzip.decompress(noncanonical) == body
    path.write_bytes(noncanonical)

    with pytest.raises(ArtifactError):
        store.persist(body)

    assert path.read_bytes() == noncanonical


@pytest.mark.parametrize("failure_path", ["success", "recorded_failure"])
def test_repository_evidence_lookup_error_is_allowlisted_and_has_no_partial_rows(
    tmp_path: Path,
    database: Any,
    monkeypatch: pytest.MonkeyPatch,
    failure_path: str,
) -> None:
    store, repository = database
    secret = "Authorization Bearer repository-raw-secret"
    events: list[Mapping[str, Any]] = []

    def fail_lookup(_evidence_hash: str) -> Any:
        raise RuntimeError(secret)

    def fail_normalizer(*_args: Any) -> Any:
        raise RuntimeError("normalizer failure")

    monkeypatch.setattr(repository, "get_by_evidence_hash", fail_lookup)
    if failure_path == "success":
        monkeypatch.setattr(
            parity, "normalize_official_responses", lambda *_: object()
        )
        monkeypatch.setattr(parity, "score_official_rosh", lambda *_: synthetic_result())
    else:
        monkeypatch.setattr(parity, "normalize_official_responses", fail_normalizer)
    runner, _, _ = make_runner(
        tmp_path,
        repository,
        small_historical_batch(draft_match()),
        hook=events.append,
    )

    with pytest.raises(RoshAnalysisError) as raised:
        runner.execute(historical_input(), request_started_at=STARTED_AT)

    assert raised.value.error_code == "source_data_incomplete"
    assert raised.value.run_id is None
    assert counts(store) == (0, 0, 0)
    exposed = f"{raised.value!s}\n{raised.value!r}\n{json.dumps(events)}"
    for token in ("Authorization", "Bearer", "repository-raw-secret"):
        assert token not in exposed


def test_transport_exact_request_or_parsed_response_drift_fails_closed_pre_draft(
    tmp_path: Path,
    database: Any,
) -> None:
    store, repository = database
    good = small_historical_batch(draft_match())
    drifted = replace(good, request_body=good.request_body + b" ")
    runner, _, artifacts = make_runner(tmp_path, repository, drifted)

    with pytest.raises(RoshAnalysisError) as raised:
        runner.execute(historical_input(), request_started_at=STARTED_AT)

    assert raised.value.error_code == "profile_drift"
    assert repository.calls == []
    assert counts(store) == (0, 0, 0)
    assert not artifacts.root.exists()
