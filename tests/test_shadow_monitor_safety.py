from __future__ import annotations

import hashlib
import tempfile
import unittest
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from itertools import permutations
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from event_intelligence.storage import IntelligenceStorage
from live_betting.engine import price_groups
from live_betting.markets import normalized_state_hash
from live_betting.draft_authority import DraftLandmarkAuthority
from live_betting.models import Market, OddsSnapshot, ShadowOrder
from live_betting.profiles import DraftCurve, PlayerForm, TeamStyleProfile
from live_betting.profiles.draft_curve import DraftPoint
from live_betting.shadow_monitor import _observation, persist_alignments, run_once
from live_betting.storage import LiveBettingStore
from live_betting.strict_eligibility import (
    accept_strict_live_map_mapping,
    init_strict_live_eligibility_schema,
)
from live_betting.vision import VisionObservation
from tests.draft_authority_fixture import (
    make_test_vision_observation,
    seed_test_draft_authority,
)


NOW = datetime(2026, 7, 13, 4, 0, tzinfo=timezone.utc)
MISSING_VISION = Path("does-not-exist.jsonl")
EVENT_ID = "ewc-dota2-2026"
SCHEDULED_AT = "2026-07-12T12:00:00+00:00"
_CURRENT_DRAFT_AUTHORITY = object()


def raybet_metadata() -> dict[str, object]:
    return {
        "id": "match-1",
        "game_id": 151,
        "tournament_name": "Esports World Cup 2026",
        "start_time": SCHEDULED_AT,
        "round": "bo3",
        "stage": "main_event",
        "team": [
            {"pos": 1, "team_id": 1, "team_name": "One"},
            {"pos": 2, "team_id": 2, "team_name": "Two"},
        ],
    }


def mapping_evidence() -> dict[str, object]:
    return {
        "kind": "manual_cross_source_review",
        "raybet_url": "https://example.invalid/raybet/match-1",
        "official_event_url": "https://example.invalid/ewc",
        "tournament": {
            "raybet_name": "Esports World Cup 2026",
            "event_name": "Esports World Cup 2026",
        },
        "schedule": {
            "raybet_scheduled_at": SCHEDULED_AT,
            "utc_offset_minutes": 0,
            "timezone_evidence": "fixture stores an explicit UTC offset",
            "scheduled_at_utc": SCHEDULED_AT,
        },
        "stage": {
            "scope": "main_event",
            "source_url": "https://example.invalid/ewc/stage",
        },
        "team_crosswalk": {
            "team_one": {
                "raybet_team_id": 1,
                "raybet_team_name": "One",
                "canonical_team_id": 10,
                "canonical_team_name": "Canonical One",
                "source_url": "https://example.invalid/teams/one",
            },
            "team_two": {
                "raybet_team_id": 2,
                "raybet_team_name": "Two",
                "canonical_team_id": 20,
                "canonical_team_name": "Canonical Two",
                "source_url": "https://example.invalid/teams/two",
            },
        },
    }


def snapshots(at: datetime, *, status: int = 1) -> list[OddsSnapshot]:
    return [
        OddsSnapshot(
            "match-1", "winner-one", "winner-group", at, 2.8, status,
            Market("winner", "map_1", "team_one", None, "team_one", True),
            last_update="one",
        ),
        OddsSnapshot(
            "match-1", "winner-two", "winner-group", at, 1.5, status,
            Market("winner", "map_1", "team_two", None, "team_two", True),
            last_update="two",
        ),
    ]


def complete_snapshots(at: datetime, *, status: int = 1) -> list[OddsSnapshot]:
    rows = snapshots(at, status=status)
    for odds_id, group, market_type, side, line in (
        ("kh-one", "kh-group", "kill_handicap", "team_one", -5.5),
        ("kh-two", "kh-group", "kill_handicap", "team_two", 5.5),
        ("total-over", "total-group", "total_kills", "over", 50.5),
        ("total-under", "total-group", "total_kills", "under", 50.5),
        ("duration-over", "duration-group", "duration", "over", 36.5),
        ("duration-under", "duration-group", "duration", "under", 36.5),
    ):
        rows.append(
            OddsSnapshot(
                "match-1", odds_id, group, at, 1.9, status,
                Market(
                    market_type,
                    "map_1",
                    side,
                    line,
                    (
                        f"both:{side}:{line}"
                        if market_type == "total_kills"
                        else f"{side}:{line}"
                    ),
                    True,
                ),
            )
        )
    return rows


def raw_odds_payload(rows: list[OddsSnapshot]) -> dict[str, object]:
    match_ids = {row.raybet_match_id for row in rows}
    if len(match_ids) != 1:
        raise ValueError("raw fixture requires one RayBet match")
    match_id = next(iter(match_ids))
    outcomes: list[dict[str, object]] = []
    for row in rows:
        market = row.market
        item: dict[str, object] = {
            "id": row.odds_id,
            "odds_group_id": row.odds_group_id or "",
            "match_stage": "r1",
            "odds": str(row.price),
            "status": row.status,
        }
        if row.last_update is not None:
            item["last_update"] = row.last_update
        if market.side in {"team_one", "team_two"}:
            item["team_id"] = 1 if market.side == "team_one" else 2
        if market.market_type == "winner":
            item.update(group_short_name="Winner", tag="win")
        elif market.market_type == "kill_handicap":
            item.update(
                group_short_name="Kill Handicap",
                tag="hdp",
                value=str(market.line),
            )
        elif market.market_type == "total_kills":
            item.update(
                group_short_name="Total Kills",
                tag="ou",
                value=f"{str(market.side).title()} {market.line}",
            )
        elif market.market_type == "duration":
            item.update(
                group_short_name="Duration",
                tag="ou",
                value=f"{str(market.side).title()} {market.line}",
            )
        else:
            raise ValueError(f"unsupported raw fixture market: {market.market_type}")
        outcomes.append(item)
    return {
        "result": {
            "id": match_id,
            "game_id": 151,
            "team": [
                {"team_id": 1, "team_name": "One", "pos": 1},
                {"team_id": 2, "team_name": "Two", "pos": 2},
            ],
            "odds": outcomes,
        }
    }


def observation(at: datetime, *, frame: str = "frame") -> VisionObservation:
    return make_test_vision_observation(
        raybet_match_id="match-1",
        map_number=1,
        captured_at=at,
        label=frame,
    )


def reframe(
    base: VisionObservation,
    *,
    frame: str,
    **changes: object,
) -> VisionObservation:
    candidate = replace(base, **changes)
    if candidate.is_paused is not False or candidate.screen_state != "game":
        raise ValueError("test reframe supports ordinary game frames only")
    return make_test_vision_observation(
        raybet_match_id=candidate.raybet_match_id,
        map_number=candidate.map_number,  # type: ignore[arg-type]
        captured_at=candidate.captured_at,
        game_clock_seconds=candidate.game_clock_seconds or 0,
        radiant_hero_ids=candidate.radiant_hero_ids,
        dire_hero_ids=candidate.dire_hero_ids,
        radiant_team_side=candidate.radiant_team_side,
        clock_confidence=candidate.clock_confidence,
        draft_confidence=candidate.draft_confidence,
        label=frame,
    )


class ShadowMonitorSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name) / "test.db"
        with IntelligenceStorage(path) as intelligence:
            intelligence.init_schema()
            intelligence.connection.execute(
                "UPDATE event_registry SET approved_at=? WHERE event_id=?",
                ((NOW - timedelta(days=30)).isoformat(), EVENT_ID),
            )
            intelligence.connection.commit()
        self.store = LiveBettingStore(path)
        self.store.init_schema()
        self.store.connection.execute(
            "CREATE TABLE IF NOT EXISTS teams (team_id INTEGER PRIMARY KEY, name TEXT)"
        )
        self.store.connection.executemany(
            "INSERT OR IGNORE INTO teams VALUES (?, ?)",
            ((10, "Canonical One"), (20, "Canonical Two")),
        )
        init_strict_live_eligibility_schema(self.store.connection)
        self.store.connection.commit()
        self.strict_mapping_context_patch = patch.object(
            self.store,
            "_strict_mapping_context_block_reason",
            return_value=None,
        )
        self.strict_mapping_order_patch = patch.object(
            self.store,
            "_strict_mapping_block_reason_for_order",
            return_value=None,
        )
        self.strict_mapping_context_patch.start()
        self.strict_mapping_order_patch.start()
        self.addCleanup(self.strict_mapping_order_patch.stop)
        self.addCleanup(self.strict_mapping_context_patch.stop)

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def record_transport(
        self, at: datetime, *, key: str, status: int = 1,
        rows: list[OddsSnapshot] | None = None,
    ) -> list[OddsSnapshot]:
        rows = rows or snapshots(at, status=status)
        self.store.store_odds_observation(
            source="direct",
            observation_key=key,
            source_event_id=None,
            raybet_match_id="match-1",
            observed_at=at,
            normalized_state_hash=normalized_state_hash(rows),
            snapshots=rows,
            raw_payload=raw_odds_payload(rows),
        )
        return rows

    def seed_draft_authority(
        self,
        observed_at: datetime,
        *,
        label: str,
    ) -> DraftLandmarkAuthority:
        self.store.upsert_raybet_match(
            raybet_metadata(), observed_at - timedelta(minutes=2)
        )
        with patch(
            "live_betting.strict_eligibility._utc_now",
            return_value=observed_at - timedelta(seconds=30),
        ):
            mapping = accept_strict_live_map_mapping(
                self.store.connection,
                raybet_match_id="match-1",
                map_number=1,
                event_id=EVENT_ID,
                team_one_id=1,
                team_two_id=2,
                canonical_team_one_id=10,
                canonical_team_two_id=20,
                source="test_fixture",
                evidence=mapping_evidence(),
                accepted_by="tester",
                accepted_at=observed_at - timedelta(minutes=1),
            )
        self.assertEqual(mapping.mapping_id, 1)
        return seed_test_draft_authority(
            self.store.connection,
            raybet_match_id="match-1",
            map_number=1,
            strict_mapping_id=mapping.mapping_id,
            observed_at=observed_at,
            label=label,
        )

    def insert_pending(
        self,
        signaled_at: datetime,
        *,
        decision_draft_authority: object = _CURRENT_DRAFT_AUTHORITY,
        order_draft_authority: object = _CURRENT_DRAFT_AUTHORITY,
        signal_rows: list[OddsSnapshot] | None = None,
        order_transform: Callable[[ShadowOrder], ShadowOrder] | None = None,
        mutate_authority_before_order: bool = False,
        expected_decision_inserted: bool = True,
        expected_inserted: bool = True,
    ) -> ShadowOrder:
        anchor_frame = observation(
            signaled_at - timedelta(seconds=2),
            frame=f"safety-anchor:{signaled_at.isoformat()}",
        )
        signal_vision = observation(
            signaled_at,
            frame=f"safety-frame:{signaled_at.isoformat()}",
        )
        self.store.insert_vision_observation(anchor_frame)
        self.store.insert_vision_observation(signal_vision)
        current_draft_authority = self.seed_draft_authority(
            signaled_at,
            label=f"shadow-safety:{signaled_at.isoformat()}",
        )
        signal_key = f"signal:{signaled_at.isoformat()}"
        transport_rows = self.record_transport(
            signaled_at,
            key=signal_key,
            rows=signal_rows,
        )
        signal = transport_rows[0]
        market_probability = price_groups(transport_rows).get(
            signal.odds_id,
            0.5,
        )
        market = signal.market
        strategy_version = "safety-test-v1"
        input_ref = f"safety-input:{signaled_at.isoformat()}"
        identity = "|".join(
            (
                "match-1",
                "winner-one",
                signal.odds_group_id or "",
                signal.market.outcome_key,
                "winner|map_1|team_one|",
                strategy_version,
                input_ref,
                "1.0",
            )
        )
        order = ShadowOrder(
            order_key=hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32],
            raybet_match_id="match-1",
            odds_id="winner-one",
            market=market,
            signaled_at=signaled_at,
            model_probability=0.6,
            market_probability=market_probability,
            signal_price=signal.price,
            signal_transport_key=signal_key,
            signal_transport_at=signaled_at,
            expires_at=signaled_at + timedelta(seconds=15),
            signal_odds_group_id=signal.odds_group_id,
            signal_outcome_key=signal.market.outcome_key,
            signal_identity_verified=True,
        )
        if order_transform is not None:
            order = order_transform(order)
        if callable(decision_draft_authority):
            decision_draft_authority = decision_draft_authority(
                current_draft_authority
            )
        if callable(order_draft_authority):
            order_draft_authority = order_draft_authority(current_draft_authority)
        if decision_draft_authority is _CURRENT_DRAFT_AUTHORITY:
            decision_draft_authority = current_draft_authority
        if order_draft_authority is _CURRENT_DRAFT_AUTHORITY:
            order_draft_authority = current_draft_authority
        decision_inputs: dict[str, object] = {
            "strict_live_eligibility": {
                "mapping_refs": {"strict_mapping_id": 1}
            },
            "vision": {
                "captured_at": signaled_at.isoformat(),
                "source_frame_ref": signal_vision.source_frame_ref,
                "game_clock_seconds": 600,
            },
            "quality": {"aggregate": 0.8},
            "draft_landmark": {
                "model_version": "safety-model-v1",
                "model_kind": "pure_draft",
                "model_hash": "a" * 64,
            },
        }
        if isinstance(decision_draft_authority, DraftLandmarkAuthority):
            decision_inputs["draft_authority"] = asdict(
                decision_draft_authority
            )
        decision_inserted = self.store.insert_decision(
                SimpleNamespace(
                    decision_key=f"safety-decision:{signaled_at.isoformat()}",
                    raybet_match_id="match-1",
                    map_number=1,
                    decided_at=signaled_at,
                    underdog_side="team_one",
                    market_probability=market_probability,
                    model_probability=0.6,
                    edge=0.1,
                    data_quality=0.8,
                    eligible=True,
                    reason="eligible",
                    contributions={
                        "__inputs__": decision_inputs
                    },
                    input_ref=input_ref,
                    strategy_version=strategy_version,
                ),
                draft_authority=decision_draft_authority,
                vision_observation=signal_vision,
                vision_transport_key=signal_key,
        )
        self.assertEqual(decision_inserted, expected_decision_inserted)
        if mutate_authority_before_order:
            self.store.connection.execute(
                """UPDATE draft_authority_revisions
                      SET authority_revision=authority_revision + 1
                    WHERE singleton=1"""
            )
            self.store.connection.commit()
        inserted = (
            self.store.insert_map_order(
                order,
                1,
                strict_mapping_id=1,
                draft_authority=order_draft_authority,
            )
            if decision_inserted
            else False
        )
        self.assertEqual(inserted, expected_inserted)
        self.pending_order_key = order.order_key
        return order

    def insert_raw_vision_frame(
        self,
        captured_at: str,
        *,
        source_frame_ref: str,
        game_clock_seconds: int = 601,
    ) -> None:
        self.store.connection.execute(
            """INSERT INTO vision_observations
               (raybet_match_id, map_number, captured_at, game_clock_seconds,
                is_paused, radiant_hero_ids, dire_hero_ids,
                radiant_team_side, clock_confidence, draft_confidence,
                source_frame_ref, screen_state, confirmed)
               VALUES ('match-1', 1, ?, ?, 0, '[1,2,3,4,5]',
                       '[6,7,8,9,10]', 'team_one', 0.95, 0.95, ?, 'game', 1)""",
            (captured_at, game_clock_seconds, source_frame_ref),
        )

    def test_order_rejects_invalid_decision_draft_authority(self) -> None:
        invalid_authorities = (
            ("missing", None),
            (
                "wrong_curve",
                lambda authority: replace(
                    authority,
                    curve_key="0" * 64,
                    source_ref=f"prospective-draft:{'0' * 64}",
                ),
            ),
            (
                "altered_probability",
                lambda authority: replace(
                    authority,
                    radiant_probability=authority.radiant_probability - 0.1,
                ),
            ),
            (
                "wrong_mapping",
                lambda authority: replace(authority, strict_mapping_id=2),
            ),
        )
        for offset, (case, authority) in enumerate(invalid_authorities):
            with self.subTest(case=case):
                order = self.insert_pending(
                    NOW + timedelta(seconds=offset),
                    decision_draft_authority=authority,
                    expected_decision_inserted=False,
                    expected_inserted=False,
                )
                self.assertIsNone(
                    self.store.connection.execute(
                        "SELECT 1 FROM shadow_orders WHERE order_key=?",
                        (order.order_key,),
                    ).fetchone()
                )

    def test_verified_vision_authority_is_unique_per_decision(self) -> None:
        self.insert_pending(NOW)

        count = self.store.connection.execute(
            """SELECT COUNT(*)
                 FROM verified_strategy_decision_vision_authority
                WHERE decision_key=?""",
            (f"safety-decision:{NOW.isoformat()}",),
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_same_instant_offset_alias_invalidates_verified_decision_view(
        self,
    ) -> None:
        self.insert_pending(NOW)
        same_instant = NOW.astimezone(timezone(timedelta(hours=8))).isoformat()

        self.insert_raw_vision_frame(
            same_instant,
            source_frame_ref="same-instant-offset-alias",
        )

        self.assertEqual(
            self.store.connection.execute(
                """SELECT COUNT(*)
                     FROM verified_strategy_decision_vision_authority
                    WHERE decision_key=?""",
                (f"safety-decision:{NOW.isoformat()}",),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.store.vision_block_reason_for_order(self.pending_order_key),
            "vision_authority_unverifiable",
        )

    def test_same_instant_offset_alias_blocks_decision_insert_trigger(self) -> None:
        self.insert_pending(NOW)
        decision = self.store.connection.execute(
            "SELECT * FROM strategy_decisions WHERE decision_key=?",
            (f"safety-decision:{NOW.isoformat()}",),
        ).fetchone()
        self.assertIsNotNone(decision)
        same_instant = NOW.astimezone(timezone(timedelta(hours=8))).isoformat()
        self.insert_raw_vision_frame(
            same_instant,
            source_frame_ref="same-instant-trigger-alias",
        )
        columns = [
            str(row[1])
            for row in self.store.connection.execute(
                "PRAGMA table_info(strategy_decisions)"
            )
        ]
        values = [decision[column] for column in columns]
        values[columns.index("decision_key")] = "same-instant-trigger-clone"

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "eligible strategy decision vision authority is required",
        ):
            self.store.connection.execute(
                f"INSERT INTO strategy_decisions ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                values,
            )

    def test_same_instant_offset_alias_blocks_python_decision_derivation(
        self,
    ) -> None:
        same_instant = NOW.astimezone(timezone(timedelta(hours=8))).isoformat()
        self.insert_raw_vision_frame(
            same_instant,
            source_frame_ref="same-instant-python-alias",
        )
        self.store.connection.commit()

        order = self.insert_pending(
            NOW,
            expected_decision_inserted=False,
            expected_inserted=False,
        )

        self.assertIsNone(
            self.store.connection.execute(
                "SELECT 1 FROM shadow_orders WHERE order_key=?",
                (order.order_key,),
            ).fetchone()
        )

    def test_one_millisecond_neighbor_is_not_a_vision_tie(self) -> None:
        neighbor_at = NOW - timedelta(milliseconds=1)
        self.insert_raw_vision_frame(
            neighbor_at.isoformat(),
            source_frame_ref="one-millisecond-earlier",
            game_clock_seconds=599,
        )
        self.store.connection.commit()

        self.insert_pending(NOW)

        self.assertEqual(
            self.store.connection.execute(
                """SELECT COUNT(*)
                     FROM verified_strategy_decision_vision_authority
                    WHERE decision_key=?""",
                (f"safety-decision:{NOW.isoformat()}",),
            ).fetchone()[0],
            1,
        )

    def test_malformed_late_frame_time_fails_closed(self) -> None:
        self.insert_pending(NOW)

        self.insert_raw_vision_frame(
            "not-a-timestamp",
            source_frame_ref="malformed-time",
        )

        self.assertEqual(
            self.store.connection.execute(
                """SELECT COUNT(*)
                     FROM verified_strategy_decision_vision_authority
                    WHERE decision_key=?""",
                (f"safety-decision:{NOW.isoformat()}",),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.store.vision_block_reason_for_order(self.pending_order_key),
            "vision_authority_unverifiable",
        )

    def test_decision_rejects_incomplete_or_ambiguous_winner_authority(self) -> None:
        def missing_side(at: datetime) -> list[OddsSnapshot]:
            return snapshots(at)[:1]

        def conflicting_group(at: datetime) -> list[OddsSnapshot]:
            rows = snapshots(at)
            return [
                *rows,
                replace(rows[0], odds_id="winner-one-conflict", price=2.7),
            ]

        def multiple_complete_groups(at: datetime) -> list[OddsSnapshot]:
            rows = snapshots(at)
            other = [
                replace(
                    row,
                    odds_id=f"{row.odds_id}-other",
                    odds_group_id="winner-group-other",
                )
                for row in rows
            ]
            return [*rows, *other]

        cases = (
            ("missing_side", missing_side),
            ("conflicting_group", conflicting_group),
            ("multiple_complete_groups", multiple_complete_groups),
        )
        for offset, (case, build_rows) in enumerate(cases):
            with self.subTest(case=case):
                signaled_at = NOW + timedelta(seconds=offset)
                self.insert_pending(
                    signaled_at,
                    signal_rows=build_rows(signaled_at),
                    expected_decision_inserted=False,
                    expected_inserted=False,
                )

    def test_response_rejects_duplicate_odds_id(self) -> None:
        rows = snapshots(NOW)
        duplicate = replace(rows[1], odds_id=rows[0].odds_id)

        with self.assertRaisesRegex(ValueError, "duplicate odds id"):
            self.record_transport(
                NOW,
                key="duplicate-odds-id",
                rows=[rows[0], duplicate],
            )

    def test_order_rejects_signal_outside_exact_winner_authority(self) -> None:
        transforms = (
            ("wrong_odds_id", lambda order: replace(order, odds_id="winner-two")),
            (
                "wrong_group",
                lambda order: replace(order, signal_odds_group_id="other-group"),
            ),
            (
                "wrong_outcome",
                lambda order: replace(
                    order,
                    market=replace(order.market, outcome_key="team_two"),
                    signal_outcome_key="team_two",
                ),
            ),
            (
                "wrong_price",
                lambda order: replace(order, signal_price=order.signal_price + 0.1),
            ),
            (
                "wrong_probability",
                lambda order: replace(
                    order,
                    market_probability=order.market_probability + 0.01,
                ),
            ),
        )
        for offset, (case, transform) in enumerate(transforms):
            with self.subTest(case=case):
                order = self.insert_pending(
                    NOW + timedelta(seconds=offset),
                    order_transform=transform,
                    expected_inserted=False,
                )
                self.assertIsNone(
                    self.store.connection.execute(
                        "SELECT 1 FROM shadow_orders WHERE order_key=?",
                        (order.order_key,),
                    ).fetchone()
                )

    def test_order_rejects_invalid_caller_draft_authority(self) -> None:
        invalid_authorities = (
            (
                "wrong_curve",
                lambda authority: replace(
                    authority,
                    curve_key="0" * 64,
                    source_ref=f"prospective-draft:{'0' * 64}",
                ),
            ),
            (
                "altered_probability",
                lambda authority: replace(
                    authority,
                    radiant_probability=authority.radiant_probability - 0.1,
                ),
            ),
            (
                "wrong_revision_tuple",
                lambda authority: (
                    authority.authority_revision + 1,
                    authority.dependency_revision,
                ),
            ),
            (
                "malformed_revision_tuple",
                lambda authority: (
                    str(authority.authority_revision),
                    authority.dependency_revision,
                ),
            ),
        )
        for offset, (case, authority) in enumerate(invalid_authorities):
            with self.subTest(case=case):
                order = self.insert_pending(
                    NOW + timedelta(seconds=offset),
                    order_draft_authority=authority,
                    expected_inserted=False,
                )
                self.assertIsNone(
                    self.store.connection.execute(
                        "SELECT 1 FROM shadow_orders WHERE order_key=?",
                        (order.order_key,),
                    ).fetchone()
                )

    def test_order_uses_persisted_authority_when_caller_omits_it(self) -> None:
        order = self.insert_pending(NOW, order_draft_authority=None)

        row = self.store.connection.execute(
            """SELECT draft_curve_key, draft_landmark_key, draft_deployment_key
                 FROM shadow_orders WHERE order_key=?""",
            (order.order_key,),
        ).fetchone()
        decision = self.store.connection.execute(
            """SELECT draft_curve_key, draft_landmark_key, draft_deployment_key
                 FROM strategy_decisions
                WHERE decision_key='safety-decision:2026-07-13T04:00:00+00:00'"""
        ).fetchone()
        self.assertEqual(tuple(row), tuple(decision))

    def test_order_rejects_unsupported_caller_authority_type(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "draft_authority must be exact authority or revisions",
        ):
            self.insert_pending(NOW, order_draft_authority="invalid")

    def test_order_rejects_caller_authority_that_differs_from_decision(self) -> None:
        order = self.insert_pending(
            NOW,
            order_draft_authority=lambda authority: replace(
                authority,
                curve_key="f" * 64,
                source_ref=f"prospective-draft:{'f' * 64}",
            ),
            expected_inserted=False,
        )

        self.assertIsNone(
            self.store.connection.execute(
                "SELECT 1 FROM shadow_orders WHERE order_key=?",
                (order.order_key,),
            ).fetchone()
        )

    def test_order_rechecks_draft_authority_inside_write_transaction(self) -> None:
        order = self.insert_pending(
            NOW,
            mutate_authority_before_order=True,
            expected_inserted=False,
        )

        self.assertIsNone(
            self.store.connection.execute(
                "SELECT 1 FROM shadow_orders WHERE order_key=?",
                (order.order_key,),
            ).fetchone()
        )

    def test_pending_fill_is_processed_without_any_vision(self) -> None:
        self.insert_pending(NOW)
        self.record_transport(NOW + timedelta(seconds=2), key="candidate")

        result = run_once(
            self.store, Mock(), MISSING_VISION,
            now=NOW + timedelta(days=2),
        )

        self.assertEqual(result["status"], "shadow_filled")
        row = self.store.connection.execute(
            "SELECT status, filled_at FROM shadow_orders WHERE order_key=?",
            (self.pending_order_key,),
        ).fetchone()
        self.assertEqual(tuple(row), ("filled", (NOW + timedelta(seconds=2)).isoformat()))

    def test_pending_rejection_is_processed_without_fresh_vision(self) -> None:
        self.insert_pending(NOW)
        self.record_transport(NOW + timedelta(seconds=2), key="closed", status=5)

        result = run_once(
            self.store, Mock(), MISSING_VISION,
            now=NOW + timedelta(days=2),
        )

        self.assertEqual(result["status"], "shadow_rejected")
        row = self.store.connection.execute(
            "SELECT status, rejection_reason FROM shadow_orders WHERE order_key=?",
            (self.pending_order_key,),
        ).fetchone()
        self.assertEqual(tuple(row), ("rejected", "market_closed"))

    def test_pending_terminal_updates_roll_back_together(self) -> None:
        self.insert_pending(NOW)
        self.record_transport(NOW + timedelta(seconds=2), key="candidate")

        with (
            patch.object(
                self.store, "update_map_attempt", side_effect=RuntimeError("injected")
            ),
            self.assertRaisesRegex(RuntimeError, "injected"),
        ):
            run_once(self.store, Mock(), MISSING_VISION, now=NOW + timedelta(seconds=3))

        order = self.store.connection.execute(
            "SELECT status, filled_at FROM shadow_orders WHERE order_key=?",
            (self.pending_order_key,),
        ).fetchone()
        attempt = self.store.connection.execute(
            "SELECT status FROM shadow_map_attempts WHERE order_key=?",
            (self.pending_order_key,),
        ).fetchone()
        self.assertEqual(tuple(order), ("pending", None))
        self.assertEqual(attempt["status"], "pending")

    def test_stale_latest_transport_cannot_reach_strategy(self) -> None:
        self.store.insert_vision_observation(observation(NOW))
        self.record_transport(NOW + timedelta(seconds=1), key="old")
        strategy = Mock()

        result = run_once(
            self.store, strategy, MISSING_VISION,
            now=NOW + timedelta(seconds=17),
        )

        self.assertEqual(result["status"], "waiting_for_fresh_odds")
        strategy.evaluate.assert_not_called()

    def test_persisted_invalidation_cannot_be_reconfirmed_from_payload_fields(self) -> None:
        frame = observation(NOW)
        self.store.insert_vision_observation(frame)
        self.store.connection.execute(
            "UPDATE vision_observations SET confirmed=0 WHERE source_frame_ref=?",
            (frame.source_frame_ref,),
        )
        row = self.store.connection.execute(
            "SELECT * FROM vision_observations WHERE source_frame_ref=?",
            (frame.source_frame_ref,),
        ).fetchone()
        self.assertFalse(_observation(row).is_confirmed)

    def test_invalid_confirmed_payload_cannot_create_initial_anchor(self) -> None:
        invalid_frames = (
            SimpleNamespace(
                raybet_match_id="match-1",
                map_number=1,
                captured_at=NOW,
                game_clock_seconds=600,
                is_paused=False,
                radiant_hero_ids=(0, 2, 3, 4, 5),
                dire_hero_ids=(6, 7, 8, 9, 10),
                radiant_team_side="team_one",
                clock_confidence=0.95,
                draft_confidence=0.95,
                source_frame_ref="invalid-hero",
                screen_state="game",
                is_confirmed=True,
            ),
            SimpleNamespace(
                raybet_match_id="match-1",
                map_number=1,
                captured_at=NOW + timedelta(seconds=1),
                game_clock_seconds=601,
                is_paused=False,
                radiant_hero_ids=(1, 2, 3, 4, 5),
                dire_hero_ids=(6, 7, 8, 9, 10),
                radiant_team_side="team_one",
                clock_confidence=0.95,
                draft_confidence=0.95,
                source_frame_ref="  ",
                screen_state="game",
                is_confirmed=True,
            ),
        )
        for frame in invalid_frames:
            with self.subTest(source_frame_ref=frame.source_frame_ref):
                self.assertTrue(self.store.insert_vision_observation(frame))

        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM vision_draft_anchors"
            ).fetchone()[0],
            0,
        )
        rows = self.store.connection.execute(
            "SELECT confirmed FROM vision_observations ORDER BY captured_at"
        ).fetchall()
        self.assertEqual([int(row[0]) for row in rows], [0, 0])

    def test_cross_session_draft_conflict_freezes_the_map(self) -> None:
        original = observation(NOW, frame="original")
        conflicting = reframe(
            original,
            frame="conflict",
            captured_at=NOW + timedelta(seconds=1),
            radiant_hero_ids=(1, 2, 3, 4, 6),
            dire_hero_ids=(5, 7, 8, 9, 10),
        )
        after_conflict = reframe(
            original,
            frame="after-conflict",
            captured_at=NOW + timedelta(seconds=2),
        )

        self.assertTrue(self.store.insert_vision_observation(original))
        self.assertTrue(self.store.insert_vision_observation(conflicting))
        self.assertTrue(self.store.insert_vision_observation(after_conflict))

        rows = self.store.connection.execute(
            """SELECT source_frame_ref, confirmed FROM vision_observations
                 ORDER BY captured_at"""
        ).fetchall()
        self.assertEqual(
            [(str(row[0]), int(row[1])) for row in rows],
            [
                (original.source_frame_ref, 1),
                (conflicting.source_frame_ref, 0),
                (after_conflict.source_frame_ref, 0),
            ],
        )
        anchor = self.store.connection.execute(
            "SELECT status, conflict_at FROM vision_draft_anchors"
        ).fetchone()
        self.assertEqual(anchor["status"], "conflict")
        self.assertIsNotNone(anchor["conflict_at"])
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM vision_draft_conflicts"
            ).fetchone()[0],
            2,
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.store.connection.execute(
                "UPDATE vision_draft_anchors SET draft_hash=?",
                ("0" * 64,),
            )

    def test_same_draft_can_anchor_previously_unknown_team_side(self) -> None:
        unknown = replace(
            observation(NOW, frame="unknown-side"),
            radiant_team_side=None,
        )
        known = reframe(
            unknown,
            frame="known-side",
            captured_at=NOW + timedelta(seconds=1),
            radiant_team_side="team_two",
        )

        self.assertTrue(self.store.insert_vision_observation(unknown))
        self.assertTrue(self.store.insert_vision_observation(known))

        anchor = self.store.connection.execute(
            """SELECT radiant_team_side, team_side_anchored_at,
                      team_side_source_frame_ref, status
                 FROM vision_draft_anchors
                WHERE raybet_match_id='match-1' AND map_number=1"""
        ).fetchone()
        self.assertEqual(
            tuple(anchor),
            (
                "team_two",
                (NOW + timedelta(seconds=1)).isoformat(),
                known.source_frame_ref,
                "anchored",
            ),
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM vision_draft_conflicts"
            ).fetchone()[0],
            0,
        )

    def test_earlier_team_side_frame_rebases_without_time_inversion(self) -> None:
        later_unknown = replace(
            observation(NOW + timedelta(seconds=10), frame="later-unknown"),
            radiant_team_side=None,
        )
        earlier_known = replace(
            observation(NOW, frame="earlier-known"),
            radiant_team_side="team_two",
        )
        self.store.insert_vision_observation(later_unknown)
        self.store.insert_vision_observation(earlier_known)

        anchor = self.store.connection.execute(
            """SELECT radiant_team_side, team_side_anchored_at,
                      team_side_source_frame_ref, anchored_at, source_frame_ref,
                      status
                 FROM vision_draft_anchors
                WHERE raybet_match_id='match-1' AND map_number=1"""
        ).fetchone()
        self.assertEqual(
            tuple(anchor),
            (
                "team_two",
                NOW.isoformat(),
                earlier_known.source_frame_ref,
                NOW.isoformat(),
                earlier_known.source_frame_ref,
                "anchored",
            ),
        )

    def test_confirmed_team_side_reversal_creates_draft_conflict(self) -> None:
        original = observation(NOW, frame="team-one-side")
        reversed_side = reframe(
            original,
            frame="team-two-side",
            captured_at=NOW + timedelta(seconds=1),
            radiant_team_side="team_two",
        )

        self.assertTrue(self.store.insert_vision_observation(original))
        self.assertTrue(self.store.insert_vision_observation(reversed_side))

        anchor = self.store.connection.execute(
            """SELECT radiant_team_side, status, conflict_at
                 FROM vision_draft_anchors
                WHERE raybet_match_id='match-1' AND map_number=1"""
        ).fetchone()
        self.assertEqual(
            tuple(anchor),
            (
                "team_one",
                "conflict",
                (NOW + timedelta(seconds=1)).isoformat(),
            ),
        )
        conflict = self.store.connection.execute(
            """SELECT observed_radiant_team_side, reason
                 FROM vision_draft_conflicts"""
        ).fetchone()
        self.assertEqual(
            tuple(conflict),
            ("team_two", "confirmed_draft_conflict"),
        )
        self.assertEqual(
            self.store.connection.execute(
                """SELECT confirmed FROM vision_observations
                    WHERE source_frame_ref=?""",
                (reversed_side.source_frame_ref,),
            ).fetchone()[0],
            0,
        )

    def test_draft_replay_is_deterministic_across_frame_arrival_order(self) -> None:
        frames = (
            replace(
                observation(NOW, frame="draft-only"),
                radiant_team_side=None,
            ),
            replace(
                observation(NOW + timedelta(seconds=1), frame="team-side"),
                radiant_team_side="team_one",
            ),
            replace(
                observation(NOW + timedelta(seconds=2), frame="draft-conflict"),
                radiant_hero_ids=(1, 2, 3, 4, 6),
                dire_hero_ids=(5, 7, 8, 9, 10),
                radiant_team_side="team_one",
            ),
            replace(
                observation(NOW + timedelta(seconds=3), frame="side-conflict"),
                radiant_team_side="team_two",
            ),
        )
        expected = None
        for arrival_order in permutations(frames):
            with LiveBettingStore(":memory:") as replay_store:
                replay_store.init_schema()
                for frame in arrival_order:
                    replay_store.insert_vision_observation(frame)
                anchor = tuple(
                    replay_store.connection.execute(
                        """SELECT radiant_team_side, team_side_anchored_at,
                                  team_side_source_frame_ref, anchored_at,
                                  source_frame_ref, status, conflict_at
                             FROM vision_draft_anchors"""
                    ).fetchone()
                )
                conflicts = tuple(
                    tuple(row)
                    for row in replay_store.connection.execute(
                        """SELECT captured_at, source_frame_ref, reason
                             FROM vision_draft_conflicts
                            ORDER BY captured_at, source_frame_ref"""
                    )
                )
                observations = tuple(
                    tuple(row)
                    for row in replay_store.connection.execute(
                        """SELECT source_frame_ref, confirmed
                             FROM vision_observations
                            ORDER BY captured_at, source_frame_ref"""
                    )
                )
                conflict_state = replay_store._draft_conflict_state("match-1", 1)
                state = (anchor, conflicts, observations, conflict_state)
                if expected is None:
                    expected = state
                self.assertEqual(state, expected)

        self.assertEqual(
            expected,
            (
                (
                    "team_one",
                    (NOW + timedelta(seconds=1)).isoformat(),
                    frames[1].source_frame_ref,
                    NOW.isoformat(),
                    frames[0].source_frame_ref,
                    "conflict",
                    (NOW + timedelta(seconds=2)).isoformat(),
                ),
                (
                    (
                        (NOW + timedelta(seconds=2)).isoformat(),
                        frames[2].source_frame_ref,
                        "confirmed_draft_conflict",
                    ),
                    (
                        (NOW + timedelta(seconds=3)).isoformat(),
                        frames[3].source_frame_ref,
                        "confirmed_draft_conflict",
                    ),
                ),
                (
                    (frames[0].source_frame_ref, 1),
                    (frames[1].source_frame_ref, 1),
                    (frames[2].source_frame_ref, 0),
                    (frames[3].source_frame_ref, 0),
                ),
                (True, (NOW + timedelta(seconds=2)).isoformat()),
            ),
        )
    def test_draft_conflict_hides_confirmed_frames_from_live_monitor(self) -> None:
        original = observation(NOW, frame="original")
        conflicting = reframe(
            original,
            frame="conflict",
            captured_at=NOW + timedelta(seconds=1),
            radiant_hero_ids=(1, 2, 3, 4, 6),
            dire_hero_ids=(5, 7, 8, 9, 10),
        )
        self.store.insert_vision_observation(original)
        self.store.insert_vision_observation(conflicting)
        self.record_transport(NOW + timedelta(seconds=2), key="after-conflict")

        result = run_once(
            self.store, Mock(), MISSING_VISION,
            now=NOW + timedelta(seconds=3),
        )

        self.assertEqual(result["status"], "waiting_for_confirmed_vision")

    def test_draft_conflict_rejects_pending_order_before_successor_fill(self) -> None:
        original = observation(NOW - timedelta(seconds=2), frame="original")
        conflicting = reframe(
            original,
            frame="conflict",
            captured_at=NOW - timedelta(seconds=1),
            radiant_hero_ids=(1, 2, 3, 4, 6),
            dire_hero_ids=(5, 7, 8, 9, 10),
        )
        self.insert_pending(NOW)
        self.store.insert_vision_observation(original)
        self.store.insert_vision_observation(conflicting)
        self.record_transport(NOW + timedelta(seconds=2), key="successor")

        result = run_once(
            self.store, Mock(), MISSING_VISION,
            now=NOW + timedelta(seconds=3),
        )

        self.assertEqual(result["status"], "shadow_rejected")
        row = self.store.connection.execute(
            "SELECT status, rejection_reason FROM shadow_orders WHERE order_key=?",
            (self.pending_order_key,),
        ).fetchone()
        self.assertEqual(tuple(row), ("rejected", "vision_draft_conflict"))
        self.assertEqual(
            self.store.connection.execute(
                """SELECT COUNT(*) FROM vision_derived_invalidations
                   WHERE dependent_type='shadow_order' AND dependent_key=?""",
                (self.pending_order_key,),
            ).fetchone()[0],
            1,
        )

    def test_draft_conflict_after_signal_rejects_pending_order_at_fill(self) -> None:
        original = observation(NOW - timedelta(seconds=1), frame="original")
        self.store.insert_vision_observation(original)
        self.insert_pending(NOW)
        conflicting = reframe(
            original,
            frame="conflict-between-signal-and-fill",
            captured_at=NOW + timedelta(seconds=1),
            radiant_hero_ids=(1, 2, 3, 4, 6),
            dire_hero_ids=(5, 7, 8, 9, 10),
        )
        self.store.insert_vision_observation(conflicting)
        self.record_transport(NOW + timedelta(seconds=2), key="successor")

        result = run_once(
            self.store, Mock(), MISSING_VISION,
            now=NOW + timedelta(seconds=3),
        )

        self.assertEqual(result["status"], "shadow_rejected")
        row = self.store.connection.execute(
            "SELECT status, rejection_reason FROM shadow_orders WHERE order_key=?",
            (self.pending_order_key,),
        ).fetchone()
        self.assertEqual(tuple(row), ("rejected", "vision_draft_conflict"))
        self.assertEqual(
            self.store.connection.execute(
                """SELECT COUNT(*) FROM vision_derived_invalidations
                   WHERE dependent_type='shadow_order' AND dependent_key=?""",
                (self.pending_order_key,),
            ).fetchone()[0],
            0,
        )

    def test_draft_conflict_after_successor_does_not_retroactively_reject_fill(
        self,
    ) -> None:
        original = observation(NOW - timedelta(seconds=1), frame="original")
        self.store.insert_vision_observation(original)
        self.insert_pending(NOW)
        successor_at = NOW + timedelta(seconds=2)
        self.record_transport(successor_at, key="first-successor")
        conflicting = reframe(
            original,
            frame="conflict-after-successor",
            captured_at=NOW + timedelta(seconds=3),
            radiant_hero_ids=(1, 2, 3, 4, 6),
            dire_hero_ids=(5, 7, 8, 9, 10),
        )
        self.store.insert_vision_observation(conflicting)

        result = run_once(
            self.store, Mock(), MISSING_VISION,
            now=NOW + timedelta(seconds=4),
        )

        self.assertEqual(result["status"], "shadow_filled")
        row = self.store.connection.execute(
            """SELECT status, fill_price, filled_at, rejection_reason
                 FROM shadow_orders WHERE order_key=?""",
            (self.pending_order_key,),
        ).fetchone()
        self.assertEqual(
            tuple(row),
            ("filled", 2.8, successor_at.isoformat(), None),
        )

    def test_multiple_successors_bind_fill_cutoff_and_price_to_first(self) -> None:
        original = observation(NOW - timedelta(seconds=1), frame="original")
        self.store.insert_vision_observation(original)
        self.insert_pending(NOW)
        first_at = NOW + timedelta(seconds=2)
        first_rows = snapshots(first_at)
        first_rows[0] = replace(
            first_rows[0], price=2.75, last_update="first-successor"
        )
        self.record_transport(
            first_at, key="first-successor", rows=first_rows
        )
        conflicting = reframe(
            original,
            frame="conflict-between-successors",
            captured_at=NOW + timedelta(seconds=3),
            radiant_hero_ids=(1, 2, 3, 4, 6),
            dire_hero_ids=(5, 7, 8, 9, 10),
        )
        self.store.insert_vision_observation(conflicting)
        second_at = NOW + timedelta(seconds=4)
        second_rows = snapshots(second_at)
        second_rows[0] = replace(
            second_rows[0], price=3.1, last_update="second-successor"
        )
        self.record_transport(
            second_at, key="second-successor", rows=second_rows
        )

        result = run_once(
            self.store, Mock(), MISSING_VISION,
            now=NOW + timedelta(seconds=5),
        )

        self.assertEqual(result["status"], "shadow_filled")
        row = self.store.connection.execute(
            """SELECT status, fill_price, filled_at, rejection_reason
                 FROM shadow_orders WHERE order_key=?""",
            (self.pending_order_key,),
        ).fetchone()
        self.assertEqual(
            tuple(row),
            ("filled", 2.75, first_at.isoformat(), None),
        )

    def test_future_draft_conflict_keeps_cutoff_vision_usable(self) -> None:
        original = observation(NOW, frame="original")
        conflicting = reframe(
            original,
            frame="future-conflict",
            captured_at=NOW + timedelta(seconds=10),
            radiant_hero_ids=(1, 2, 3, 4, 6),
            dire_hero_ids=(5, 7, 8, 9, 10),
        )
        self.store.insert_vision_observation(original)
        self.store.insert_vision_observation(conflicting)
        self.store.upsert_raybet_match(
            raybet_metadata(), NOW - timedelta(minutes=2)
        )
        self.record_transport(NOW + timedelta(seconds=2), key="before-conflict")

        result = run_once(
            self.store, Mock(), MISSING_VISION,
            now=NOW + timedelta(seconds=3),
        )

        self.assertNotEqual(result["status"], "waiting_for_confirmed_vision")
        self.assertEqual(result["status"], "no_signal")
        self.assertEqual(result["reason"], "strict_live_ineligible")

    def test_delayed_worker_uses_transport_time_before_draft_conflict(self) -> None:
        original = observation(NOW, frame="original")
        conflicting = reframe(
            original,
            frame="later-conflict",
            captured_at=NOW + timedelta(seconds=10),
            radiant_hero_ids=(1, 2, 3, 4, 6),
            dire_hero_ids=(5, 7, 8, 9, 10),
        )
        self.store.insert_vision_observation(original)
        self.store.insert_vision_observation(conflicting)
        self.store.upsert_raybet_match(
            raybet_metadata(), NOW - timedelta(minutes=2)
        )
        self.record_transport(NOW + timedelta(seconds=2), key="before-conflict")

        result = run_once(
            self.store,
            Mock(),
            MISSING_VISION,
            now=NOW + timedelta(seconds=12),
        )

        self.assertEqual(result["status"], "no_signal")
        self.assertEqual(result["reason"], "strict_live_ineligible")
        self.assertEqual(
            self.store.connection.execute(
                "SELECT decided_at FROM strategy_decisions"
            ).fetchone()[0],
            (NOW + timedelta(seconds=2)).isoformat(),
        )

    def test_conflicted_match_does_not_hide_another_usable_match(self) -> None:
        other_observation = replace(
            observation(NOW, frame="other-match"),
            raybet_match_id="match-2",
        )
        original = observation(NOW + timedelta(seconds=1), frame="original")
        conflicting = reframe(
            original,
            frame="conflict",
            captured_at=NOW + timedelta(seconds=2),
            radiant_hero_ids=(1, 2, 3, 4, 6),
            dire_hero_ids=(5, 7, 8, 9, 10),
        )
        self.store.insert_vision_observation(other_observation)
        self.store.insert_vision_observation(original)
        self.store.insert_vision_observation(conflicting)
        self.record_transport(NOW + timedelta(seconds=3), key="conflicted")
        other_at = NOW + timedelta(seconds=1)
        other_rows = [
            replace(row, raybet_match_id="match-2")
            for row in snapshots(other_at)
        ]
        self.store.store_odds_observation(
            source="direct",
            observation_key="other-transport",
            source_event_id=None,
            raybet_match_id="match-2",
            observed_at=other_at,
            normalized_state_hash=normalized_state_hash(other_rows),
            snapshots=other_rows,
            raw_payload=raw_odds_payload(other_rows),
        )

        result = run_once(
            self.store,
            Mock(),
            MISSING_VISION,
            now=NOW + timedelta(seconds=4),
        )

        self.assertEqual(result["status"], "no_signal")
        self.assertEqual(result["reason"], "strict_live_ineligible")
        self.assertEqual(
            self.store.connection.execute(
                "SELECT raybet_match_id FROM strategy_decisions"
            ).fetchone()[0],
            "match-2",
        )

    def test_storage_rejects_decision_at_or_after_draft_conflict(self) -> None:
        original = observation(NOW, frame="original")
        conflicting = reframe(
            original,
            frame="conflict",
            captured_at=NOW + timedelta(seconds=1),
            radiant_hero_ids=(1, 2, 3, 4, 6),
            dire_hero_ids=(5, 7, 8, 9, 10),
        )
        self.store.insert_vision_observation(original)
        authority = self.seed_draft_authority(
            NOW,
            label="shadow-storage-conflict",
        )
        before_rows = self.record_transport(NOW, key="decision-before-conflict")
        market_probability = price_groups(before_rows)[before_rows[0].odds_id]
        base = dict(
            raybet_match_id="match-1",
            map_number=1,
            underdog_side="team_one",
            market_probability=market_probability,
            model_probability=0.5,
            edge=0.1,
            data_quality=0.8,
            eligible=True,
            reason="eligible",
            contributions={
                "draft": 0.1,
                "__inputs__": {
                    "draft_authority": asdict(authority),
                    "strict_live_eligibility": {
                        "mapping_refs": {"strict_mapping_id": 1}
                    },
                },
            },
            input_ref="input-1",
            strategy_version="strategy-1",
        )
        self.assertTrue(
            self.store.insert_decision(
                SimpleNamespace(
                    **base,
                    decision_key="decision-before-conflict",
                    decided_at=NOW,
                ),
                draft_authority=authority,
                vision_observation=original,
                vision_transport_key="decision-before-conflict",
            )
        )
        self.store.insert_vision_observation(conflicting)
        self.record_transport(
            NOW + timedelta(seconds=1),
            key="decision-after-conflict",
        )
        self.assertFalse(
            self.store.insert_decision(
                SimpleNamespace(
                    **{**base, "decision_key": "decision-after-conflict"},
                    decided_at=NOW + timedelta(seconds=1),
                ),
                draft_authority=authority,
                vision_observation=original,
                vision_transport_key="decision-after-conflict",
            )
        )

    def test_out_of_order_conflict_uses_earliest_capture_cutoff(self) -> None:
        original = observation(NOW, frame="original")
        first_conflict = reframe(
            original,
            frame="conflict-late",
            captured_at=NOW + timedelta(seconds=10),
            radiant_hero_ids=(1, 2, 3, 4, 6),
            dire_hero_ids=(5, 7, 8, 9, 10),
        )
        earlier_conflict = reframe(
            original,
            frame="conflict-earlier",
            captured_at=NOW + timedelta(seconds=5),
            radiant_hero_ids=(1, 2, 3, 4, 7),
            dire_hero_ids=(5, 6, 8, 9, 10),
        )
        self.store.insert_vision_observation(original)
        authority = self.seed_draft_authority(
            NOW,
            label="shadow-out-of-order-conflict",
        )
        self.store.insert_vision_observation(first_conflict)
        self.store.insert_vision_observation(earlier_conflict)
        transport_at = NOW + timedelta(seconds=7)
        transport_rows = self.record_transport(
            transport_at,
            key="decision-out-of-order",
        )
        decision = SimpleNamespace(
            decision_key="decision-out-of-order",
            raybet_match_id="match-1",
            map_number=1,
            decided_at=transport_at,
            underdog_side="team_one",
            market_probability=price_groups(transport_rows)[
                transport_rows[0].odds_id
            ],
            model_probability=0.5,
            edge=0.1,
            data_quality=0.8,
            eligible=True,
            reason="eligible",
            contributions={
                "draft": 0.1,
                "__inputs__": {
                    "draft_authority": asdict(authority),
                    "strict_live_eligibility": {
                        "mapping_refs": {"strict_mapping_id": 1}
                    },
                },
            },
            input_ref="input-1",
            strategy_version="strategy-1",
        )

        self.assertFalse(
            self.store.insert_decision(
                decision,
                draft_authority=authority,
                vision_observation=original,
                vision_transport_key="decision-out-of-order",
            )
        )

    def test_future_anchor_arriving_first_rebases_to_capture_order(self) -> None:
        original = observation(NOW, frame="original")
        later_conflict = reframe(
            original,
            frame="conflict-late",
            captured_at=NOW + timedelta(seconds=10),
            radiant_hero_ids=(1, 2, 3, 4, 6),
            dire_hero_ids=(5, 7, 8, 9, 10),
        )
        self.store.insert_vision_observation(later_conflict)
        self.store.insert_vision_observation(original)

        anchor = self.store.connection.execute(
            """SELECT draft_hash, radiant_hero_ids, anchored_at,
                      source_frame_ref, status, conflict_at
                 FROM vision_draft_anchors
                WHERE raybet_match_id='match-1' AND map_number=1"""
        ).fetchone()
        self.assertEqual(anchor["source_frame_ref"], original.source_frame_ref)
        self.assertEqual(anchor["anchored_at"], NOW.isoformat())
        self.assertEqual(anchor["status"], "conflict")
        self.assertEqual(anchor["conflict_at"], (NOW + timedelta(seconds=10)).isoformat())
        rows = self.store.connection.execute(
            """SELECT source_frame_ref, confirmed
                 FROM vision_observations
                WHERE raybet_match_id='match-1'
                ORDER BY captured_at"""
        ).fetchall()
        self.assertEqual(
            [(str(row[0]), int(row[1])) for row in rows],
            [
                (original.source_frame_ref, 1),
                (later_conflict.source_frame_ref, 0),
            ],
        )

        self.assertEqual(
            self.store.connection.execute(
                """SELECT COUNT(*) FROM vision_draft_conflicts
                    WHERE raybet_match_id='match-1' AND map_number=1"""
            ).fetchone()[0],
            1,
        )

    def test_out_of_order_conflict_is_excluded_from_causal_alignment_reads(self) -> None:
        original = observation(NOW, frame="original")
        first_conflict = reframe(
            original,
            frame="conflict-late",
            captured_at=NOW + timedelta(seconds=10),
            radiant_hero_ids=(1, 2, 3, 4, 6),
            dire_hero_ids=(5, 7, 8, 9, 10),
        )
        earlier_conflict = reframe(
            original,
            frame="conflict-earlier",
            captured_at=NOW + timedelta(seconds=5),
            radiant_hero_ids=(1, 2, 3, 4, 7),
            dire_hero_ids=(5, 6, 8, 9, 10),
        )
        self.store.insert_vision_observation(original)
        self.store.insert_vision_observation(first_conflict)
        self.store.insert_vision_observation(earlier_conflict)
        self.record_transport(NOW + timedelta(seconds=7), key="before-late-conflict")

        self.assertEqual(
            persist_alignments(
                self.store, "match-1", as_of=NOW + timedelta(seconds=7)
            ),
            0,
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM odds_alignments"
            ).fetchone()[0],
            0,
        )

    def test_pending_successor_rechecks_conflict_before_filling(self) -> None:
        order = self.insert_pending(NOW)
        original = observation(NOW - timedelta(seconds=2), frame="original")
        conflicting = reframe(
            original,
            frame="conflict",
            captured_at=NOW - timedelta(seconds=1),
            radiant_hero_ids=(1, 2, 3, 4, 6),
            dire_hero_ids=(5, 7, 8, 9, 10),
        )
        self.store.insert_vision_observation(original)
        self.store.insert_vision_observation(conflicting)
        self.record_transport(NOW + timedelta(seconds=2), key="successor")

        resolved = self.store.process_pending_successor(
            order, watermark=NOW + timedelta(seconds=2)
        )

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.status, "rejected")
        self.assertEqual(resolved.rejection_reason, "vision_draft_conflict")
        self.assertEqual(
            self.store.connection.execute(
                "SELECT status FROM shadow_orders WHERE order_key=?",
                (self.pending_order_key,),
            ).fetchone()[0],
            "rejected",
        )

    def test_transport_uses_latest_prior_vision_not_a_future_frame(self) -> None:
        old = observation(NOW, frame="old")
        self.store.insert_vision_observation(old)
        self.record_transport(NOW + timedelta(seconds=1), key="before-new-frame")
        self.store.insert_vision_observation(
            observation(NOW + timedelta(seconds=2), frame="current")
        )
        strategy, _, _ = self._prepare_strategy_run(add_observation=False)

        with (
            patch("live_betting.shadow_monitor._profiles", side_effect=strategy.fake_profiles),
            patch(
                "live_betting.shadow_monitor.build_draft_curve",
                side_effect=strategy.fake_draft,
            ),
            patch.object(self.store, "insert_decision", return_value=True),
        ):
            result = run_once(
                self.store, strategy, MISSING_VISION,
                now=NOW + timedelta(seconds=3),
            )

        self.assertEqual(result["status"], "no_signal")
        aligned = strategy.evaluate.call_args.kwargs["observation"]
        self.assertEqual(aligned.source_frame_ref, old.source_frame_ref)
        self.assertEqual(aligned.game_clock_seconds, 601)

    def test_transport_without_a_recent_prior_vision_waits_for_alignment(self) -> None:
        self.record_transport(NOW + timedelta(seconds=1), key="no-prior-frame")
        self.store.insert_vision_observation(
            observation(NOW + timedelta(seconds=2), frame="future")
        )
        strategy = Mock()

        result = run_once(
            self.store, strategy, MISSING_VISION,
            now=NOW + timedelta(seconds=3),
        )

        self.assertEqual(result["status"], "waiting_for_usable_alignment")
        self.assertEqual(result["reason"], "no_prior_confirmed_observation")
        strategy.evaluate.assert_not_called()

    def test_transport_more_than_fifteen_seconds_after_vision_is_unusable(self) -> None:
        self.store.insert_vision_observation(observation(NOW))
        self.record_transport(NOW + timedelta(seconds=16), key="vision-gap")
        strategy = Mock()

        result = run_once(
            self.store, strategy, MISSING_VISION,
            now=NOW + timedelta(seconds=17),
        )

        self.assertEqual(result["status"], "waiting_for_usable_alignment")
        self.assertEqual(result["reason"], "observation_gap")
        strategy.evaluate.assert_not_called()

    def test_missing_strict_mapping_is_persisted_as_structured_no_signal(self) -> None:
        self.store.insert_vision_observation(observation(NOW))
        self.store.upsert_raybet_match(
            raybet_metadata(), NOW - timedelta(minutes=2)
        )
        self.record_transport(NOW + timedelta(seconds=1), key="unmapped")
        strategy = Mock()

        with (
            patch("live_betting.shadow_monitor._profiles") as profiles,
            patch("live_betting.shadow_monitor.build_draft_curve") as draft,
        ):
            result = run_once(
                self.store,
                strategy,
                MISSING_VISION,
                now=NOW + timedelta(seconds=2),
            )

        self.assertEqual(result["status"], "no_signal")
        self.assertEqual(result["reason"], "strict_live_ineligible")
        self.assertEqual(result["reason_code"], "accepted_mapping_missing")
        profiles.assert_not_called()
        draft.assert_not_called()
        strategy.evaluate.assert_not_called()
        row = self.store.connection.execute(
            "SELECT reason, contributions_json FROM strategy_decisions"
        ).fetchone()
        self.assertEqual(row["reason"], "strict_live_ineligible:accepted_mapping_missing")
        persisted = __import__("json").loads(row["contributions_json"])
        self.assertEqual(
            persisted["__inputs__"]["transport"]["current_key"], "unmapped"
        )

    def test_missing_validated_landmark_is_persisted_before_profiles(self) -> None:
        strategy, _, _ = self._prepare_strategy_run()
        at = NOW + timedelta(seconds=1)
        self.record_transport(
            at, key="no-landmark", rows=complete_snapshots(at)
        )

        with patch("live_betting.shadow_monitor._profiles") as profiles:
            result = run_once(
                self.store,
                strategy,
                MISSING_VISION,
                now=NOW + timedelta(seconds=2),
            )

        self.assertEqual(result["status"], "no_signal")
        self.assertEqual(result["reason"], "draft_landmark_unavailable")
        self.assertEqual(
            result["reason_code"], "validated_live_draft_prediction_missing"
        )
        profiles.assert_not_called()
        strategy.evaluate.assert_not_called()
        row = self.store.connection.execute(
            "SELECT reason FROM strategy_decisions"
        ).fetchone()
        self.assertIn("validated_live_draft_prediction_missing", row["reason"])
        research = self.store.connection.execute(
            """SELECT actionability, raw_model_probability, feature_hash,
                      model_hash, calibration_hash, gate_status,
                      gate_failures_json, manual_clock_trust,
                      manual_clock_validation
                 FROM research_live_predictions"""
        ).fetchone()
        self.assertIsNotNone(research)
        self.assertEqual(research["actionability"], "research_only")
        self.assertIsNone(research["raw_model_probability"])
        self.assertIsNone(research["feature_hash"])
        self.assertIsNone(research["model_hash"])
        self.assertIsNone(research["calibration_hash"])
        self.assertEqual(research["gate_status"], "unavailable")
        self.assertIn(
            "validated_live_draft_prediction_missing",
            __import__("json").loads(research["gate_failures_json"]),
        )
        self.assertEqual(research["manual_clock_trust"], "not_observed")
        self.assertEqual(research["manual_clock_validation"], "not_observed")
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM shadow_orders").fetchone()[0],
            0,
        )

    def _prepare_strategy_run(
        self, *, add_observation: bool = True,
    ) -> tuple[Mock, list[int], list[int]]:
        if add_observation:
            self.store.insert_vision_observation(observation(NOW))
        authority = self.seed_draft_authority(
            NOW,
            label="shadow-transport-flow",
        )
        strategy = Mock()
        strategy.evaluate.return_value = SimpleNamespace(
            decision=SimpleNamespace(
                reason="test", edge=0.0, data_quality=1.0,
                decision_key="decision", inputs={},
            ),
            order=None,
        )
        profile_times: list[int] = []
        draft_times: list[int] = []

        def fake_profiles(_connection: object, _team_id: int, as_of: int):
            profile_times.append(as_of)
            return (
                TeamStyleProfile(0, 0, 0.18, 0.16, 0.84, 0.35, 36.0, 0.0),
                PlayerForm((), 0.0, {}, 0, 0.0),
            )

        def fake_draft(
            _connection: object, _radiant: tuple[int, ...],
            _dire: tuple[int, ...], as_of: int,
            **_target: object,
        ) -> DraftCurve:
            draft_times.append(as_of)
            return DraftCurve(
                (DraftPoint(
                    10, authority.radiant_probability, 0.0, 0.0,
                    authority.quality, validated=True,
                    support=authority.support,
                    calibration_ref=(
                        f"draft-calibration:{authority.calibration_hash}"
                    ),
                    input_refs=("test",),
                    uncertainty=authority.uncertainty,
                    feature_hash=authority.feature_hash,
                    model_hash=authority.model_hash,
                    calibration_hash=authority.calibration_hash,
                    global_calibration_passed=True,
                    global_gate_ref=authority.global_gate_ref,
                    model_version=authority.model_version,
                    model_kind="pure_draft",
                    availability_mode="prospective",
                    input_snapshot_hash=authority.input_snapshot_hash,
                    landmark_key=authority.landmark_key,
                    curve_key=authority.curve_key,
                    deployment_key=authority.deployment_key,
                    target_snapshot_hash=authority.target_snapshot_hash,
                ),),
                source_ref=authority.source_ref,
                authority_revision=authority.authority_revision,
                dependency_revision=authority.dependency_revision,
                curve_key=authority.curve_key,
                deployment_key=authority.deployment_key,
                target_snapshot_hash=authority.target_snapshot_hash,
                strict_mapping_id=authority.strict_mapping_id,
            )

        strategy.fake_profiles = fake_profiles
        strategy.fake_draft = fake_draft
        return strategy, profile_times, draft_times

    def test_transport_time_drives_profiles_draft_and_explicit_previous_state(self) -> None:
        strategy, profile_times, draft_times = self._prepare_strategy_run()
        first_at = NOW + timedelta(seconds=1)
        current_at = NOW + timedelta(seconds=4)
        self.record_transport(first_at, key="first")
        self.record_transport(current_at, key="unchanged")

        with (
            patch("live_betting.shadow_monitor._profiles", side_effect=strategy.fake_profiles),
            patch(
                "live_betting.shadow_monitor.build_draft_curve",
                side_effect=strategy.fake_draft,
            ),
            patch.object(self.store, "insert_decision", return_value=True),
        ):
            result = run_once(
                self.store, strategy, MISSING_VISION,
                now=NOW + timedelta(seconds=5),
            )

        self.assertEqual(result["status"], "no_signal")
        expected_as_of = int(current_at.timestamp())
        self.assertEqual(profile_times, [expected_as_of, expected_as_of])
        self.assertEqual(draft_times, [expected_as_of])
        call = strategy.evaluate.call_args.kwargs
        self.assertEqual(call["decided_at"], current_at)
        self.assertEqual(call["snapshot_observed_at"], current_at)
        self.assertEqual(call["previous_snapshot_observed_at"], first_at)
        self.assertIsNotNone(call["previous_snapshots"])
        self.assertEqual(call["observation"].game_clock_seconds, 604)
        self.assertEqual(call["previous_observation"].game_clock_seconds, 601)

    def test_repolling_one_transport_never_invents_previous_state(self) -> None:
        strategy, _, _ = self._prepare_strategy_run()
        only_at = NOW + timedelta(seconds=1)
        self.record_transport(only_at, key="only")

        for run_at in (NOW + timedelta(seconds=2), NOW + timedelta(seconds=3)):
            with (
                patch(
                    "live_betting.shadow_monitor._profiles",
                    side_effect=strategy.fake_profiles,
                ),
                patch(
                    "live_betting.shadow_monitor.build_draft_curve",
                    side_effect=strategy.fake_draft,
                ),
                patch.object(self.store, "insert_decision", return_value=True),
            ):
                run_once(self.store, strategy, MISSING_VISION, now=run_at)

        self.assertEqual(strategy.evaluate.call_count, 2)
        for call in strategy.evaluate.call_args_list:
            self.assertIsNone(call.kwargs["previous_snapshots"])
            self.assertIsNone(call.kwargs["previous_snapshot_observed_at"])


if __name__ == "__main__":
    unittest.main()
