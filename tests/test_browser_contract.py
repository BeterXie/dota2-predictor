from __future__ import annotations

import json
import unittest
from copy import deepcopy
from pathlib import Path

from pydantic import ValidationError

from live_betting.browser_contract import (
    BrowserEvent,
    canonical_json,
    find_forbidden_batch_key,
    payload_sha256,
)


def valid_event() -> dict:
    payload = {"result": {"id": 38407985, "game_id": 151, "odds": []}}
    return {
        "schema_version": 1,
        "event_id": "a" * 64,
        "capture_session_id": "b" * 32,
        "captured_at_utc": "2026-07-13T08:12:34.567Z",
        "page_origin": "https://www.ray086.com",
        "page_path": "/sports/esports",
        "source_path": "/v2/odds",
        "transport": "fetch",
        "event_type": "odds",
        "raybet_match_id": "38407985",
        "game_id": 151,
        "payload": payload,
        "payload_hash": payload_sha256(payload),
        "payload_bytes": len(json.dumps(payload, separators=(",", ":")).encode()),
        "capture_reason": None,
        "extension_version": "0.1.0",
    }


class BrowserContractTests(unittest.TestCase):
    def parse(self, value: dict) -> BrowserEvent:
        return BrowserEvent.model_validate_json(json.dumps(value))

    def test_canonical_json_matches_extension_rfc8785_vectors(self) -> None:
        path = (
            Path(__file__).resolve().parents[1]
            / "edge-extension"
            / "tests"
            / "fixtures"
            / "canonical-vectors.json"
        )
        for vector in json.loads(path.read_text(encoding="utf-8")):
            with self.subTest(name=vector["name"]):
                self.assertEqual(
                    canonical_json(vector["value"]).decode("utf-8"),
                    vector["canonical"],
                )

    def test_accepts_strict_schema_v1_event(self) -> None:
        event = self.parse(valid_event())
        self.assertEqual(event.game_id, 151)
        self.assertEqual(
            event.captured_at_utc.isoformat(), "2026-07-13T08:12:34.567000+00:00"
        )

    def test_rejects_unknown_envelope_field_and_future_schema(self) -> None:
        unknown = valid_event()
        unknown["extra"] = True
        with self.assertRaises(ValidationError):
            self.parse(unknown)
        future = valid_event()
        future["schema_version"] = 2
        with self.assertRaises(ValidationError):
            self.parse(future)

    def test_rejects_hash_mismatch_and_forbidden_nested_field(self) -> None:
        mismatch = valid_event()
        mismatch["payload_hash"] = "0" * 64
        with self.assertRaises(ValidationError):
            self.parse(mismatch)
        forbidden = valid_event()
        forbidden["payload"]["result"]["wallet_balance"] = 100
        forbidden["payload_hash"] = payload_sha256(forbidden["payload"])
        with self.assertRaises(ValidationError):
            self.parse(forbidden)

    def test_backend_rejects_all_extension_identity_and_prototype_keys(self) -> None:
        keys = (
            "persistent_client",
            "visitorId",
            "browser-id",
            "machine_id",
            "installId",
            "__proto__",
            "prototype",
            "constructor",
        )
        for key in keys:
            with self.subTest(key=key):
                event = valid_event()
                event["payload"]["result"][key] = "redacted"
                event["payload_hash"] = payload_sha256(event["payload"])
                with self.assertRaises(ValidationError):
                    self.parse(event)
                self.assertEqual(find_forbidden_batch_key([event]), key)

    def test_batch_security_scan_ignores_legitimate_session_envelope(self) -> None:
        event = valid_event()
        self.assertIsNone(find_forbidden_batch_key([event]))
        event["authorization_token"] = "redacted"
        self.assertEqual(find_forbidden_batch_key([event]), "authorization_token")
        del event["authorization_token"]
        event["constructor"] = "redacted"
        self.assertEqual(find_forbidden_batch_key([event]), "constructor")

    def test_only_dota_and_metadata_only_unknown_are_accepted(self) -> None:
        wrong_game = valid_event()
        wrong_game["game_id"] = 1
        with self.assertRaises(ValidationError):
            self.parse(wrong_game)
        unknown = deepcopy(valid_event())
        unknown.update(
            {
                "event_type": "unknown",
                "game_id": None,
                "raybet_match_id": None,
                "payload": {},
                "payload_hash": payload_sha256({}),
                "capture_reason": "unknown_endpoint",
            }
        )
        self.parse(unknown)

    def test_manual_control_is_always_untrusted_diagnostic(self) -> None:
        event = valid_event()
        event.update({"event_type": "manual_control", "source_path": "/page-state"})
        with self.assertRaises(ValidationError):
            self.parse(event)
        event["capture_reason"] = "diagnostic_untrusted"
        self.parse(event)

    def test_oversized_sanitized_hash_is_format_checked_but_not_recomputed(
        self,
    ) -> None:
        event = valid_event()
        event.update(
            {
                "payload": {},
                "payload_hash": "c" * 64,
                "payload_bytes": 300_000,
                "capture_reason": "payload_too_large",
            }
        )
        parsed = self.parse(event)
        self.assertEqual(parsed.payload_hash, "c" * 64)


if __name__ == "__main__":
    unittest.main()
