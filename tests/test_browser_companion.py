from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from live_betting.browser_auth import (
    PairingManager,
    PairingStateStore,
    RequestAuthenticator,
    sign_request,
)
from live_betting.browser_companion import CompanionConfig, create_app
from live_betting.browser_contract import payload_sha256


ORIGIN = "chrome-extension://" + "a" * 32


class FakeProtector:
    def protect(self, plaintext: bytes) -> bytes:
        return bytes(value ^ 0x5A for value in plaintext)

    def unprotect(self, ciphertext: bytes) -> bytes:
        return bytes(value ^ 0x5A for value in ciphertext)


def browser_event(event_id: str = "1" * 64) -> dict:
    payload = {
        "code": 200,
        "result": {
            "id": "38407985",
            "game_id": 151,
            "match_name": "Alpha - VS - Beta",
            "team": [
                {"team_id": 1, "team_name": "Alpha", "pos": 1},
                {"team_id": 2, "team_name": "Beta", "pos": 2},
            ],
            "odds": [
                {
                    "odds_id": "win-a",
                    "odds_group_id": "g1",
                    "odds": 2.8,
                    "status": "open",
                    "group_short_name": "Winner",
                    "tag": "win",
                    "match_stage": "r1",
                    "team_id": 1,
                },
                {
                    "odds_id": "win-b",
                    "odds_group_id": "g1",
                    "odds": 1.45,
                    "status": "open",
                    "group_short_name": "Winner",
                    "tag": "win",
                    "match_stage": "r1",
                    "team_id": 2,
                },
            ],
        },
    }
    return {
        "schema_version": 1,
        "event_id": event_id,
        "capture_session_id": "2" * 32,
        "captured_at_utc": "2026-07-13T00:00:00.000Z",
        "page_origin": "https://www.ray086.com",
        "page_path": "/esports",
        "source_path": "/v2/odds",
        "transport": "fetch",
        "event_type": "odds",
        "raybet_match_id": "38407985",
        "game_id": 151,
        "payload": payload,
        "payload_hash": payload_sha256(payload),
        "payload_bytes": len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()),
        "capture_reason": None,
        "extension_version": "0.1.0",
    }


class BrowserCompanionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        state_store = PairingStateStore(root / "pair.json", FakeProtector())
        self.pairing = PairingManager(state_store)
        self.code = self.pairing.issue_code()
        config = CompanionConfig(database=root / "test.db", pairing_state=root / "pair.json")
        self.app = create_app(
            config,
            pairing=self.pairing,
            authenticator=RequestAuthenticator(self.pairing),
        )
        self.client = TestClient(self.app)
        paired = self.client.post(
            "/v1/pair",
            headers={"Origin": ORIGIN},
            json={"code": self.code, "extension_version": "0.1.0"},
        )
        self.assertEqual(paired.status_code, 200)
        self.secret = base64.b64decode(paired.json()["secret"])

    def tearDown(self) -> None:
        self.client.close()
        self.temp.cleanup()

    def auth_headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        nonce = hashlib.sha256(f"{path}:{timestamp}:{time.time_ns()}".encode()).hexdigest()[:32]
        return {
            "Origin": ORIGIN,
            "X-Dota-Extension-Version": "0.1.0",
            "X-Dota-Timestamp": timestamp,
            "X-Dota-Nonce": nonce,
            "X-Dota-Signature": sign_request(
                self.secret, timestamp, nonce, method, path, body
            ),
        }

    def test_health_and_origin_limited_preflight(self) -> None:
        self.assertEqual(self.client.get("/health").json(), {"protocol_version": 1, "state": "ok"})
        response = self.client.options("/v1/events", headers={"Origin": ORIGIN})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["access-control-allow-origin"], ORIGIN)
        denied = self.client.options(
            "/v1/events", headers={"Origin": "chrome-extension://" + "b" * 32}
        )
        self.assertEqual(denied.status_code, 403)

    def test_authenticated_batch_is_idempotent_and_status_has_no_payload(self) -> None:
        event = browser_event()
        body = json.dumps([event], separators=(",", ":")).encode()
        first = self.client.post(
            "/v1/events",
            content=body,
            headers={**self.auth_headers("POST", "/v1/events", body), "Content-Type": "application/json"},
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["results"][0]["status"], "accepted")
        second = self.client.post(
            "/v1/events",
            content=body,
            headers={**self.auth_headers("POST", "/v1/events", body), "Content-Type": "application/json"},
        )
        self.assertEqual(second.json()["results"][0]["status"], "duplicate")

        headers = self.auth_headers("GET", "/v1/status", b"")
        status = self.client.get("/v1/status", headers=headers)
        self.assertEqual(status.status_code, 200)
        serialized = status.text.lower()
        self.assertNotIn("payload", serialized)
        self.assertEqual(status.json()["duplicate_count"], 1)
        self.assertEqual(status.json()["known_dota_match_count"], 1)

        post_headers = self.auth_headers("POST", "/v1/status", b"")
        post_status = self.client.post("/v1/status", headers=post_headers)
        self.assertEqual(post_status.status_code, 200)
        self.assertEqual(post_status.json()["known_dota_match_count"], 1)

    def test_bad_auth_and_forbidden_batch_are_rejected(self) -> None:
        body = b"[]"
        response = self.client.post(
            "/v1/events",
            content=body,
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 401)

        event = browser_event()
        event["payload"]["authorization_token"] = "fixture-sensitive"
        forbidden = json.dumps([event], separators=(",", ":")).encode()
        response = self.client.post(
            "/v1/events",
            content=forbidden,
            headers={
                **self.auth_headers("POST", "/v1/events", forbidden),
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "forbidden_field")
        self.assertNotIn("fixture-sensitive", response.text)

    def test_authenticated_event_batch_requires_json_content_type(self) -> None:
        body = json.dumps([browser_event()], separators=(",", ":")).encode()
        response = self.client.post(
            "/v1/events",
            content=body,
            headers={
                **self.auth_headers("POST", "/v1/events", body),
                "Content-Type": "text/plain",
            },
        )
        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.json()["code"], "unsupported_media_type")


if __name__ == "__main__":
    unittest.main()
