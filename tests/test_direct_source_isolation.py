from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from live_betting.markets import normalized_state_hash
from live_betting.models import Market, OddsSnapshot, ShadowOrder
from live_betting.research import ResearchPrediction, ResearchPriceLabel
from live_betting.settlement import (
    SettlementAuthorityError,
    resolve_authoritative_settlement,
)
from live_betting.shadow_monitor import _transport_refs
from live_betting.storage import LiveBettingStore
from tests.draft_authority_fixture import make_test_vision_observation
from tests.test_settlement_authority import authority_connection


NOW = datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc)
MATCH_ID = "source-isolation-match"


def _snapshots(at: datetime, team_one_price: float) -> list[OddsSnapshot]:
    return [
        OddsSnapshot(
            MATCH_ID,
            "winner-one",
            "winner-group",
            at,
            team_one_price,
            1,
            Market("winner", "map_1", "team_one", None, "team_one", True),
        ),
        OddsSnapshot(
            MATCH_ID,
            "winner-two",
            "winner-group",
            at,
            1.5,
            1,
            Market("winner", "map_1", "team_two", None, "team_two", True),
        ),
    ]


def _raw_payload(rows: list[OddsSnapshot]) -> dict[str, object]:
    return {
        "result": {
            "id": MATCH_ID,
            "game_id": 151,
            "team": [
                {"team_id": 1, "team_name": "One", "pos": 1},
                {"team_id": 2, "team_name": "Two", "pos": 2},
            ],
            "odds": [
                {
                    "id": row.odds_id,
                    "odds_group_id": row.odds_group_id,
                    "match_stage": "r1",
                    "group_short_name": "Winner",
                    "tag": "win",
                    "team_id": 1 if row.market.side == "team_one" else 2,
                    "odds": row.price,
                    "status": row.status,
                }
                for row in rows
            ],
        }
    }


def _observe(
    store: LiveBettingStore,
    *,
    key: str,
    source: str,
    at: datetime,
    team_one_price: float,
) -> tuple[str, int]:
    rows = _snapshots(at, team_one_price)
    return store.store_odds_observation(
        source=source,
        observation_key=key,
        source_event_id=None,
        raybet_match_id=MATCH_ID,
        observed_at=at,
        normalized_state_hash=normalized_state_hash(rows),
        snapshots=rows,
        raw_payload=_raw_payload(rows),
    )


def test_browser_does_not_advance_direct_timing_watermark_or_transport_refs(
    tmp_path: Path,
) -> None:
    with LiveBettingStore(tmp_path / "source.db") as store:
        store.init_schema()
        direct_first = NOW
        browser_later = NOW + timedelta(seconds=3)
        direct_second = NOW + timedelta(seconds=2)

        assert _observe(
            store,
            key="direct-first",
            source="direct",
            at=direct_first,
            team_one_price=2.0,
        )[0] == "on_time"
        assert _observe(
            store,
            key="browser-later",
            source="browser",
            at=browser_later,
            team_one_price=9.0,
        )[0] == "on_time"
        assert _observe(
            store,
            key="direct-second",
            source="direct",
            at=direct_second,
            team_one_price=2.1,
        )[0] == "on_time"

        assert store.processed_transport_watermark(
            MATCH_ID, as_of=browser_later
        ) == direct_second
        assert [
            ref.observation_key
            for ref in _transport_refs(store.connection, MATCH_ID, browser_later)
        ] == ["direct-second", "direct-first"]


def test_opendota_cannot_be_a_live_market_transport(tmp_path: Path) -> None:
    with LiveBettingStore(tmp_path / "source.db") as store:
        store.init_schema()
        assert _observe(
            store,
            key="opendota-only",
            source="opendota",
            at=NOW,
            team_one_price=2.0,
        )[1] == 0
        assert store.connection.execute(
            "SELECT 1 FROM odds_transport_observations WHERE observation_key='opendota-only'"
        ).fetchone() is None
        assert store.processed_transport_watermark(MATCH_ID, as_of=NOW) is None
        assert store.next_fill_candidate(
            ShadowOrder(
                order_key="opendota-order",
                raybet_match_id=MATCH_ID,
                odds_id="winner-one",
                market=Market("winner", "map_1", "team_one", None, "team_one", True),
                signaled_at=NOW - timedelta(seconds=1),
                model_probability=0.6,
                market_probability=0.5,
                signal_price=2.0,
                signal_transport_key="opendota-only",
                signal_transport_at=NOW - timedelta(seconds=1),
                expires_at=NOW + timedelta(seconds=14),
                signal_odds_group_id="winner-group",
                signal_outcome_key="team_one",
                signal_identity_verified=True,
            )
        ) is None


def test_browser_is_not_application_market_authority_or_first_fill_successor(
    tmp_path: Path,
) -> None:
    with LiveBettingStore(tmp_path / "source.db") as store:
        store.init_schema()
        browser_at = NOW + timedelta(seconds=1)
        direct_at = NOW + timedelta(seconds=2)
        _observe(
            store,
            key="signal",
            source="direct",
            at=NOW,
            team_one_price=2.0,
        )
        _observe(
            store,
            key="browser-successor",
            source="browser",
            at=browser_at,
            team_one_price=9.0,
        )
        _observe(
            store,
            key="direct-successor",
            source="direct",
            at=direct_at,
            team_one_price=2.1,
        )

        persisted_view_keys = {
            str(row[0])
            for row in store.connection.execute(
                "SELECT observation_key FROM trusted_odds_winner_market_authority"
            )
        }
        assert persisted_view_keys == {
            "signal",
            "browser-successor",
            "direct-successor",
        }

        schema_objects = {
            str(row[0]): str(row[1])
            for row in store.connection.execute(
                """SELECT name, sql FROM sqlite_master
                    WHERE name IN (
                        'trusted_odds_winner_market_authority',
                        'strategy_decision_vision_authority_insert',
                        'verified_strategy_decision_vision_authority',
                        'research_price_label_authority_insert'
                    )"""
            )
        }
        assert set(schema_objects) == {
            "trusted_odds_winner_market_authority",
            "strategy_decision_vision_authority_insert",
            "verified_strategy_decision_vision_authority",
            "research_price_label_authority_insert",
        }
        assert all("source='direct'" not in sql for sql in schema_objects.values())

        order = ShadowOrder(
            order_key="source-order",
            raybet_match_id=MATCH_ID,
            odds_id="winner-one",
            market=Market(
                "winner", "map_1", "team_one", None, "team_one", True
            ),
            signaled_at=NOW,
            model_probability=0.6,
            market_probability=0.5,
            signal_price=2.0,
            signal_transport_key="signal",
            signal_transport_at=NOW,
            expires_at=NOW + timedelta(seconds=15),
            signal_odds_group_id="winner-group",
            signal_outcome_key="team_one",
            signal_identity_verified=True,
        )
        candidate = store.next_fill_candidate(order)

        browser_order = replace(
            order,
            signaled_at=browser_at,
            signal_transport_key="browser-successor",
            signal_transport_at=browser_at,
            expires_at=browser_at + timedelta(seconds=15),
            signal_price=9.0,
        )
        assert not store._signal_identity_matches(browser_order)
        assert not store._signal_market_authority_matches(browser_order, 1)
        assert candidate is not None
        assert candidate["transport_observation_key"] == "direct-successor"
        assert float(candidate["price"]) == 2.1


def test_browser_transport_cannot_bind_decision_vision_authority(
    tmp_path: Path,
) -> None:
    with LiveBettingStore(tmp_path / "source.db") as store:
        store.init_schema()
        vision = make_test_vision_observation(
            raybet_match_id=MATCH_ID,
            map_number=1,
            captured_at=NOW - timedelta(seconds=1),
            label="source-isolation-browser-decision",
        )
        assert store.insert_vision_observation(vision)
        _observe(
            store,
            key="browser-decision",
            source="browser",
            at=NOW,
            team_one_price=2.0,
        )

        authority = store._derive_decision_vision_authority(
            SimpleNamespace(
                raybet_match_id=MATCH_ID,
                map_number=1,
                underdog_side="team_one",
                market_probability=0.5,
                decided_at=NOW,
            ),
            vision_observation=vision,
            vision_transport_key="browser-decision",
            draft_authority=object(),
        )

        assert authority is None


def test_browser_transport_cannot_create_research_price_authority(
    tmp_path: Path,
) -> None:
    with LiveBettingStore(tmp_path / "source.db") as store:
        store.init_schema()
        first_hash = normalized_state_hash(_snapshots(NOW, 2.0))
        _observe(
            store,
            key="browser-prediction",
            source="browser",
            at=NOW,
            team_one_price=2.0,
        )
        prediction = ResearchPrediction(
            prediction_key="browser-prediction",
            schema_version="research-v1",
            raybet_match_id=MATCH_ID,
            map_number=1,
            observed_at=NOW,
            game_clock_seconds=600,
            game_minute=10.0,
            selected_side="team_one",
            market_probability=0.5,
            market_price=2.0,
            raw_model_probability=None,
            feature_hash=None,
            model_hash=None,
            calibration_hash=None,
            transport_key="browser-prediction",
            transport_hash=first_hash,
            radiant_hero_ids=(1, 2, 3, 4, 5),
            dire_hero_ids=(6, 7, 8, 9, 10),
            radiant_team_side="team_one",
            strict_mapping_id=1,
            clock_source="vision",
            clock_trust="trusted_vision",
            manual_clock_event_id=None,
            manual_clock_seconds=None,
            manual_clock_trust="not_observed",
            manual_clock_validation="not_observed",
            actionability="research_only",
            gate_status="failed",
            gate_failures=("source_isolation_fixture",),
            input_context_hash="a" * 64,
            draft_authority=None,
            created_at=NOW,
        )
        assert store.insert_research_prediction(prediction)

        successor_at = NOW + timedelta(seconds=2)
        successor_hash = normalized_state_hash(_snapshots(successor_at, 2.1))
        _observe(
            store,
            key="browser-research-successor",
            source="browser",
            at=successor_at,
            team_one_price=2.1,
        )
        market = store.connection.execute(
            """SELECT underdog_price, underdog_probability
                 FROM trusted_odds_winner_market_authority
                WHERE observation_key='browser-research-successor'"""
        ).fetchone()
        assert market is not None

        assert not store.insert_research_price_label(
            ResearchPriceLabel(
                label_key="browser-label",
                prediction_key=prediction.prediction_key,
                transport_key="browser-research-successor",
                transport_hash=successor_hash,
                observed_at=successor_at,
                selected_side="team_one",
                price=float(market["underdog_price"]),
                market_probability=float(market["underdog_probability"]),
                seconds_after_prediction=2.0,
                created_at=successor_at,
            )
        )
        assert store.connection.execute(
            "SELECT COUNT(*) FROM research_price_labels"
        ).fetchone()[0] == 0


def test_settlement_rejects_browser_signal_lineage() -> None:
    connection = authority_connection()
    try:
        connection.execute(
            "ALTER TABLE shadow_orders ADD COLUMN signal_transport_key TEXT"
        )
        connection.execute(
            "ALTER TABLE shadow_orders ADD COLUMN signal_transport_at TEXT"
        )
        connection.execute(
            """CREATE TABLE odds_transport_observations (
                   observation_key TEXT PRIMARY KEY,
                   source TEXT NOT NULL,
                   raybet_match_id TEXT NOT NULL,
                   observed_at TEXT NOT NULL
               )"""
        )
        connection.execute(
            """UPDATE shadow_orders
                  SET signal_transport_key='browser-signal',
                      signal_transport_at='2026-07-17T09:58:00+00:00'
                WHERE order_key='order-1'"""
        )
        connection.execute(
            """INSERT INTO odds_transport_observations VALUES (
                   'browser-signal', 'browser', 'match-1',
                   '2026-07-17T09:58:00+00:00'
               )"""
        )

        with pytest.raises(
            SettlementAuthorityError,
            match="settlement_order_market_source_invalid",
        ):
            resolve_authoritative_settlement(connection, "order-1")
    finally:
        connection.close()
