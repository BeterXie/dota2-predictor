"""Immutable, content-addressed archive for source JSON responses."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_ALLOWED_QUERY_NAMES = {
    "date",
    "end_date",
    "league_id",
    "limit",
    "match_id",
    "match_type",
    "offset",
    "page",
    "start_date",
}


def canonical_json_value_bytes(value: Any) -> bytes:
    """Serialize one already-parsed JSON value without reparsing strings."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_json_bytes(value: Any) -> bytes:
    """Canonicalize a serialized JSON document or an in-memory JSON value."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = json.loads(bytes(value).decode("utf-8"))
    elif isinstance(value, str):
        value = json.loads(value)
    return canonical_json_value_bytes(value)


def _schema_shape(value: Any) -> Any:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        shapes = {_shape_json(_schema_shape(item)) for item in value}
        return {"array": [json.loads(shape) for shape in sorted(shapes)]}
    if isinstance(value, dict):
        return {
            "object": {
                str(key): _schema_shape(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        }
    raise TypeError(f"value is not JSON-compatible: {type(value).__name__}")


def _shape_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def schema_fingerprint(value: Any) -> str:
    """Hash field names and JSON types while ignoring source values."""
    shape = _shape_json(_schema_shape(value)).encode("utf-8")
    return hashlib.sha256(shape).hexdigest()


def verify_raw_artifact_file(
    path: str | Path,
    *,
    content_hash: str,
    uncompressed_bytes: int | None = None,
    compressed_bytes: int | None = None,
    expected_schema_fingerprint: str | None = None,
) -> None:
    """Recompute the complete authority of one canonical gzip artifact."""

    artifact = Path(path)
    if artifact.is_symlink() or not artifact.is_file():
        raise RuntimeError(f"raw artifact is missing or unsafe: {artifact}")
    compressed = artifact.read_bytes()
    if compressed_bytes is not None and len(compressed) != compressed_bytes:
        raise RuntimeError(f"raw artifact compressed size mismatch: {artifact}")
    try:
        canonical = gzip.decompress(compressed)
        payload = json.loads(canonical.decode("utf-8"))
    except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"corrupt raw artifact: {artifact}") from exc
    if uncompressed_bytes is not None and len(canonical) != uncompressed_bytes:
        raise RuntimeError(f"raw artifact byte count mismatch: {artifact}")
    if hashlib.sha256(canonical).hexdigest() != content_hash:
        raise RuntimeError(f"raw artifact hash mismatch: {artifact}")
    if canonical_json_value_bytes(payload) != canonical:
        raise RuntimeError(f"raw artifact is not canonical JSON: {artifact}")
    if (
        expected_schema_fingerprint is not None
        and schema_fingerprint(payload) != expected_schema_fingerprint
    ):
        raise RuntimeError(f"raw artifact schema fingerprint mismatch: {artifact}")


def sanitize_request_identity(identity: str) -> str:
    """Remove credentials, fragments, and secret query values from an identity."""
    if not isinstance(identity, str) or not identity:
        raise ValueError("request identity must be a non-empty string")
    parts = urlsplit(identity)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower().replace("-", "_") in _ALLOWED_QUERY_NAMES
    ]
    query.sort()

    netloc = ""
    if parts.hostname is not None:
        hostname = parts.hostname
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = hostname
        if parts.port is not None:
            netloc = f"{netloc}:{parts.port}"
    elif parts.netloc:
        netloc = parts.netloc.rsplit("@", 1)[-1]

    return urlunsplit(
        (parts.scheme.lower(), netloc, parts.path, urlencode(query), "")
    )


def _require_aware(value: datetime | None, field: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ArtifactReceipt:
    source: str
    endpoint: str
    request_identity: str
    match_id: int | None
    content_sha256: str
    schema_fingerprint: str
    path: Path
    byte_count: int
    compressed_byte_count: int
    artifact_created: bool
    observation_id: str
    observed_at: datetime
    source_timestamp: datetime | None
    first_usable_at: datetime | None
    status_code: int | None

    @property
    def content_version(self) -> str:
        return self.content_sha256


ObservationSink = Callable[[ArtifactReceipt], None]


class RawArchive:
    """Write canonical JSON once and emit provenance for every observation."""

    def __init__(
        self,
        root: str | Path,
        observation_sink: ObservationSink | None = None,
        *,
        cache_paths: bool = True,
    ) -> None:
        self.root = Path(root).resolve()
        self._observation_sink = observation_sink
        self._cache_paths = cache_paths
        self._known_paths: dict[tuple[str, str], Path] = {}
        self._legacy_index: dict[tuple[str, str], Path] | None = None

    def archive_json(
        self,
        *,
        source: str,
        endpoint: str,
        request_identity: str,
        payload_bytes: bytes | bytearray | memoryview | str,
        observed_at: datetime,
        match_id: int | None,
        status_code: int | None,
        source_timestamp: datetime | None = None,
        first_usable_at: datetime | None = None,
    ) -> ArtifactReceipt:
        if not _SOURCE_RE.fullmatch(source):
            raise ValueError("source must use lowercase letters, digits, '_' or '-'")
        observed_at = _require_aware(observed_at, "observed_at")  # type: ignore[assignment]
        source_timestamp = _require_aware(source_timestamp, "source_timestamp")
        first_usable_at = _require_aware(first_usable_at, "first_usable_at")
        if first_usable_at is not None and first_usable_at < observed_at:
            raise ValueError("first_usable_at cannot precede observed_at")
        if match_id is not None and (
            isinstance(match_id, bool) or not isinstance(match_id, int) or match_id <= 0
        ):
            raise ValueError("match_id must be a positive integer or None")
        if status_code is not None and (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not 100 <= status_code <= 599
        ):
            raise ValueError("status_code must be a valid HTTP status or None")

        canonical = canonical_json_bytes(payload_bytes)
        payload = json.loads(canonical)
        content_hash = hashlib.sha256(canonical).hexdigest()
        fingerprint = schema_fingerprint(payload)
        sanitized_endpoint = sanitize_request_identity(endpoint)
        sanitized_identity = sanitize_request_identity(request_identity)
        path, created = self._write_artifact(
            source=source,
            match_id=match_id,
            observed_at=observed_at,
            content_hash=content_hash,
            canonical=canonical,
        )
        observation_id = self._observation_id(
            source=source,
            endpoint=sanitized_endpoint,
            request_identity=sanitized_identity,
            content_hash=content_hash,
            observed_at=observed_at,
            match_id=match_id,
            source_timestamp=source_timestamp,
            status_code=status_code,
        )
        receipt = ArtifactReceipt(
            source=source,
            endpoint=sanitized_endpoint,
            request_identity=sanitized_identity,
            match_id=match_id,
            content_sha256=content_hash,
            schema_fingerprint=fingerprint,
            path=path,
            byte_count=len(canonical),
            compressed_byte_count=path.stat().st_size,
            artifact_created=created,
            observation_id=observation_id,
            observed_at=observed_at,
            source_timestamp=source_timestamp,
            first_usable_at=first_usable_at,
            status_code=status_code,
        )
        if self._observation_sink is not None:
            self._observation_sink(receipt)
        return receipt

    def _write_artifact(
        self,
        *,
        source: str,
        match_id: int | None,
        observed_at: datetime,
        content_hash: str,
        canonical: bytes,
    ) -> tuple[Path, bool]:
        cache_key = (source, content_hash)
        target = self.root / source / content_hash[:2] / f"{content_hash}.json.gz"
        existing = self._known_paths.get(cache_key) if self._cache_paths else None
        if existing is None and target.is_file():
            existing = target
        if existing is None and source != "raybet":
            if self._legacy_index is None:
                self._legacy_index = {}
                source_root = self.root / source
                if source_root.exists():
                    for candidate in source_root.rglob("*.json.gz"):
                        self._legacy_index.setdefault(
                            (source, candidate.name.removesuffix(".json.gz")),
                            candidate,
                        )
            existing = self._legacy_index.get(cache_key)
        if existing is not None:
            self._verify(existing, content_hash)
            if self._cache_paths:
                self._known_paths[cache_key] = existing
            return existing, False

        # New artifacts use a hash-sharded path so a hot collector never has
        # to scan an ever-growing date/match tree. Legacy paths remain
        # discoverable above for read compatibility.
        target.parent.mkdir(parents=True, exist_ok=True)
        compressed = gzip.compress(canonical, mtime=0)
        created = False
        temporary_path: Path | None = None
        try:
            # Keep the temporary basename short.  The final content-addressed
            # filename already contains the full SHA-256; repeating it in the
            # temporary prefix can push otherwise valid Windows paths beyond
            # the legacy MAX_PATH boundary before the atomic link is created.
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".tmp-", suffix=".tmp", dir=target.parent
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(compressed)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary_path, target)
            except FileExistsError:
                self._verify(target, content_hash)
            else:
                created = True
        except BaseException:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        if self._cache_paths:
            self._known_paths[cache_key] = target
        return target, created

    @staticmethod
    def _verify(path: Path, expected_hash: str) -> None:
        verify_raw_artifact_file(path, content_hash=expected_hash)

    @staticmethod
    def _observation_id(
        *,
        source: str,
        endpoint: str,
        request_identity: str,
        content_hash: str,
        observed_at: datetime,
        match_id: int | None,
        source_timestamp: datetime | None,
        status_code: int | None,
    ) -> str:
        identity = "\n".join(
            (
                source,
                endpoint,
                request_identity,
                "" if match_id is None else str(match_id),
                content_hash,
                observed_at.isoformat(),
                "" if source_timestamp is None else source_timestamp.isoformat(),
                "" if status_code is None else str(status_code),
            )
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()
