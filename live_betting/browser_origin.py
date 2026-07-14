"""Origin validation and rate limiting for the localhost browser companion."""

from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from collections.abc import Callable


EXTENSION_ORIGIN_RE = re.compile(r"^chrome-extension://[a-p]{32}$")


def is_extension_origin(origin: str | None) -> bool:
    return bool(origin and EXTENSION_ORIGIN_RE.fullmatch(origin))


class SlidingWindowRateLimiter:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def allow(self, bucket: str, origin: str, limit: int, window: float = 60.0) -> bool:
        now = self.clock()
        hits = self._hits[(bucket, origin)]
        while hits and hits[0] <= now - window:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True
