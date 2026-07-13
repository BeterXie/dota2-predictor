from __future__ import annotations

import inspect
import unittest

from live_betting.execution import DisabledExecutionAdapter, ExecutionResult


class DisabledExecutionTests(unittest.TestCase):
    def test_execute_has_only_disabled_result(self) -> None:
        result = DisabledExecutionAdapter().execute({"stake": 1})
        self.assertEqual(result, ExecutionResult())
        with self.assertRaises(TypeError):
            ExecutionResult("enabled")  # type: ignore[call-arg]

    def test_module_has_no_enable_switch_or_execution_client(self) -> None:
        source = inspect.getsource(__import__("live_betting.execution", fromlist=["*"]))
        for forbidden in ("requests", "httpx", "selenium", "playwright", "subprocess", "socket", "feature_flag"):
            self.assertNotIn(forbidden, source.casefold())


if __name__ == "__main__":
    unittest.main()
