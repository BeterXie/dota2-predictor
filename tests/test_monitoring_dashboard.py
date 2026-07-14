from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from live_betting.health import record_health
from live_betting.models import Market, OddsSnapshot
from live_betting.storage import LiveBettingStore
from web import queries
from web.app import app
from web.monitoring import (
    build_monitor_snapshot,
    derive_health,
    monitor_cursor,
    winner_timeline,
)


NOW = datetime(2026, 7, 14, 14, 0, tzinfo=timezone.utc)


class MonitoringDashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "monitor.db"
        self.store = LiveBettingStore(self.database)
        self.store.init_schema()

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def add_match(
        self,
        match_id: str = "match-1",
        *,
        status: int = 1,
        scheduled_at: str = "2026-07-14 22:00:00",
    ) -> None:
        self.store.upsert_raybet_match(
            {
                "id": match_id,
                "tournament_name": "World Cup",
                "start_time": scheduled_at,
                "round": "bo3",
                "status": status,
                "team": [
                    {"id": 11, "pos": 1, "team_name": "Radiant Five"},
                    {"id": 22, "pos": 2, "team_name": "Dire Five"},
                ],
            },
            NOW,
        )
        self.store.connection.commit()

    def add_winner_pair(self, observed_at: datetime, one: float, two: float) -> None:
        for odds_id, side, price in (
            ("winner-one", "team_one", one),
            ("winner-two", "team_two", two),
        ):
            self.store.insert_odds(
                OddsSnapshot(
                    "match-1",
                    odds_id,
                    "winner-map-1",
                    observed_at,
                    price,
                    5,
                    Market("winner", "map_1", side, None, side, True),
                )
            )
        self.store.connection.commit()

    def test_stale_healthy_heartbeat_is_derived_as_unhealthy(self) -> None:
        heartbeat = NOW - timedelta(minutes=5)
        record_health(
            self.store.connection,
            "raybet_worker",
            "healthy",
            heartbeat_at=heartbeat,
            success_at=heartbeat,
        )

        health = derive_health(self.store.connection, now=NOW)

        row = next(item for item in health if item["component"] == "raybet_worker")
        self.assertEqual(row["reported_status"], "healthy")
        self.assertEqual(row["status"], "unhealthy")
        self.assertEqual(row["freshness"], "stale")
        self.assertEqual(row["age_seconds"], 300.0)

    def test_snapshot_keeps_unconfirmed_matches_and_marks_missing_readiness(self) -> None:
        self.add_match()

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        self.assertEqual(len(snapshot["matches"]), 1)
        match = snapshot["matches"][0]
        self.assertEqual(match["raybet_match_id"], "match-1")
        self.assertEqual(match["lifecycle"], "degraded")
        self.assertEqual(match["readiness"]["odds"]["status"], "missing")
        self.assertEqual(match["readiness"]["mapping"]["status"], "missing")

    def test_winner_timeline_uses_only_observed_points_and_devigged_probability(self) -> None:
        self.add_match()
        first = NOW - timedelta(seconds=12)
        second = NOW - timedelta(seconds=6)
        self.add_winner_pair(first, 2.0, 2.0)
        self.add_winner_pair(second, 4.0, 4 / 3)

        timeline = winner_timeline(self.store.connection, "match-1")

        self.assertEqual([point["observed_at"] for point in timeline], [first.isoformat(), second.isoformat()])
        self.assertEqual(timeline[0]["probabilities"], {"team_one": 0.5, "team_two": 0.5})
        self.assertAlmostEqual(timeline[1]["probabilities"]["team_one"], 0.25)
        self.assertAlmostEqual(timeline[1]["probabilities"]["team_two"], 0.75)

    def test_cursor_changes_only_after_monitor_data_changes(self) -> None:
        self.add_match()
        before = monitor_cursor(self.store.connection)
        self.assertEqual(before, monitor_cursor(self.store.connection))

        self.add_winner_pair(NOW, 2.0, 2.0)

        self.assertNotEqual(before, monitor_cursor(self.store.connection))

    def test_monitor_api_exposes_bootstrap_and_match_detail(self) -> None:
        self.add_match()
        self.add_winner_pair(NOW, 2.0, 2.0)
        previous_path = queries.DB_PATH
        queries.init_db(str(self.database))
        try:
            with TestClient(app) as client:
                bootstrap = client.get("/api/monitor/bootstrap")
                detail = client.get("/api/monitor/matches/match-1")
        finally:
            queries.init_db(previous_path)

        self.assertEqual(bootstrap.status_code, 200)
        self.assertEqual(bootstrap.json()["matches"][0]["raybet_match_id"], "match-1")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(len(detail.json()["winner_timeline"]), 1)


if __name__ == "__main__":
    unittest.main()
