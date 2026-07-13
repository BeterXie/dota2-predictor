from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from live_betting.monitor import load_pandascore_matches


class MonitorConfigTests(unittest.TestCase):
    def test_pandascore_token_does_not_enable_requests_implicitly(self) -> None:
        with (
            patch.dict("os.environ", {"PANDASCORE_TOKEN": "configured"}),
            patch("live_betting.monitor.fetch_pandascore_matches") as fetch,
        ):
            self.assertEqual(load_pandascore_matches(False), [])
        fetch.assert_not_called()

    def test_pandascore_requires_explicit_opt_in_and_token(self) -> None:
        with (
            patch.dict("os.environ", {"PANDASCORE_TOKEN": "configured"}),
            patch(
                "live_betting.monitor.fetch_pandascore_matches",
                new=Mock(return_value="awaitable"),
            ) as fetch,
            patch("live_betting.monitor.asyncio.run", return_value=[]) as run,
        ):
            self.assertEqual(load_pandascore_matches(True), [])
        fetch.assert_called_once_with("configured")
        run.assert_called_once_with("awaitable")


if __name__ == "__main__":
    unittest.main()
