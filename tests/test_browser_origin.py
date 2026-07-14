from __future__ import annotations

import unittest

from live_betting.browser_origin import SlidingWindowRateLimiter, is_extension_origin


class BrowserOriginTests(unittest.TestCase):
    def test_only_chrome_extension_origins_are_well_formed(self) -> None:
        self.assertTrue(is_extension_origin("chrome-extension://" + "a" * 32))
        self.assertFalse(is_extension_origin("chrome-extension://" + "q" * 32))
        self.assertFalse(is_extension_origin("edge-extension://" + "a" * 32))
        self.assertFalse(is_extension_origin("https://www.ray086.com"))
        self.assertFalse(is_extension_origin(None))

    def test_sliding_window_limit_expires_old_hits(self) -> None:
        now = [100.0]
        limiter = SlidingWindowRateLimiter(lambda: now[0])
        self.assertTrue(limiter.allow("events", "origin", 2))
        self.assertTrue(limiter.allow("events", "origin", 2))
        self.assertFalse(limiter.allow("events", "origin", 2))
        now[0] += 60.0
        self.assertTrue(limiter.allow("events", "origin", 2))


if __name__ == "__main__":
    unittest.main()
