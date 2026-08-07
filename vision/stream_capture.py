"""Reconnectable OpenCV capture for RayBet HLS streams."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(frozen=True)
class StreamFrame:
    image: np.ndarray
    captured_at: float
    source_hash: str
    sequence: int
    frame_hash: str | None = None


class HLSStreamCapture:
    def __init__(
        self,
        url: str,
        *,
        capture_factory: Callable[[str], object] = cv2.VideoCapture,
        reconnect_delay: float = 1.0,
    ) -> None:
        self.url = url
        self.source_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
        self.capture_factory = capture_factory
        self.reconnect_delay = reconnect_delay
        self._capture = None
        self._sequence = 0

    def close(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def __enter__(self) -> "HLSStreamCapture":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _open(self) -> None:
        self.close()
        capture = self.capture_factory(self.url)
        if hasattr(capture, "set"):
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._capture = capture

    def read(self, *, timeout: float = 20.0) -> StreamFrame:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._capture is None:
                self._open()
            ok, image = self._capture.read()
            if ok and image is not None and image.size:
                self._sequence += 1
                return StreamFrame(image, time.time(), self.source_hash, self._sequence)
            self.close()
            time.sleep(self.reconnect_delay)
        raise TimeoutError(f"no frame received within {timeout:.1f}s")

    def sample(
        self, *, interval: float, count: int | None = None
    ) -> Iterator[StreamFrame]:
        emitted = 0
        next_emit = 0.0
        while count is None or emitted < count:
            frame = self.read()
            now = time.monotonic()
            if now < next_emit:
                continue
            yield frame
            emitted += 1
            next_emit = now + interval


def nonblack_ratio(image: np.ndarray, threshold: int = 20) -> float:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return float((gray > threshold).mean())
