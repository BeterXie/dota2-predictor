"""One-time localhost pairing and request authentication."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import time
from collections import defaultdict, deque
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol


PAIRING_TTL_SECONDS = 600
REQUEST_WINDOW_SECONDS = 30
NONCE_TTL_SECONDS = 300
MAX_NONCES = 10_000
EXTENSION_ORIGIN_RE = re.compile(r"^chrome-extension://[a-p]{32}$")
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
SIGNATURE_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


class AuthFailure(Exception):
    def __init__(self, code: str, status_code: int = 401) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class Protector(Protocol):
    def protect(self, plaintext: bytes) -> bytes: ...
    def unprotect(self, ciphertext: bytes) -> bytes: ...


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, object]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


class DpapiProtector:
    """Protect pairing state for the current Windows user."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows DPAPI is required for companion pairing state")
        self._crypt32 = ctypes.windll.crypt32
        self._kernel32 = ctypes.windll.kernel32

    def protect(self, plaintext: bytes) -> bytes:
        source, source_buffer = _blob(plaintext)
        output = _DataBlob()
        if not self._crypt32.CryptProtectData(
            ctypes.byref(source), "Dota2Predictor", None, None, None, 1,
            ctypes.byref(output),
        ):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            self._kernel32.LocalFree(output.pbData)
            del source_buffer

    def unprotect(self, ciphertext: bytes) -> bytes:
        source, source_buffer = _blob(ciphertext)
        output = _DataBlob()
        if not self._crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output),
        ):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            self._kernel32.LocalFree(output.pbData)
            del source_buffer


@dataclass(frozen=True)
class PairingState:
    origin: str
    secret: bytes


class PairingStateStore:
    def __init__(self, path: str | Path | None = None, protector: Protector | None = None) -> None:
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        self.path = Path(path) if path else local / "Dota2Predictor" / "browser_pairing.json"
        self.protector = protector or DpapiProtector()

    def load(self) -> PairingState | None:
        if not self.path.exists():
            return None
        envelope = json.loads(self.path.read_text(encoding="utf-8"))
        if envelope.get("version") != 1 or set(envelope) != {"version", "protected_state"}:
            raise ValueError("invalid pairing state envelope")
        clear = self.protector.unprotect(base64.b64decode(envelope["protected_state"], validate=True))
        data = json.loads(clear.decode("utf-8"))
        origin = str(data["origin"])
        secret = base64.b64decode(data["secret"], validate=True)
        if not is_extension_origin(origin) or len(secret) != 32:
            raise ValueError("invalid protected pairing state")
        return PairingState(origin, secret)

    def save(self, state: PairingState) -> None:
        if not is_extension_origin(state.origin) or len(state.secret) != 32:
            raise ValueError("invalid pairing state")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        clear = json.dumps({
            "origin": state.origin,
            "secret": base64.b64encode(state.secret).decode("ascii"),
        }, separators=(",", ":")).encode("utf-8")
        envelope = json.dumps({
            "version": 1,
            "protected_state": base64.b64encode(self.protector.protect(clear)).decode("ascii"),
        }, separators=(",", ":"))
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(envelope, encoding="utf-8")
        try:
            temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        temporary.replace(self.path)

    def reset(self) -> None:
        self.path.unlink(missing_ok=True)


def is_extension_origin(origin: str | None) -> bool:
    return bool(origin and EXTENSION_ORIGIN_RE.fullmatch(origin))


class PairingManager:
    def __init__(self, store: PairingStateStore, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.clock = clock
        self._state = store.load()
        self._code_digest: bytes | None = None
        self._code_expires_at = 0.0
        self._pair_attempts: dict[str, deque[float]] = defaultdict(deque)

    @property
    def state(self) -> PairingState | None:
        return self._state

    def issue_code(self) -> str:
        if self._state is not None:
            raise AuthFailure("pairing_disabled", 409)
        code = f"{secrets.randbelow(100_000_000):08d}"
        self._code_digest = hashlib.sha256(code.encode("ascii")).digest()
        self._code_expires_at = self.clock() + PAIRING_TTL_SECONDS
        return code

    def pair(self, code: str, origin: str) -> str:
        attempts = self._pair_attempts[origin]
        now = self.clock()
        while attempts and attempts[0] <= now - 60:
            attempts.popleft()
        if len(attempts) >= 5:
            raise AuthFailure("rate_limited", 429)
        attempts.append(now)
        supplied = hashlib.sha256(code.encode("utf-8")).digest()
        valid = (
            self._state is None
            and self._code_digest is not None
            and now <= self._code_expires_at
            and hmac.compare_digest(supplied, self._code_digest)
            and is_extension_origin(origin)
        )
        if not valid:
            raise AuthFailure("invalid_pairing_request", 401)
        secret = secrets.token_bytes(32)
        state = PairingState(origin, secret)
        self.store.save(state)
        self._state = state
        self._code_digest = None
        self._code_expires_at = 0.0
        return base64.b64encode(secret).decode("ascii")

    def reset(self) -> None:
        self.store.reset()
        self._state = None
        self._code_digest = None
        self._code_expires_at = 0.0


def signature_message(timestamp: str, nonce: str, method: str, path: str, body: bytes) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{timestamp}\n{nonce}\n{method.upper()}\n{path}\n{body_hash}".encode("utf-8")


def sign_request(secret: bytes, timestamp: str, nonce: str, method: str, path: str, body: bytes) -> str:
    return hmac.new(secret, signature_message(timestamp, nonce, method, path, body), hashlib.sha256).hexdigest()


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


class RequestAuthenticator:
    def __init__(
        self, pairing: PairingManager, clock: Callable[[], float] = time.time,
        limiter: SlidingWindowRateLimiter | None = None,
    ) -> None:
        self.pairing = pairing
        self.clock = clock
        self.limiter = limiter or SlidingWindowRateLimiter(clock)
        self._nonces: dict[str, float] = {}

    def authenticate(
        self, headers: Mapping[str, str], *, origin: str | None, method: str,
        path: str, body: bytes, rate_bucket: str,
    ) -> None:
        state = self.pairing.state
        if state is None:
            raise AuthFailure("not_paired", 401)
        if origin != state.origin:
            raise AuthFailure("origin_mismatch", 403)
        normalized = {key.casefold(): value for key, value in headers.items()}
        version = normalized.get("x-dota-extension-version", "")
        timestamp = normalized.get("x-dota-timestamp", "")
        nonce = normalized.get("x-dota-nonce", "")
        signature = normalized.get("x-dota-signature", "")
        if not VERSION_RE.fullmatch(version):
            raise AuthFailure("invalid_extension_version")
        if not timestamp.isdigit() or not NONCE_RE.fullmatch(nonce) or not SIGNATURE_RE.fullmatch(signature):
            raise AuthFailure("invalid_auth_headers")
        now = self.clock()
        try:
            request_time = int(timestamp) / 1000
        except ValueError:
            raise AuthFailure("invalid_auth_headers") from None
        if abs(now - request_time) > REQUEST_WINDOW_SECONDS:
            raise AuthFailure("stale_request")
        self._purge_nonces(now)
        if nonce in self._nonces:
            raise AuthFailure("nonce_reused")
        expected = sign_request(state.secret, timestamp, nonce, method, path, body)
        if not hmac.compare_digest(signature, expected):
            raise AuthFailure("invalid_signature")
        limit = 120 if rate_bucket == "events" else 60
        if not self.limiter.allow(rate_bucket, state.origin, limit):
            raise AuthFailure("rate_limited", 429)
        self._nonces[nonce] = now
        if len(self._nonces) > MAX_NONCES:
            oldest = min(self._nonces, key=self._nonces.get)
            del self._nonces[oldest]

    def _purge_nonces(self, now: float) -> None:
        expired = [nonce for nonce, seen in self._nonces.items() if seen <= now - NONCE_TTL_SECONDS]
        for nonce in expired:
            del self._nonces[nonce]
