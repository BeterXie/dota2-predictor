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
    current_markets,
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
        updated_at: datetime = NOW,
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
            updated_at,
        )
        self.store.connection.commit()

    def add_winner_pair(
        self,
        observed_at: datetime,
        one: float,
        two: float,
        *,
        period: str = "map_1",
        status: int = 5,
    ) -> None:
        for odds_id, side, price in (
            (f"winner-{period}-one", "team_one", one),
            (f"winner-{period}-two", "team_two", two),
        ):
            self.store.insert_odds(
                OddsSnapshot(
                    "match-1",
                    odds_id,
                    f"winner-{period}",
                    observed_at,
                    price,
                    status,
                    Market("winner", period, side, None, side, True),
                )
            )
        self.store.connection.commit()

    def add_winner_response(
        self,
        observed_at: datetime,
        one: float,
        two: float | None,
        *,
        observation_key: str,
        period: str = "map_1",
        status: int = 1,
    ) -> None:
        snapshots = [
            OddsSnapshot(
                "match-1",
                f"winner-{period}-one",
                f"winner-{period}",
                observed_at,
                one,
                status,
                Market("winner", period, "team_one", None, "team_one", True),
            )
        ]
        if two is not None:
            snapshots.append(
                OddsSnapshot(
                    "match-1",
                    f"winner-{period}-two",
                    f"winner-{period}",
                    observed_at,
                    two,
                    status,
                    Market("winner", period, "team_two", None, "team_two", True),
                )
            )
        self.store.store_odds_observation(
            source="direct",
            observation_key=observation_key,
            source_event_id=None,
            raybet_match_id="match-1",
            observed_at=observed_at,
            normalized_state_hash="same-semantic-state",
            snapshots=snapshots,
        )

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

    def test_optional_unconfigured_mail_is_not_counted_as_an_abnormal_process(self) -> None:
        for component in ("raybet_worker", "shadow_worker"):
            record_health(
                self.store.connection,
                component,
                "healthy",
                heartbeat_at=NOW,
                success_at=NOW,
            )
        record_health(
            self.store.connection,
            "mail",
            "degraded",
            heartbeat_at=NOW,
            error_at=NOW,
            error="configuration_missing",
        )
        record_health(
            self.store.connection,
            "mail_worker",
            "degraded",
            heartbeat_at=NOW - timedelta(days=1),
            error_at=NOW - timedelta(days=1),
            error="configuration_missing",
        )

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        self.assertEqual(snapshot["summary"]["unhealthy_components"], 0)
        mail = next(item for item in snapshot["health"] if item["component"] == "mail")
        self.assertEqual(mail["status"], "degraded")
        self.assertEqual(mail["last_error"], "configuration_missing")

    def test_non_optional_worker_health_is_counted(self) -> None:
        for component in ("raybet_worker", "shadow_worker"):
            record_health(
                self.store.connection,
                component,
                "healthy",
                heartbeat_at=NOW,
                success_at=NOW,
            )
        record_health(
            self.store.connection,
            "vision_worker",
            "unhealthy",
            heartbeat_at=NOW,
            error_at=NOW,
            error="capture_failed",
        )

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        self.assertEqual(snapshot["summary"]["unhealthy_components"], 1)

    def test_snapshot_keeps_unconfirmed_matches_and_marks_missing_readiness(self) -> None:
        self.add_match()

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        self.assertEqual(len(snapshot["matches"]), 1)
        match = snapshot["matches"][0]
        self.assertEqual(match["raybet_match_id"], "match-1")
        self.assertEqual(match["lifecycle"], "degraded")
        self.assertEqual(match["readiness"]["odds"]["status"], "missing")
        self.assertEqual(match["readiness"]["mapping"]["status"], "missing")

    def test_provider_status_two_is_live_only_while_fresh(self) -> None:
        self.add_match(status=2)

        fresh = build_monitor_snapshot(self.store.connection, now=NOW)
        stale = build_monitor_snapshot(
            self.store.connection, now=NOW + timedelta(seconds=91)
        )

        self.assertEqual(fresh["matches"][0]["lifecycle"], "live")
        self.assertEqual(stale["matches"][0]["lifecycle"], "degraded")

    def test_provider_status_five_is_ended(self) -> None:
        self.add_match(status=5)

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        self.assertEqual(snapshot["matches"][0]["lifecycle"], "ended")

    def test_upcoming_match_defaults_to_first_unsettled_map(self) -> None:
        self.add_match(scheduled_at="2026-07-15 22:00:00")
        for period, status in (("map_1", 1), ("map_2", 1), ("map_3", 4)):
            self.add_winner_pair(NOW, 2.0, 2.0, period=period, status=status)

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        self.assertEqual(snapshot["matches"][0]["winner"]["period"], "map_1")

    def test_upcoming_match_uses_map_one_when_periods_arrive_separately(self) -> None:
        self.add_match(scheduled_at="2026-07-15 22:00:00")
        self.add_winner_pair(
            NOW - timedelta(seconds=8), 2.0, 2.0, period="map_1", status=1
        )
        self.add_winner_pair(NOW, 2.0, 2.0, period="map_2", status=1)

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        winner = snapshot["matches"][0]["winner"]
        self.assertEqual(winner["period"], "map_1")
        self.assertEqual(
            winner["observed_at"], (NOW - timedelta(seconds=8)).isoformat()
        )

    def test_winner_uses_latest_complete_response_even_when_quotes_are_unchanged(self) -> None:
        self.add_match(scheduled_at="2026-07-15 22:00:00")
        first = NOW - timedelta(seconds=12)
        latest_complete = NOW - timedelta(seconds=6)
        self.add_winner_response(first, 2.0, 2.0, observation_key="response-1")
        self.add_winner_response(
            latest_complete,
            2.0,
            2.0,
            observation_key="response-2",
        )
        self.add_winner_response(
            NOW,
            2.2,
            None,
            observation_key="response-3-incomplete",
        )

        latest_snapshot = self.store.connection.execute(
            "SELECT MAX(received_at) FROM odds_snapshots WHERE raybet_match_id='match-1'"
        ).fetchone()[0]
        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        self.assertEqual(latest_snapshot, NOW.isoformat())
        winner = snapshot["matches"][0]["winner"]
        self.assertEqual(winner["observed_at"], latest_complete.isoformat())
        self.assertEqual(winner["prices"], {"team_one": 2.0, "team_two": 2.0})
        self.assertEqual(
            snapshot["matches"][0]["readiness"]["odds"],
            {
                "status": "ready",
                "observed_at": NOW.isoformat(),
                "age_seconds": 0.0,
            },
        )
        latest_markets = current_markets(self.store.connection, "match-1")
        self.assertEqual(len(latest_markets), 1)
        self.assertEqual(latest_markets[0]["side"], "team_one")
        self.assertEqual(latest_markets[0]["received_at"], NOW.isoformat())

    def test_live_match_skips_explicitly_settled_maps(self) -> None:
        self.add_match(status=2)
        for period, status in (("map_1", 5), ("map_2", 1), ("map_3", 1)):
            self.add_winner_pair(NOW, 2.0, 2.0, period=period, status=status)

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        self.assertEqual(snapshot["matches"][0]["winner"]["period"], "map_2")

    def test_closed_winner_pair_is_not_reported_as_complete(self) -> None:
        self.add_match(status=2)
        self.add_winner_pair(NOW, 2.0, 2.0, status=4)

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        self.assertFalse(snapshot["matches"][0]["winner"]["complete"])

    def test_ended_match_uses_last_settled_map_not_future_market(self) -> None:
        self.add_match(status=5)
        for period, status in (("map_1", 5), ("map_2", 5), ("map_3", 4)):
            self.add_winner_pair(NOW, 2.0, 2.0, period=period, status=status)

        snapshot = build_monitor_snapshot(self.store.connection, now=NOW)

        self.assertEqual(snapshot["matches"][0]["winner"]["period"], "map_2")

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

    def test_winner_timeline_does_not_pair_different_market_groups(self) -> None:
        self.add_match()
        for odds_id, group_id, side in (
            ("group-a-one", "group-a", "team_one"),
            ("group-b-two", "group-b", "team_two"),
        ):
            self.store.insert_odds(
                OddsSnapshot(
                    "match-1",
                    odds_id,
                    group_id,
                    NOW,
                    2.0,
                    1,
                    Market("winner", "map_1", side, None, side, True),
                )
            )
        self.store.connection.commit()

        self.assertEqual(winner_timeline(self.store.connection, "match-1"), [])

    def test_cursor_changes_only_after_monitor_data_changes(self) -> None:
        self.add_match()
        before = monitor_cursor(self.store.connection)
        self.assertEqual(before, monitor_cursor(self.store.connection))

        self.add_winner_pair(NOW, 2.0, 2.0)

        self.assertNotEqual(before, monitor_cursor(self.store.connection))

    def test_cursor_changes_for_an_unchanged_complete_transport(self) -> None:
        self.add_match()
        first = NOW - timedelta(seconds=6)
        self.add_winner_response(first, 2.0, 2.0, observation_key="response-1")
        before = monitor_cursor(self.store.connection)
        snapshot_count = self.store.connection.execute(
            "SELECT COUNT(*) FROM odds_snapshots WHERE raybet_match_id='match-1'"
        ).fetchone()[0]

        self.add_winner_response(NOW, 2.0, 2.0, observation_key="response-2")

        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM odds_snapshots WHERE raybet_match_id='match-1'"
            ).fetchone()[0],
            snapshot_count,
        )
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
