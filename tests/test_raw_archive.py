from __future__ import annotations

import gzip
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from event_intelligence.opendota import OpenDotaAdapter
from event_intelligence.raw_archive import (
    RawArchive,
    canonical_json_bytes,
    sanitize_request_identity,
)


UTC = timezone.utc


class RawArchiveTests(unittest.TestCase):
    def test_identical_json_has_one_artifact_and_distinct_observations(self) -> None:
        observations = []
        with tempfile.TemporaryDirectory() as directory:
            archive = RawArchive(Path(directory), observation_sink=observations.append)
            first = archive.archive_json(
                source="opendota",
                endpoint="/api/matches/123",
                request_identity=(
                    "https://api.opendota.com/api/matches/123"
                    "?api_key=do-not-store&project=research"
                ),
                payload_bytes=b'{"players":[],"match_id":123}',
                observed_at=datetime(2026, 7, 13, 1, 2, 3, tzinfo=UTC),
                match_id=123,
                status_code=200,
            )
            second = archive.archive_json(
                source="opendota",
                endpoint="/api/matches/123",
                request_identity="/api/matches/123?project=research&token=also-secret",
                payload_bytes=b'{ "match_id": 123, "players": [] }',
                observed_at=datetime(2026, 7, 14, 1, 17, 3, tzinfo=UTC),
                match_id=123,
                status_code=200,
                first_usable_at=datetime(2026, 7, 14, 1, 17, 4, tzinfo=UTC),
            )

            self.assertEqual(first.content_sha256, second.content_sha256)
            self.assertEqual(first.path, second.path)
            self.assertTrue(first.artifact_created)
            self.assertFalse(second.artifact_created)
            self.assertNotEqual(first.observation_id, second.observation_id)
            self.assertEqual(len(observations), 2)
            self.assertEqual(len(list(Path(directory).rglob("*.json.gz"))), 1)
            self.assertNotIn("do-not-store", first.request_identity)
            self.assertNotIn("also-secret", second.request_identity)
            self.assertEqual(
                gzip.decompress(first.path.read_bytes()),
                canonical_json_bytes({"match_id": 123, "players": []}),
            )

    def test_changed_payload_creates_a_new_content_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = RawArchive(Path(directory))
            first = archive.archive_json(
                source="opendota",
                endpoint="/api/matches/123",
                request_identity="/api/matches/123",
                payload_bytes=b'{"match_id":123,"version":null}',
                observed_at=datetime(2026, 7, 13, tzinfo=UTC),
                match_id=123,
                status_code=200,
            )
            changed = archive.archive_json(
                source="opendota",
                endpoint="/api/matches/123",
                request_identity="/api/matches/123",
                payload_bytes=b'{"match_id":123,"version":21}',
                observed_at=datetime(2026, 7, 13, 1, tzinfo=UTC),
                match_id=123,
                status_code=200,
            )

            self.assertNotEqual(first.content_sha256, changed.content_sha256)
            self.assertNotEqual(first.path, changed.path)
            self.assertEqual(len(list(Path(directory).rglob("*.json.gz"))), 2)

    def test_request_identity_removes_credentials_and_is_stable(self) -> None:
        sanitized = sanitize_request_identity(
            "https://user:password@api.opendota.com/api/matches/123"
            "?z=2&api_key=secret&token=secret-2&a=1#private"
        )
        self.assertEqual(
            sanitized,
            "https://api.opendota.com/api/matches/123",
        )
        self.assertNotIn("secret", sanitized)
        self.assertNotIn("password", sanitized)
        self.assertNotIn("private", sanitized)

        allowed = sanitize_request_identity(
            "https://api.opendota.com/api/proMatches?limit=10&offset=20"
            "&client_secret=no&auth_token=no&x-api-key=no&sig=no"
        )
        self.assertEqual(
            allowed,
            "https://api.opendota.com/api/proMatches?limit=10&offset=20",
        )

    def test_rejects_non_json_and_naive_observation_times(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = RawArchive(Path(directory))
            with self.assertRaises(ValueError):
                archive.archive_json(
                    source="opendota",
                    endpoint="/api/matches/123",
                    request_identity="/api/matches/123",
                    payload_bytes=b"not-json",
                    observed_at=datetime(2026, 7, 13),
                    match_id=123,
                    status_code=200,
                )


class FakeOpenDotaClient:
    def __init__(self, match_payload: dict | None = None) -> None:
        self.match_payload = match_payload or {"match_id": 123, "players": []}

    async def get_match(self, match_id: int) -> dict:
        return self.match_payload

    async def get_league_matches(self, league_id: int) -> list[dict]:
        return [{"match_id": 123, "leagueid": league_id}]


class OpenDotaAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_returns_canonical_bytes_and_receipt_metadata(self) -> None:
        received_at = datetime(2026, 7, 13, 2, 3, 4, tzinfo=UTC)
        adapter = OpenDotaAdapter(
            FakeOpenDotaClient({"players": [], "match_id": 123}),
            clock=lambda: received_at,
        )

        response = await adapter.fetch_match(123)

        self.assertEqual(response.payload["match_id"], 123)
        self.assertEqual(
            response.canonical_json,
            b'{"match_id":123,"players":[]}',
        )
        self.assertEqual(response.endpoint, "/api/matches/123")
        self.assertEqual(response.request_identity, "/api/matches/123")
        self.assertEqual(response.received_at, received_at)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, "application/json")
        self.assertEqual(len(response.content_sha256), 64)

    async def test_adapter_preserves_identity_mismatch_for_downstream_audit(self) -> None:
        adapter = OpenDotaAdapter(FakeOpenDotaClient({"match_id": 999}))
        response = await adapter.fetch_match(123)

        self.assertEqual(response.endpoint, "/api/matches/123")
        self.assertEqual(response.payload["match_id"], 999)

    async def test_adapter_supports_league_discovery(self) -> None:
        adapter = OpenDotaAdapter(FakeOpenDotaClient())
        response = await adapter.fetch_league_matches(19543)
        self.assertEqual(response.payload, [{"match_id": 123, "leagueid": 19543}])
        self.assertEqual(response.endpoint, "/api/leagues/19543/matches")


if __name__ == "__main__":
    unittest.main()
