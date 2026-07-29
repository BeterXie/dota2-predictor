"""Non-blocking coordinator for live explicit-draft official R.O.S.H. runs."""

from __future__ import annotations

import logging
import time
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from prematch.stratz_official_profile import RoshParityProfile

from .rosh_evidence import official_rosh_draft_hash
from .rosh_parity import (
    ExactByteArtifactStore,
    RoshAnalysisError,
    RoshParityOrchestrator,
)
from .rosh_parity_storage import RoshRunRepository, StoredRoshRun
from .storage import LiveBettingStore
from .stratz_rosh_client import StratzRoshClient


logger = logging.getLogger(__name__)
_RETRYABLE_ERROR_CODES = frozenset({"upstream_rate_limited", "upstream_unavailable"})


@dataclass(frozen=True)
class OfficialRoshRunKey:
    draft_hash: str
    rosh_profile_id: str
    canonical_profile_hash: str
    date_time: int


@dataclass(frozen=True)
class OfficialRoshRunStatus:
    status: str
    attempts: int
    error_code: str | None = None


@dataclass
class _AttemptState:
    attempts: int = 0
    status: str = "unavailable"
    error_code: str | None = None
    retry_at: float = 0.0
    terminal: bool = False


@dataclass
class _PendingRun:
    key: OfficialRoshRunKey
    future: Future[StoredRoshRun]
    submitted_at: float
    timeout_logged: bool = False


RunnerFactory = Callable[
    [RoshRunRepository, ExactByteArtifactStore], RoshParityOrchestrator
]


class OfficialRoshRunCoordinator:
    """Run at most one official analysis at a time without blocking the live loop."""

    def __init__(
        self,
        *,
        database_path: str | Path,
        artifact_root: str | Path | None = None,
        executor: Executor | None = None,
        runner_factory: RunnerFactory | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        max_attempts: int = 3,
        backoff_base_seconds: float = 5.0,
        backoff_cap_seconds: float = 30.0,
        timeout_seconds: float = 90.0,
    ) -> None:
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        if (
            backoff_base_seconds < 0
            or backoff_cap_seconds < 0
            or timeout_seconds <= 0
        ):
            raise ValueError("backoff and timeout values are invalid")
        self._database_path = Path(database_path)
        self._artifact_root = Path(
            artifact_root
            if artifact_root is not None
            else self._database_path.parent / "rosh-analysis-artifacts"
        )
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="official-rosh",
        )
        self._owns_executor = executor is None
        self._runner_factory = runner_factory or self._default_runner
        self._monotonic = monotonic
        self._max_attempts = max_attempts
        self._backoff_base_seconds = float(backoff_base_seconds)
        self._backoff_cap_seconds = float(backoff_cap_seconds)
        self._timeout_seconds = float(timeout_seconds)
        self._states: dict[OfficialRoshRunKey, _AttemptState] = {}
        self._active: _PendingRun | None = None
        self._closed = False

    @staticmethod
    def _default_runner(
        repository: RoshRunRepository,
        artifacts: ExactByteArtifactStore,
    ) -> RoshParityOrchestrator:
        return RoshParityOrchestrator(
            transport=StratzRoshClient(timeout_seconds=5.0),
            artifacts=artifacts,
            repository=repository,
        )

    def poll_or_submit(
        self,
        key: OfficialRoshRunKey,
        *,
        radiant_hero_ids: tuple[int, ...],
        dire_hero_ids: tuple[int, ...],
        request_started_at: datetime,
        profile: RoshParityProfile,
    ) -> OfficialRoshRunStatus:
        if self._closed:
            return OfficialRoshRunStatus("unavailable", 0, "coordinator_closed")
        if not self._valid_request(
            key,
            radiant_hero_ids,
            dire_hero_ids,
            request_started_at,
            profile,
        ):
            return OfficialRoshRunStatus("unavailable", 0, "request_identity_invalid")

        if self._active is not None:
            active = self._active
            now = self._monotonic()
            expired = now - active.submitted_at > self._timeout_seconds
            if not active.future.done():
                if expired:
                    if not active.timeout_logged:
                        active.timeout_logged = True
                        logger.warning("official Rosh background analysis timed out")
                    state = self._states[active.key]
                    state.status = "failed"
                    state.error_code = "background_timeout"
                if active.key == key:
                    attempts = self._states[key].attempts
                    return OfficialRoshRunStatus(
                        "failed" if expired else "pending",
                        attempts,
                        "background_timeout" if expired else None,
                    )
                return OfficialRoshRunStatus("unavailable", 0, "single_worker_busy")
            self._active = None
            completed = self._expire(active) if expired else self._consume(active)
            if active.key == key:
                return completed

        state = self._states.setdefault(key, _AttemptState())
        if state.status == "succeeded":
            return OfficialRoshRunStatus("succeeded", state.attempts)
        if state.terminal or state.attempts >= self._max_attempts:
            return OfficialRoshRunStatus("failed", state.attempts, state.error_code)
        if self._monotonic() < state.retry_at:
            return OfficialRoshRunStatus("failed", state.attempts, state.error_code)

        state.attempts += 1
        state.status = "pending"
        state.error_code = None
        future = self._executor.submit(
            self._execute,
            key,
            radiant_hero_ids,
            dire_hero_ids,
            request_started_at,
            profile,
        )
        self._active = _PendingRun(key, future, self._monotonic())
        return OfficialRoshRunStatus("pending", state.attempts)

    def _expire(self, pending: _PendingRun) -> OfficialRoshRunStatus:
        state = self._states[pending.key]
        try:
            pending.future.result()
        except Exception:
            pass
        state.status = "failed"
        state.error_code = "background_timeout"
        state.terminal = state.attempts >= self._max_attempts
        if not state.terminal:
            delay = min(
                self._backoff_cap_seconds,
                self._backoff_base_seconds * (2 ** (state.attempts - 1)),
            )
            state.retry_at = self._monotonic() + delay
        return OfficialRoshRunStatus("failed", state.attempts, state.error_code)

    def _consume(self, pending: _PendingRun) -> OfficialRoshRunStatus:
        state = self._states[pending.key]
        try:
            stored = pending.future.result()
            run = stored.run
            if (
                run.status != "succeeded"
                or run.draft_hash != pending.key.draft_hash
                or run.rosh_profile_id != pending.key.rosh_profile_id
                or run.canonical_profile_hash != pending.key.canonical_profile_hash
                or run.date_time != pending.key.date_time
            ):
                raise ValueError("official Rosh result identity mismatch")
        except RoshAnalysisError as error:
            state.status = "failed"
            state.error_code = error.error_code
            retryable = error.error_code in _RETRYABLE_ERROR_CODES
            state.terminal = not retryable or state.attempts >= self._max_attempts
            if not state.terminal:
                delay = min(
                    self._backoff_cap_seconds,
                    self._backoff_base_seconds * (2 ** (state.attempts - 1)),
                )
                state.retry_at = self._monotonic() + delay
            logger.warning(
                "official Rosh background analysis failed (%s, attempt %d/%d)",
                error.error_code,
                state.attempts,
                self._max_attempts,
            )
            return OfficialRoshRunStatus("failed", state.attempts, state.error_code)
        except Exception as error:
            state.status = "failed"
            state.error_code = "background_failure"
            state.terminal = True
            logger.warning(
                "official Rosh background analysis failed closed (%s)",
                type(error).__name__,
            )
            return OfficialRoshRunStatus("failed", state.attempts, state.error_code)

        state.status = "succeeded"
        state.error_code = None
        state.terminal = True
        return OfficialRoshRunStatus("succeeded", state.attempts)

    def _execute(
        self,
        key: OfficialRoshRunKey,
        radiant_hero_ids: tuple[int, ...],
        dire_hero_ids: tuple[int, ...],
        request_started_at: datetime,
        profile: RoshParityProfile,
    ) -> StoredRoshRun:
        analysis_input = {
            "mode": "explicit_draft",
            "date_time": key.date_time,
            "bracket_ids": ["IMMORTAL"],
            "radiant": [
                {"hero_id": hero_id, "position_id": position_id}
                for position_id, hero_id in enumerate(radiant_hero_ids, 1)
            ],
            "dire": [
                {"hero_id": hero_id, "position_id": position_id}
                for position_id, hero_id in enumerate(dire_hero_ids, 1)
            ],
        }
        # Open inside the worker so SQLite objects are never shared across threads.
        with LiveBettingStore(self._database_path) as store:
            repository = RoshRunRepository(store.connection)
            existing = repository.get_succeeded_for_explicit_identity(
                key.draft_hash,
                rosh_profile_id=key.rosh_profile_id,
                canonical_profile_hash=key.canonical_profile_hash,
                date_time=key.date_time,
            )
            if existing is not None:
                return existing
            runner = self._runner_factory(
                repository,
                ExactByteArtifactStore(self._artifact_root),
            )
            return runner.execute(
                analysis_input,
                profile,
                request_started_at=request_started_at,
            )

    @staticmethod
    def _valid_request(
        key: OfficialRoshRunKey,
        radiant_hero_ids: tuple[int, ...],
        dire_hero_ids: tuple[int, ...],
        request_started_at: datetime,
        profile: RoshParityProfile,
    ) -> bool:
        try:
            expected_draft_hash = official_rosh_draft_hash(
                radiant_hero_ids,
                dire_hero_ids,
            )
        except ValueError:
            return False
        return (
            expected_draft_hash == key.draft_hash
            and key.rosh_profile_id == profile.rosh_profile_id
            and key.canonical_profile_hash == profile.canonical_profile_hash
            and type(key.date_time) is int
            and key.date_time > 0
            and isinstance(request_started_at, datetime)
            and request_started_at.tzinfo is not None
            and request_started_at.utcoffset() is not None
            and int(request_started_at.astimezone(timezone.utc).timestamp())
            >= key.date_time
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._active is not None:
            self._active.future.cancel()
            self._active = None
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def __enter__(self) -> "OfficialRoshRunCoordinator":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


__all__ = [
    "OfficialRoshRunCoordinator",
    "OfficialRoshRunKey",
    "OfficialRoshRunStatus",
]
