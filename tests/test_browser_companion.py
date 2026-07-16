from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from live_betting.browser_companion import MAX_BODY_BYTES, CompanionConfig, create_app
from live_betting.browser_contract import canonical_json, payload_sha256
from live_betting.health import record_health
from live_betting.storage import LiveBettingStore


ORIGIN = "chrome-extension://" + "a" * 32
VERSION_HEADERS = {
    "Origin": ORIGIN,
    "X-Dota-Extension-Version": "0.1.0",
}


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
        "payload_bytes": len(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ),
        "capture_reason": None,
        "extension_version": "0.1.0",
    }


class BrowserCompanionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.database = root / "test.db"
        self.app = create_app(
            CompanionConfig(database=self.database, extension_origin=ORIGIN)
        )
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.temp.cleanup()

    @staticmethod
    def headers(*, content_type: bool = False) -> dict[str, str]:
        headers = dict(VERSION_HEADERS)
        if content_type:
            headers["Content-Type"] = "application/json"
        return headers

    def test_health_and_origin_limited_preflight(self) -> None:
        self.assertEqual(
            self.client.get("/health").json(),
            {"protocol_version": 1, "state": "ok"},
        )
        self.assertEqual(self.client.get("/openapi.json").status_code, 404)
        response = self.client.options("/v1/events", headers={"Origin": ORIGIN})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["access-control-allow-origin"], ORIGIN)
        for denied_origin in (
            "chrome-extension://" + "b" * 32,
            "https://www.ray086.com",
        ):
            denied = self.client.options(
                "/v1/events", headers={"Origin": denied_origin}
            )
            self.assertEqual(denied.status_code, 403)

    def test_direct_batch_is_idempotent_and_status_has_no_payload(self) -> None:
        event = browser_event()
        body = json.dumps([event], separators=(",", ":")).encode()
        first = self.client.post(
            "/v1/events", content=body, headers=self.headers(content_type=True)
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["protocol_version"], 1)
        self.assertEqual(first.json()["results"][0]["status"], "accepted")
        second = self.client.post(
            "/v1/events", content=body, headers=self.headers(content_type=True)
        )
        self.assertEqual(second.json()["results"][0]["status"], "duplicate")

        status = self.client.post(
            "/v1/status", content=b"{}", headers=self.headers(content_type=True)
        )
        self.assertEqual(status.status_code, 200)
        self.assertNotIn("payload", status.text.lower())
        self.assertEqual(status.json()["duplicate_count"], 1)
        self.assertEqual(status.json()["known_dota_match_count"], 1)
        self.assertEqual(self.client.get("/v1/status").status_code, 405)

    def test_conflicting_retry_is_rejected_and_counted(self) -> None:
        first_event = browser_event()
        first = self.client.post(
            "/v1/events",
            content=json.dumps([first_event], separators=(",", ":")).encode(),
            headers=self.headers(content_type=True),
        )
        self.assertEqual(first.status_code, 200, first.text)

        conflict = browser_event()
        conflict["payload"]["result"]["odds"][0]["odds"] = 3.0
        conflict["payload_hash"] = payload_sha256(conflict["payload"])
        conflict["payload_bytes"] = len(canonical_json(conflict["payload"]))
        response = self.client.post(
            "/v1/events",
            content=json.dumps([conflict], separators=(",", ":")).encode(),
            headers=self.headers(content_type=True),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["results"][0]["status"], "rejected")
        self.assertEqual(response.json()["results"][0]["reason"], "event_id_conflict")
        status = self.client.post(
            "/v1/status", content=b"{}", headers=self.headers(content_type=True)
        )
        self.assertEqual(status.json()["rejection_count"], 1)

    def test_database_unavailable_is_stable_and_not_acknowledged(self) -> None:
        class FailingIngestor:
            def ingest(self, _store, _event):
                raise sqlite3.OperationalError("database is locked")

        app = create_app(
            CompanionConfig(database=self.database, extension_origin=ORIGIN),
            ingestor=FailingIngestor(),  # type: ignore[arg-type]
        )
        with TestClient(app) as client:
            response = client.post(
                "/v1/events",
                content=json.dumps([browser_event()], separators=(",", ":")).encode(),
                headers=self.headers(content_type=True),
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "database_unavailable")

    def test_status_requires_empty_json_and_reports_fresh_shadow(self) -> None:
        for body in (b"", b"[]", b'{"unexpected":true}', b"null", b"not-json"):
            response = self.client.post(
                "/v1/status", content=body, headers=self.headers(content_type=True)
            )
            self.assertEqual(response.status_code, 400, response.text)

        now = datetime.now(timezone.utc)
        with LiveBettingStore(self.database) as store:
            record_health(
                store.connection,
                "shadow",
                "healthy",
                heartbeat_at=now,
                success_at=now,
            )
        response = self.client.post(
            "/v1/status", content=b"{}", headers=self.headers(content_type=True)
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["shadow_strategy_active"])

    def test_non_finite_json_constants_are_rejected_as_invalid_json(self) -> None:
        for body in (b"[NaN]", b"[Infinity]", b"[-Infinity]"):
            response = self.client.post(
                "/v1/events", content=body, headers=self.headers(content_type=True)
            )
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(response.json()["code"], "invalid_json")

        response = self.client.post(
            "/v1/status", content=b"{\"value\":NaN}", headers=self.headers(content_type=True)
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["code"], "invalid_json")

    def test_origin_and_version_are_required(self) -> None:
        body = json.dumps([browser_event()], separators=(",", ":")).encode()
        for headers, expected in (
            ({"Content-Type": "application/json"}, 403),
            ({
                "Origin": "chrome-extension://" + "b" * 32,
                "Content-Type": "application/json",
                "X-Dota-Extension-Version": "0.1.0",
            }, 403),
            ({
                "Origin": "https://www.ray086.com",
                "Content-Type": "application/json",
                "X-Dota-Extension-Version": "0.1.0",
            }, 403),
            ({"Origin": ORIGIN, "Content-Type": "application/json"}, 400),
            ({
                "Origin": ORIGIN,
                "Content-Type": "application/json",
                "X-Dota-Extension-Version": "9.9.9",
            }, 400),
        ):
            response = self.client.post("/v1/events", content=body, headers=headers)
            self.assertEqual(response.status_code, expected, response.text)

        for headers, expected in (
            ({"Content-Type": "application/json"}, 403),
            ({
                "Origin": "chrome-extension://" + "b" * 32,
                "Content-Type": "application/json",
                "X-Dota-Extension-Version": "0.1.0",
            }, 403),
            ({"Origin": ORIGIN, "Content-Type": "application/json"}, 400),
            ({
                "Origin": ORIGIN,
                "Content-Type": "application/json",
                "X-Dota-Extension-Version": "9.9.9",
            }, 400),
        ):
            response = self.client.post("/v1/status", content=b"{}", headers=headers)
            self.assertEqual(response.status_code, expected, response.text)

    def test_event_version_matches_the_direct_client_version(self) -> None:
        event = browser_event()
        event["extension_version"] = "9.9.9"
        response = self.client.post(
            "/v1/events",
            content=json.dumps([event], separators=(",", ":")).encode(),
            headers=self.headers(content_type=True),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["results"][0]["reason"],
            "unsupported_extension_version",
        )

    def test_body_and_batch_boundaries(self) -> None:
        exact = self.client.post(
            "/v1/events",
            content=b" " * MAX_BODY_BYTES,
            headers=self.headers(content_type=True),
        )
        self.assertEqual(exact.status_code, 400)
        self.assertEqual(exact.json()["code"], "invalid_json")
        oversized = self.client.post(
            "/v1/events",
            content=b" " * (MAX_BODY_BYTES + 1),
            headers=self.headers(content_type=True),
        )
        self.assertEqual(oversized.status_code, 413)

        accepted_batch = [browser_event(f"{index + 10:064x}") for index in range(50)]
        accepted = self.client.post(
            "/v1/events",
            content=json.dumps(accepted_batch, separators=(",", ":")).encode(),
            headers=self.headers(content_type=True),
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(len(accepted.json()["results"]), 50)

        too_many = [browser_event(f"{index + 100:064x}") for index in range(51)]
        rejected = self.client.post(
            "/v1/events",
            content=json.dumps(too_many, separators=(",", ":")).encode(),
            headers=self.headers(content_type=True),
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.json()["code"], "invalid_batch")

    def test_forbidden_fields_and_content_type_are_rejected(self) -> None:
        event = browser_event()
        event["payload"]["authorization_token"] = "fixture-sensitive"
        forbidden = json.dumps([event], separators=(",", ":")).encode()
        response = self.client.post(
            "/v1/events",
            content=forbidden,
            headers=self.headers(content_type=True),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "forbidden_field")
        self.assertNotIn("fixture-sensitive", response.text)

        valid = json.dumps([browser_event()], separators=(",", ":")).encode()
        response = self.client.post(
            "/v1/events", content=valid, headers=self.headers()
        )
        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.json()["code"], "unsupported_media_type")

    def test_status_rate_limit_is_retained(self) -> None:
        for _ in range(60):
            response = self.client.post(
                "/v1/status", content=b"{}", headers=self.headers(content_type=True)
            )
            self.assertEqual(response.status_code, 200)
        limited = self.client.post(
            "/v1/status", content=b"{}", headers=self.headers(content_type=True)
        )
        self.assertEqual(limited.status_code, 429)

    def test_event_rate_limit_is_retained(self) -> None:
        for _ in range(120):
            response = self.client.post(
                "/v1/events", content=b"[]", headers=self.headers(content_type=True)
            )
            self.assertEqual(response.status_code, 400)
        limited = self.client.post(
            "/v1/events", content=b"[]", headers=self.headers(content_type=True)
        )
        self.assertEqual(limited.status_code, 429)

    def test_invalid_configured_origin_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "extension_origin"):
            CompanionConfig(extension_origin="chrome-extension://invalid")


if __name__ == "__main__":
    unittest.main()
