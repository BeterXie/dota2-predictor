"""Permanently disabled boundary for dry-run execution checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class ExecutionResult:
    status: Literal["execution_disabled"] = field(default="execution_disabled", init=False)


class DisabledExecutionAdapter:
    def execute(self, _order: object) -> ExecutionResult:
        return ExecutionResult()
