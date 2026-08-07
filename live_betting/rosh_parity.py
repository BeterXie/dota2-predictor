"""Content-addressed exact-byte storage for R.O.S.H. transport artifacts."""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


_SECRET_PATTERN = re.compile(
    rb"authorization|bearer\s+|set-cookie|cookie|password|"
    rb"(?:api[-_ ]?key)|secret|session[-_ ]?(?:id|token)|access[-_ ]?token",
    re.IGNORECASE,
)


class ArtifactError(RuntimeError):
    """Raised when exact transport bytes cannot be safely retained."""


@dataclass(frozen=True)
class ExactArtifactReceipt:
    content_sha256: str
    gzip_sha256: str
    relative_path: str
    byte_count: int


class ExactByteArtifactStore:
    """Content-addressed gzip storage that never parses or rewrites JSON."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()

    def persist(self, body: bytes) -> ExactArtifactReceipt:
        if not isinstance(body, bytes):
            raise ArtifactError("artifact body must be exact bytes")
        if _SECRET_PATTERN.search(body):
            raise ArtifactError("artifact failed credential scan")
        content_hash = hashlib.sha256(body).hexdigest()
        relative = PurePosixPath("sha256", content_hash[:2], f"{content_hash}.json.gz")
        path = self.root.joinpath(*relative.parts)
        compressed = gzip.compress(body, compresslevel=9, mtime=0)
        try:
            with self._lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists():
                    self._verify(path, compressed)
                else:
                    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
                    try:
                        temporary.write_bytes(compressed)
                        os.replace(temporary, path)
                    finally:
                        if temporary.exists():
                            temporary.unlink()
                    self._verify(path, compressed)
                gzip_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        except (OSError, EOFError, gzip.BadGzipFile) as error:
            raise ArtifactError("exact artifact persistence failed") from error
        return ExactArtifactReceipt(
            content_hash,
            gzip_hash,
            relative.as_posix(),
            len(body),
        )

    @staticmethod
    def _verify(path: Path, expected: bytes) -> None:
        stored = path.read_bytes()
        if stored != expected:
            raise ArtifactError("content-addressed artifact is not canonical")


__all__ = [
    "ArtifactError",
    "ExactArtifactReceipt",
    "ExactByteArtifactStore",
]
