"""Lifecycle and evidence scheduling for live Vision stream capture.

The tracker deliberately has no database dependency.  It turns noisy per-frame
HUD readings into a small set of capture events while keeping the production
prediction input boundary unchanged.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Literal


CapturePhase = Literal[
    "waiting_for_stream",
    "draft_candidate",
    "draft_started",
    "game_started",
    "ended_grace",
    "closed",
]


@dataclass(frozen=True)
class EvidenceTrigger:
    """One reason to persist the current frame as evidence."""

    event: str
    phase: CapturePhase
    scheduled_at: float
    reason: str


class CaptureLifecycle:
    """Confirm BP/game transitions and schedule bounded evidence captures."""

    def __init__(
        self,
        *,
        evidence_interval: float = 30.0,
        end_grace_seconds: float = 90.0,
        draft_window_size: int = 5,
        draft_minimum_support: int = 4,
        game_window_size: int = 5,
        game_minimum_support: int = 3,
    ) -> None:
        if evidence_interval <= 0:
            raise ValueError("evidence_interval must be positive")
        if end_grace_seconds <= 0:
            raise ValueError("end_grace_seconds must be positive")
        if not 1 <= draft_minimum_support <= draft_window_size:
            raise ValueError("draft support must fit inside the draft window")
        if not 1 <= game_minimum_support <= game_window_size:
            raise ValueError("game support must fit inside the game window")
        self.evidence_interval = float(evidence_interval)
        self.end_grace_seconds = float(end_grace_seconds)
        self.draft_window_size = draft_window_size
        self.draft_minimum_support = draft_minimum_support
        self.game_window_size = game_window_size
        self.game_minimum_support = game_minimum_support
        self.phase: CapturePhase = "waiting_for_stream"
        self.bp_candidate_started_at: float | None = None
        self.draft_started_at: float | None = None
        self.game_started_at: float | None = None
        self.ended_at: float | None = None
        self.close_at: float | None = None
        self.last_evidence_at: float | None = None
        self.next_periodic_at: float | None = None
        self.last_map_number: int | None = None
        self._draft_samples: deque[tuple[float, bool]] = deque(
            maxlen=draft_window_size
        )
        self._game_samples: deque[tuple[float, bool]] = deque(
            maxlen=game_window_size
        )
        self._final_emitted = False

    @property
    def has_started(self) -> bool:
        return self.phase in {
            "draft_started",
            "game_started",
            "ended_grace",
            "closed",
        }

    @property
    def final_emitted(self) -> bool:
        return self._final_emitted

    def _trigger(
        self,
        event: str,
        captured_at: float,
        *,
        scheduled_at: float | None = None,
        reason: str,
    ) -> EvidenceTrigger:
        return EvidenceTrigger(
            event=event,
            phase=self.phase,
            scheduled_at=captured_at if scheduled_at is None else scheduled_at,
            reason=reason,
        )

    def _capture_immediate(
        self, event: str, captured_at: float, *, reason: str
    ) -> EvidenceTrigger:
        self.last_evidence_at = captured_at
        if self.next_periodic_at is None:
            self.next_periodic_at = captured_at + self.evidence_interval
        return self._trigger(event, captured_at, reason=reason)

    def _capture_periodic(self, captured_at: float) -> EvidenceTrigger:
        scheduled_at = self.next_periodic_at or captured_at
        # A delayed frame is recorded at the current time.  Do not backfill a
        # burst of missed screenshots; the next slot starts a fresh interval.
        self.next_periodic_at = captured_at + self.evidence_interval
        self.last_evidence_at = captured_at
        return self._trigger(
            "periodic_30s",
            captured_at,
            scheduled_at=scheduled_at,
            reason="periodic",
        )

    def restore(self, events: Iterable[dict[str, object]]) -> None:
        """Restore durable event identity after a watcher restart."""

        for payload in events:
            event = str(payload.get("event") or "")
            value = payload.get("captured_at")
            try:
                captured_at = float(value or 0.0)
            except (TypeError, ValueError):
                try:
                    captured_at = datetime.fromisoformat(str(value)).timestamp()
                except (TypeError, ValueError):
                    continue
            if captured_at <= 0:
                continue
            if event == "draft_started" and self.draft_started_at is None:
                self.draft_started_at = captured_at
                self.phase = "draft_started"
                self.next_periodic_at = captured_at + self.evidence_interval
            elif event == "game_started" and self.game_started_at is None:
                self.game_started_at = captured_at
                self.phase = "game_started"
            elif event == "periodic_30s":
                self.last_evidence_at = captured_at
                self.next_periodic_at = captured_at + self.evidence_interval
            elif event == "map_changed":
                self.phase = "game_started"
            elif event == "ended_final":
                self._final_emitted = True
                self.phase = "ended_grace"
                self.ended_at = captured_at
                self.close_at = captured_at + self.end_grace_seconds
            if event in {"draft_started", "game_started", "map_changed"}:
                self.last_evidence_at = max(self.last_evidence_at or 0.0, captured_at)

    def restore_game_started(self, captured_at: float) -> None:
        """Seed restart state from an existing Observation JSONL sequence."""

        if captured_at > 0 and self.game_started_at is None:
            self.game_started_at = captured_at
            self.phase = "game_started"
            self.next_periodic_at = captured_at + self.evidence_interval

    def observe(
        self,
        *,
        captured_at: float,
        screen_state: str,
        screen_confidence: float,
        layout_supported: bool,
        replay_gate_status: str,
        game_clock_seconds: int | None,
        scoreboard_ready: bool,
        map_number: int | None,
    ) -> tuple[EvidenceTrigger, ...]:
        """Consume one HUD reading and return evidence triggers for its frame."""

        if self.phase == "closed":
            return ()
        triggers: list[EvidenceTrigger] = []
        valid_draft = (
            screen_state == "draft"
            and layout_supported
            and screen_confidence >= 0.60
            and replay_gate_status != "replay"
        )
        if not self.has_started or self.phase in {"draft_candidate", "draft_started"}:
            if valid_draft and self.phase not in {"game_started", "ended_grace"}:
                if (
                    self._draft_samples
                    and captured_at - self._draft_samples[-1][0] > 5.0
                ):
                    self._draft_samples.clear()
                    self.bp_candidate_started_at = captured_at
                if not self._draft_samples:
                    self.bp_candidate_started_at = captured_at
                self._draft_samples.append((captured_at, True))
                if self.phase == "waiting_for_stream":
                    self.phase = "draft_candidate"
                if (
                    self.phase == "draft_candidate"
                    and len(self._draft_samples) >= self.draft_window_size
                    and sum(item[1] for item in self._draft_samples)
                    >= self.draft_minimum_support
                    and captured_at - self._draft_samples[0][0] >= 4.0
                ):
                    self.phase = "draft_started"
                    self.draft_started_at = captured_at
                    self.next_periodic_at = captured_at + self.evidence_interval
                    triggers.append(
                        self._capture_immediate(
                            "draft_started", captured_at, reason="bp_confirmed"
                        )
                    )
            elif self.phase == "draft_candidate":
                previous_at = self._draft_samples[-1][0] if self._draft_samples else None
                self._draft_samples.append((captured_at, False))
                if previous_at is not None and captured_at - previous_at > 5.0:
                    self._draft_samples.clear()
                    self.bp_candidate_started_at = None
                    self.phase = "waiting_for_stream"
                elif not any(item[1] for item in self._draft_samples):
                    self._draft_samples.clear()
                    self.bp_candidate_started_at = None
                    self.phase = "waiting_for_stream"

        valid_game = (
            screen_state == "game"
            and replay_gate_status == "live"
            and game_clock_seconds is not None
            and scoreboard_ready
        )
        if self.phase in {"waiting_for_stream", "draft_candidate", "draft_started"}:
            if valid_game:
                if (
                    self._game_samples
                    and captured_at - self._game_samples[-1][0] > 5.0
                ):
                    self._game_samples.clear()
                self._game_samples.append((captured_at, True))
                if (
                    len(self._game_samples) >= self.game_minimum_support
                    and sum(item[1] for item in self._game_samples)
                    >= self.game_minimum_support
                    and captured_at - self._game_samples[0][0] >= 2.0
                ):
                    self.phase = "game_started"
                    self.game_started_at = captured_at
                    triggers.append(
                        self._capture_immediate(
                            "game_started", captured_at, reason="game_confirmed"
                        )
                    )
            else:
                self._game_samples.append((captured_at, False))

        if map_number is not None:
            if (
                self.last_map_number is not None
                and map_number != self.last_map_number
                and self.phase in {"game_started", "ended_grace"}
            ):
                triggers.append(
                    self._capture_immediate(
                        "map_changed", captured_at, reason="map_transition"
                    )
                )
            self.last_map_number = map_number

        if self.phase in {"draft_started", "game_started", "ended_grace"}:
            if (
                self.next_periodic_at is not None
                and captured_at >= self.next_periodic_at
                and self.phase != "closed"
            ):
                triggers.append(self._capture_periodic(captured_at))
            if self.phase == "ended_grace" and not self._final_emitted:
                self._final_emitted = True
                triggers.append(
                    self._capture_immediate(
                        "ended_final", captured_at, reason="provider_completed"
                    )
                )
        return tuple(triggers)

    def mark_provider_complete(self, completed_at: float) -> None:
        """Enter a short grace period so delayed final frames are retained."""

        if self.phase == "closed":
            return
        if self.phase != "ended_grace":
            self.phase = "ended_grace"
            self.ended_at = completed_at
            self.close_at = completed_at + self.end_grace_seconds

    def should_close(self, now: float) -> bool:
        return (
            self.phase == "ended_grace"
            and self.close_at is not None
            and now >= self.close_at
        )

    def close(self) -> None:
        self.phase = "closed"


def load_manifest(path: str | Path) -> list[dict[str, object]]:
    """Read a bounded event manifest, ignoring incomplete trailing lines."""

    manifest = Path(path)
    if not manifest.exists():
        return []
    rows: deque[dict[str, object]] = deque(maxlen=2000)
    try:
        with manifest.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    payload = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(payload, dict):
                    rows.append(payload)
    except OSError:
        return []
    return list(rows)
