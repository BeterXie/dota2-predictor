from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from live_betting.browser_auth import (
    AuthFailure,
    PairingManager,
    PairingStateStore,
    RequestAuthenticator,
    sign_request,
)


ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"


class FakeProtector:
    def protect(self, plaintext: bytes) -> bytes:
        return b"protected:" + plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(b"protected:"):
            raise ValueError("not protected")
        return ciphertext[len(b"protected:"):][::-1]


class BrowserAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.now = 1_720_000_000.0
        self.clock = lambda: self.now
        store = PairingStateStore(Path(self.directory.name) / "pairing.json", FakeProtector())
        self.manager = PairingManager(store, self.clock)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def pair(self) -> bytes:
        code = self.manager.issue_code()
        encoded = self.manager.pair(code, ORIGIN)
        return base64.b64decode(encoded)

    def headers(self, secret: bytes, nonce: str = "nonce_0123456789abcdef", timestamp: str | None = None) -> dict[str, str]:
        timestamp = timestamp or str(int(self.now * 1000))
        body = b"[]"
        return {
            "X-Dota-Extension-Version": "0.1.0",
            "X-Dota-Timestamp": timestamp,
            "X-Dota-Nonce": nonce,
            "X-Dota-Signature": sign_request(secret, timestamp, nonce, "POST", "/v1/events", body),
        }

    def test_pairing_code_is_one_use_and_state_is_not_plaintext(self) -> None:
        code = self.manager.issue_code()
        self.manager.pair(code, ORIGIN)
        contents = self.manager.store.path.read_text(encoding="utf-8")
        self.assertNotIn(ORIGIN, contents)
        with self.assertRaisesRegex(AuthFailure, "invalid_pairing_request|pairing_disabled"):
            self.manager.pair(code, ORIGIN)

    def test_expired_code_and_wrong_origin_have_same_failure(self) -> None:
        code = self.manager.issue_code()
        self.now += 601
        with self.assertRaisesRegex(AuthFailure, "invalid_pairing_request"):
            self.manager.pair(code, ORIGIN)
        other = PairingManager(
            PairingStateStore(Path(self.directory.name) / "other.json", FakeProtector()), self.clock
        )
        code = other.issue_code()
        with self.assertRaisesRegex(AuthFailure, "invalid_pairing_request"):
            other.pair(code, "https://example.com")

    def test_pairing_is_limited_to_five_attempts_per_minute(self) -> None:
        self.manager.issue_code()
        for _ in range(5):
            with self.assertRaisesRegex(AuthFailure, "invalid_pairing_request"):
                self.manager.pair("wrong", ORIGIN)
        with self.assertRaisesRegex(AuthFailure, "rate_limited"):
            self.manager.pair("wrong", ORIGIN)

    def test_exact_hmac_authentication_and_nonce_replay(self) -> None:
        secret = self.pair()
        auth = RequestAuthenticator(self.manager, self.clock)
        headers = self.headers(secret)
        auth.authenticate(
            headers, origin=ORIGIN, method="POST", path="/v1/events",
            body=b"[]", rate_bucket="events",
        )
        with self.assertRaisesRegex(AuthFailure, "nonce_reused"):
            auth.authenticate(
                headers, origin=ORIGIN, method="POST", path="/v1/events",
                body=b"[]", rate_bucket="events",
            )

    def test_rejects_wrong_origin_signature_and_stale_timestamp(self) -> None:
        secret = self.pair()
        auth = RequestAuthenticator(self.manager, self.clock)
        with self.assertRaisesRegex(AuthFailure, "origin_mismatch"):
            auth.authenticate(
                self.headers(secret), origin="chrome-extension://pppppppppppppppppppppppppppppppp",
                method="POST", path="/v1/events", body=b"[]", rate_bucket="events",
            )
        bad = self.headers(secret, "nonce_bad_signature_1234")
        bad["X-Dota-Signature"] = "0" * 64
        with self.assertRaisesRegex(AuthFailure, "invalid_signature"):
            auth.authenticate(
                bad, origin=ORIGIN, method="POST", path="/v1/events",
                body=b"[]", rate_bucket="events",
            )
        stale = str(int((self.now - 31) * 1000))
        with self.assertRaisesRegex(AuthFailure, "stale_request"):
            auth.authenticate(
                self.headers(secret, "nonce_stale_0123456789", stale), origin=ORIGIN,
                method="POST", path="/v1/events", body=b"[]", rate_bucket="events",
            )

    def test_event_requests_are_limited_to_120_per_minute(self) -> None:
        secret = self.pair()
        auth = RequestAuthenticator(self.manager, self.clock)
        for index in range(120):
            nonce = f"nonce_{index:016d}"
            auth.authenticate(
                self.headers(secret, nonce), origin=ORIGIN, method="POST",
                path="/v1/events", body=b"[]", rate_bucket="events",
            )
        with self.assertRaisesRegex(AuthFailure, "rate_limited"):
            auth.authenticate(
                self.headers(secret, "nonce_9999999999999999"), origin=ORIGIN,
                method="POST", path="/v1/events", body=b"[]", rate_bucket="events",
            )


if __name__ == "__main__":
    unittest.main()
