"""SQLite persistence for live collection and shadow orders."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .models import LiveEvent, LiveFrame, Market, OddsSnapshot, ProviderMatch, ShadowOrder
from .pricing import market_key
from .sanitize import sanitize_raybet_payload
from .strategy import attempt_fill, is_open


CURRENT_SCHEMA_VERSION = 4
VISION_DRAFT_CONFLICT_REASON = "confirmed_draft_conflict"


def _valid_confirmed_vision_payload(
    radiant_hero_ids: object,
    dire_hero_ids: object,
    source_frame_ref: object,
) -> bool:
    """Validate the immutable inputs required for a confirmed draft frame."""
    if not isinstance(radiant_hero_ids, (list, tuple)):
        return False
    if not isinstance(dire_hero_ids, (list, tuple)):
        return False
    if not isinstance(source_frame_ref, str) or not source_frame_ref.strip():
        return False
    heroes = tuple(radiant_hero_ids) + tuple(dire_hero_ids)
    return (
        len(radiant_hero_ids) == 5
        and len(dire_hero_ids) == 5
        and all(type(hero_id) is int and hero_id > 0 for hero_id in heroes)
        and len(set(heroes)) == 10
    )


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS live_schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_matches (
    provider TEXT NOT NULL,
    provider_match_id TEXT NOT NULL,
    tournament TEXT,
    team_one TEXT,
    team_two TEXT,
    scheduled_at TEXT,
    best_of INTEGER,
    status TEXT,
    raw_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (provider, provider_match_id)
);
CREATE TABLE IF NOT EXISTS raybet_matches (
    raybet_match_id TEXT PRIMARY KEY,
    tournament TEXT,
    team_one TEXT,
    team_two TEXT,
    scheduled_at TEXT,
    best_of INTEGER,
    status TEXT,
    live_url TEXT,
    raw_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS match_links (
    raybet_match_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_match_id TEXT NOT NULL,
    confidence REAL NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (raybet_match_id, provider)
);
CREATE TABLE IF NOT EXISTS odds_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raybet_match_id TEXT NOT NULL,
    odds_id TEXT NOT NULL,
    odds_group_id TEXT,
    received_at TEXT NOT NULL,
    price REAL NOT NULL,
    status TEXT,
    market_type TEXT NOT NULL,
    period TEXT NOT NULL,
    side TEXT,
    line REAL,
    outcome_key TEXT NOT NULL,
    supported INTEGER NOT NULL,
    last_update TEXT,
    raw_json TEXT NOT NULL,
    UNIQUE (raybet_match_id, odds_id, received_at)
);
CREATE INDEX IF NOT EXISTS idx_live_odds_match_time
    ON odds_snapshots(raybet_match_id, received_at);
CREATE TABLE IF NOT EXISTS browser_events (
    event_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    capture_session_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    transport TEXT NOT NULL,
    event_type TEXT NOT NULL,
    raybet_match_id TEXT,
    game_id INTEGER,
    page_origin TEXT NOT NULL,
    page_path TEXT NOT NULL,
    source_path TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_bytes INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    capture_reason TEXT,
    extension_version TEXT NOT NULL,
    recognized INTEGER NOT NULL,
    processing_status TEXT NOT NULL,
    processing_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_browser_events_match_time
    ON browser_events(raybet_match_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_browser_events_type_time
    ON browser_events(event_type, captured_at);
CREATE TRIGGER IF NOT EXISTS browser_events_immutable
BEFORE UPDATE ON browser_events
WHEN OLD.event_id IS NOT NEW.event_id
  OR OLD.schema_version IS NOT NEW.schema_version
  OR OLD.capture_session_id IS NOT NEW.capture_session_id
  OR OLD.captured_at IS NOT NEW.captured_at
  OR OLD.received_at IS NOT NEW.received_at
  OR OLD.transport IS NOT NEW.transport
  OR OLD.event_type IS NOT NEW.event_type
  OR OLD.raybet_match_id IS NOT NEW.raybet_match_id
  OR OLD.game_id IS NOT NEW.game_id
  OR OLD.page_origin IS NOT NEW.page_origin
  OR OLD.page_path IS NOT NEW.page_path
  OR OLD.source_path IS NOT NEW.source_path
  OR OLD.payload_hash IS NOT NEW.payload_hash
  OR OLD.payload_bytes IS NOT NEW.payload_bytes
  OR OLD.payload_json IS NOT NEW.payload_json
  OR OLD.capture_reason IS NOT NEW.capture_reason
  OR OLD.extension_version IS NOT NEW.extension_version
  OR OLD.recognized IS NOT NEW.recognized
BEGIN
    SELECT RAISE(ABORT, 'browser event payload is immutable');
END;
CREATE TABLE IF NOT EXISTS odds_transport_observations (
    observation_key TEXT PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN ('direct', 'browser')),
    source_event_id TEXT,
    raybet_match_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    normalized_state_hash TEXT NOT NULL,
    timing_status TEXT NOT NULL,
    processing_status TEXT NOT NULL,
    normalized_change_count INTEGER NOT NULL,
    FOREIGN KEY (source_event_id) REFERENCES browser_events(event_id)
);
CREATE INDEX IF NOT EXISTS idx_odds_transport_match_time
    ON odds_transport_observations(raybet_match_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_odds_transport_hash_time
    ON odds_transport_observations(normalized_state_hash, observed_at);
CREATE TRIGGER IF NOT EXISTS odds_transport_observations_guard_update
BEFORE UPDATE ON odds_transport_observations
WHEN OLD.observation_key IS NOT NEW.observation_key
  OR OLD.source IS NOT NEW.source
  OR OLD.source_event_id IS NOT NEW.source_event_id
  OR OLD.raybet_match_id IS NOT NEW.raybet_match_id
  OR OLD.observed_at IS NOT NEW.observed_at
  OR OLD.normalized_state_hash IS NOT NEW.normalized_state_hash
  OR OLD.timing_status IS NOT NEW.timing_status
  OR NOT (
      (OLD.processing_status IS NEW.processing_status
       AND OLD.normalized_change_count IS NEW.normalized_change_count)
      OR (OLD.processing_status='processing'
          AND NEW.processing_status='processed'
          AND OLD.normalized_change_count=0
          AND NEW.normalized_change_count>=0)
  )
BEGIN
    SELECT RAISE(ABORT, 'odds transport observation is immutable');
END;
CREATE TRIGGER IF NOT EXISTS odds_transport_observations_immutable_delete
BEFORE DELETE ON odds_transport_observations
BEGIN
    SELECT RAISE(ABORT, 'odds transport observation is immutable');
END;
CREATE TABLE IF NOT EXISTS odds_response_outcomes (
    observation_key TEXT NOT NULL,
    raybet_match_id TEXT NOT NULL,
    odds_id TEXT NOT NULL,
    odds_group_id TEXT,
    received_at TEXT NOT NULL,
    price REAL NOT NULL,
    status TEXT,
    market_type TEXT NOT NULL,
    period TEXT NOT NULL,
    side TEXT,
    line REAL,
    outcome_key TEXT NOT NULL,
    supported INTEGER NOT NULL,
    last_update TEXT,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (observation_key, odds_id),
    FOREIGN KEY (observation_key)
        REFERENCES odds_transport_observations(observation_key)
);
CREATE INDEX IF NOT EXISTS idx_odds_response_match_outcome
    ON odds_response_outcomes(raybet_match_id, odds_id, observation_key);
CREATE TRIGGER IF NOT EXISTS odds_response_outcomes_immutable_update
BEFORE UPDATE ON odds_response_outcomes
BEGIN
    SELECT RAISE(ABORT, 'odds response outcome is immutable');
END;
CREATE TRIGGER IF NOT EXISTS odds_response_outcomes_immutable_delete
BEFORE DELETE ON odds_response_outcomes
BEGIN
    SELECT RAISE(ABORT, 'odds response outcome is immutable');
END;
CREATE TABLE IF NOT EXISTS live_frames (
    provider TEXT NOT NULL,
    provider_match_id TEXT NOT NULL,
    provider_game_id TEXT,
    sequence TEXT NOT NULL DEFAULT '',
    source_at TEXT,
    received_at TEXT NOT NULL,
    game_time INTEGER,
    team_one_kills INTEGER,
    team_two_kills INTEGER,
    team_one_gold INTEGER,
    team_two_gold INTEGER,
    state TEXT,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (provider, provider_match_id, provider_game_id, sequence)
);
CREATE TABLE IF NOT EXISTS live_events (
    provider TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    provider_match_id TEXT NOT NULL,
    provider_game_id TEXT,
    event_type TEXT NOT NULL,
    source_at TEXT,
    received_at TEXT NOT NULL,
    game_time INTEGER,
    team TEXT,
    player TEXT,
    value REAL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (provider, provider_event_id)
);
CREATE TABLE IF NOT EXISTS model_quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raybet_match_id TEXT NOT NULL,
    provider_game_id TEXT,
    market_key TEXT NOT NULL,
    model_probability REAL NOT NULL,
    market_probability REAL NOT NULL,
    edge REAL NOT NULL,
    quoted_at TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    input_ref TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_orders (
    order_key TEXT PRIMARY KEY,
    raybet_match_id TEXT NOT NULL,
    strict_mapping_id INTEGER,
    odds_id TEXT NOT NULL,
    market_key TEXT NOT NULL,
    signaled_at TEXT NOT NULL,
    model_probability REAL NOT NULL,
    market_probability REAL NOT NULL,
    signal_price REAL NOT NULL,
    signal_transport_key TEXT NOT NULL,
    signal_transport_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    signal_odds_group_id TEXT,
    signal_outcome_key TEXT,
    signal_identity_verified INTEGER NOT NULL
        CHECK (signal_identity_verified IN (0, 1)),
    stake REAL NOT NULL,
    status TEXT NOT NULL,
    fill_price REAL,
    filled_at TEXT,
    rejection_reason TEXT
);
CREATE TABLE IF NOT EXISTS shadow_order_decision_lineage (
    order_key TEXT PRIMARY KEY,
    decision_key TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS shadow_order_decision_lineage_immutable_update
BEFORE UPDATE ON shadow_order_decision_lineage
BEGIN
    SELECT RAISE(ABORT, 'shadow order decision lineage is immutable');
END;
CREATE TRIGGER IF NOT EXISTS shadow_order_decision_lineage_immutable_delete
BEFORE DELETE ON shadow_order_decision_lineage
BEGIN
    SELECT RAISE(ABORT, 'shadow order decision lineage is immutable');
END;
CREATE TABLE IF NOT EXISTS settlements (
    order_key TEXT PRIMARY KEY,
    result TEXT NOT NULL,
    return_units REAL NOT NULL,
    settled_at TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    review_required INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settlement_result_evidence (
    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    raybet_match_id TEXT NOT NULL,
    map_number INTEGER NOT NULL CHECK (map_number > 0),
    dota_match_id INTEGER,
    source TEXT NOT NULL CHECK (source IN ('raybet', 'opendota')),
    status TEXT NOT NULL CHECK (status IN ('confirmed', 'pending', 'conflict')),
    winner_side TEXT CHECK (winner_side IN ('team_one', 'team_two')),
    evidence_ref TEXT NOT NULL,
    facts_json TEXT NOT NULL CHECK (json_valid(facts_json)),
    observed_at TEXT NOT NULL,
    UNIQUE (raybet_match_id, map_number, source, evidence_ref)
);
CREATE INDEX IF NOT EXISTS idx_settlement_result_evidence_map
    ON settlement_result_evidence(raybet_match_id, map_number, source, observed_at);
CREATE TRIGGER IF NOT EXISTS settlement_result_evidence_no_update
BEFORE UPDATE ON settlement_result_evidence
BEGIN
    SELECT RAISE(ABORT, 'settlement result evidence is append-only');
END;
CREATE TRIGGER IF NOT EXISTS settlement_result_evidence_no_delete
BEFORE DELETE ON settlement_result_evidence
BEGIN
    SELECT RAISE(ABORT, 'settlement result evidence is append-only');
END;
CREATE TABLE IF NOT EXISTS settlement_reconciliations (
    raybet_match_id TEXT NOT NULL,
    map_number INTEGER NOT NULL CHECK (map_number > 0),
    dota_match_id INTEGER NOT NULL,
    raybet_winner_side TEXT CHECK (raybet_winner_side IN ('team_one', 'team_two')),
    opendota_winner_side TEXT NOT NULL
        CHECK (opendota_winner_side IN ('team_one', 'team_two')),
    raybet_evidence_ref TEXT NOT NULL,
    opendota_evidence_ref TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'confirmed', 'manual_review')),
    reason TEXT NOT NULL,
    first_observed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (raybet_match_id, map_number)
);
CREATE INDEX IF NOT EXISTS idx_settlement_reconciliations_dota_match
    ON settlement_reconciliations(dota_match_id);
CREATE TABLE IF NOT EXISTS notification_outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_key TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN
        ('filled', 'settled', 'monitor_alert', 'monitor_recovery')),
    channel TEXT NOT NULL DEFAULT 'email',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'leased', 'sent', 'dead_letter')),
    recipient TEXT NOT NULL,
    message_id TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    statistics_cutoff TEXT NOT NULL,
    template_version TEXT NOT NULL,
    lease_token TEXT,
    lease_until TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at TEXT,
    last_error TEXT,
    sent_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (order_key, event_type, channel)
);
CREATE INDEX IF NOT EXISTS idx_notification_outbox_due
    ON notification_outbox(status, next_attempt_at, lease_until);
CREATE TABLE IF NOT EXISTS notification_outbox_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    outbox_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS service_health (
    component TEXT PRIMARY KEY,
    status TEXT NOT NULL
        CHECK (status IN ('starting', 'healthy', 'degraded', 'unhealthy', 'stopped')),
    last_heartbeat_at TEXT,
    last_success_at TEXT,
    last_error_at TEXT,
    last_error TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS notification_outbox_payload_immutable
BEFORE UPDATE ON notification_outbox
WHEN OLD.order_key IS NOT NEW.order_key
  OR OLD.event_type IS NOT NEW.event_type
  OR OLD.channel IS NOT NEW.channel
  OR OLD.payload_json IS NOT NEW.payload_json
  OR OLD.statistics_cutoff IS NOT NEW.statistics_cutoff
  OR OLD.template_version IS NOT NEW.template_version
  OR OLD.recipient IS NOT NEW.recipient
  OR OLD.message_id IS NOT NEW.message_id
BEGIN
    SELECT RAISE(ABORT, 'notification outbox payload is immutable');
END;
CREATE TABLE IF NOT EXISTS collector_runs (
    collector TEXT PRIMARY KEY,
    last_success_at TEXT,
    last_error_at TEXT,
    last_error TEXT,
    cursor TEXT,
    gap_detected INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS vision_observations (
    raybet_match_id TEXT NOT NULL,
    map_number INTEGER,
    captured_at TEXT NOT NULL,
    game_clock_seconds INTEGER,
    is_paused INTEGER,
    radiant_hero_ids TEXT NOT NULL,
    dire_hero_ids TEXT NOT NULL,
    radiant_team_side TEXT,
    clock_confidence REAL NOT NULL,
    draft_confidence REAL NOT NULL,
    source_frame_ref TEXT NOT NULL,
    screen_state TEXT NOT NULL,
    confirmed INTEGER NOT NULL,
    PRIMARY KEY (raybet_match_id, captured_at, source_frame_ref)
);
CREATE INDEX IF NOT EXISTS idx_vision_match_map_time
    ON vision_observations(raybet_match_id, map_number, captured_at);
CREATE TABLE IF NOT EXISTS vision_observation_invalidations (
    raybet_match_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    source_frame_ref TEXT NOT NULL,
    invalidated_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (raybet_match_id, captured_at, source_frame_ref),
    FOREIGN KEY (raybet_match_id, captured_at, source_frame_ref)
        REFERENCES vision_observations(
            raybet_match_id, captured_at, source_frame_ref
        )
);
CREATE TRIGGER IF NOT EXISTS vision_observation_invalidations_immutable_update
BEFORE UPDATE ON vision_observation_invalidations
BEGIN
    SELECT RAISE(ABORT, 'vision invalidation audit is immutable');
END;
CREATE TRIGGER IF NOT EXISTS vision_observation_invalidations_immutable_delete
BEFORE DELETE ON vision_observation_invalidations
BEGIN
    SELECT RAISE(ABORT, 'vision invalidation audit is immutable');
END;
CREATE TABLE IF NOT EXISTS vision_draft_anchors (
    raybet_match_id TEXT NOT NULL,
    map_number INTEGER NOT NULL CHECK (map_number > 0),
    draft_hash TEXT NOT NULL CHECK (length(draft_hash) = 64),
    radiant_hero_ids TEXT NOT NULL,
    dire_hero_ids TEXT NOT NULL,
    radiant_team_side TEXT CHECK (radiant_team_side IN ('team_one', 'team_two')),
    team_side_anchored_at TEXT,
    team_side_source_frame_ref TEXT,
    anchored_at TEXT NOT NULL,
    source_frame_ref TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('anchored', 'conflict')),
    conflict_at TEXT,
    PRIMARY KEY (raybet_match_id, map_number)
);
CREATE TABLE IF NOT EXISTS vision_draft_conflicts (
    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
    raybet_match_id TEXT NOT NULL,
    map_number INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    source_frame_ref TEXT NOT NULL,
    observed_draft_hash TEXT NOT NULL CHECK (length(observed_draft_hash) = 64),
    radiant_hero_ids TEXT NOT NULL,
    dire_hero_ids TEXT NOT NULL,
    observed_radiant_team_side TEXT
        CHECK (observed_radiant_team_side IN ('team_one', 'team_two')),
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (raybet_match_id, map_number, captured_at, source_frame_ref),
    FOREIGN KEY (raybet_match_id, map_number)
        REFERENCES vision_draft_anchors(raybet_match_id, map_number)
);
CREATE TRIGGER IF NOT EXISTS vision_draft_conflicts_immutable_update
BEFORE UPDATE ON vision_draft_conflicts
BEGIN
    SELECT RAISE(ABORT, 'vision draft conflict is immutable');
END;
CREATE TRIGGER IF NOT EXISTS vision_draft_conflicts_immutable_delete
BEFORE DELETE ON vision_draft_conflicts
BEGIN
    SELECT RAISE(ABORT, 'vision draft conflict is immutable');
END;
CREATE TABLE IF NOT EXISTS vision_derived_invalidations (
    dependent_type TEXT NOT NULL CHECK (dependent_type IN
        ('odds_alignment', 'strategy_decision', 'research_prediction',
         'shadow_order')),
    dependent_key TEXT NOT NULL,
    raybet_match_id TEXT NOT NULL,
    map_number INTEGER NOT NULL,
    reason TEXT NOT NULL,
    block_reason TEXT NOT NULL DEFAULT 'vision_draft_conflict',
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (dependent_type, dependent_key)
);
CREATE TRIGGER IF NOT EXISTS vision_derived_invalidations_immutable_update
BEFORE UPDATE ON vision_derived_invalidations
BEGIN
    SELECT RAISE(ABORT, 'vision derived invalidation is immutable');
END;
CREATE TRIGGER IF NOT EXISTS vision_derived_invalidations_immutable_delete
BEFORE DELETE ON vision_derived_invalidations
BEGIN
    SELECT RAISE(ABORT, 'vision derived invalidation is immutable');
END;
CREATE TABLE IF NOT EXISTS odds_alignments (
    odds_snapshot_id INTEGER PRIMARY KEY,
    raybet_match_id TEXT NOT NULL,
    map_number INTEGER,
    game_clock_seconds INTEGER,
    observation_captured_at TEXT,
    method TEXT NOT NULL,
    lag_seconds REAL,
    usable INTEGER NOT NULL,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_alignment_match_map_time
    ON odds_alignments(raybet_match_id, map_number, game_clock_seconds);
CREATE TABLE IF NOT EXISTS strategy_decisions (
    decision_key TEXT PRIMARY KEY,
    raybet_match_id TEXT NOT NULL,
    map_number INTEGER NOT NULL,
    decided_at TEXT NOT NULL,
    underdog_side TEXT NOT NULL,
    market_probability REAL NOT NULL,
    model_probability REAL NOT NULL,
    edge REAL NOT NULL,
    data_quality REAL NOT NULL,
    eligible INTEGER NOT NULL,
    reason TEXT NOT NULL,
    contributions_json TEXT NOT NULL,
    input_ref TEXT NOT NULL,
    strategy_version TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS strategy_decisions_immutable_update
BEFORE UPDATE ON strategy_decisions
BEGIN
    SELECT RAISE(ABORT, 'strategy decisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS strategy_decisions_immutable_delete
BEFORE DELETE ON strategy_decisions
BEGIN
    SELECT RAISE(ABORT, 'strategy decisions are immutable');
END;
CREATE TABLE IF NOT EXISTS prospective_draft_curves (
    curve_key TEXT PRIMARY KEY CHECK (length(curve_key)=64),
    raybet_match_id TEXT NOT NULL,
    map_number INTEGER NOT NULL CHECK (map_number > 0),
    strict_mapping_id INTEGER NOT NULL CHECK (strict_mapping_id > 0),
    lineup_hash TEXT NOT NULL CHECK (length(lineup_hash)=64),
    radiant_hero_ids_json TEXT NOT NULL,
    dire_hero_ids_json TEXT NOT NULL,
    prediction_cutoff TEXT NOT NULL,
    first_usable_at TEXT NOT NULL,
    availability_mode TEXT NOT NULL CHECK (availability_mode='prospective'),
    created_at TEXT NOT NULL,
    UNIQUE (raybet_match_id, map_number, strict_mapping_id, lineup_hash,
            first_usable_at, curve_key)
);
CREATE INDEX IF NOT EXISTS idx_prospective_draft_curve_target
    ON prospective_draft_curves(
        raybet_match_id, map_number, strict_mapping_id, lineup_hash,
        first_usable_at
    );
CREATE TABLE IF NOT EXISTS prospective_draft_landmarks (
    landmark_key TEXT PRIMARY KEY CHECK (length(landmark_key)=64),
    curve_key TEXT NOT NULL REFERENCES prospective_draft_curves(curve_key),
    horizon_minutes INTEGER NOT NULL CHECK (horizon_minutes IN (10, 20, 30, 40, 50)),
    radiant_probability REAL NOT NULL CHECK (radiant_probability BETWEEN 0.0 AND 1.0),
    scaling_edge REAL NOT NULL,
    synergy_edge REAL NOT NULL,
    quality REAL NOT NULL CHECK (quality BETWEEN 0.0 AND 1.0),
    validation_status TEXT NOT NULL
        CHECK (validation_status IN ('passed', 'failed', 'insufficient_evidence')),
    support INTEGER NOT NULL CHECK (support >= 0),
    calibration_ref TEXT NOT NULL,
    input_refs_json TEXT NOT NULL,
    uncertainty REAL CHECK (uncertainty IS NULL OR uncertainty BETWEEN 0.0 AND 0.5),
    validation_reason TEXT,
    feature_hash TEXT NOT NULL CHECK (length(feature_hash)=64),
    model_hash TEXT NOT NULL CHECK (length(model_hash)=64),
    calibration_hash TEXT NOT NULL CHECK (length(calibration_hash)=64),
    global_calibration_passed INTEGER NOT NULL
        CHECK (global_calibration_passed IN (0, 1)),
    global_gate_ref TEXT NOT NULL,
    model_version TEXT NOT NULL,
    model_kind TEXT NOT NULL CHECK (model_kind='pure_draft'),
    availability_mode TEXT NOT NULL CHECK (availability_mode='prospective'),
    input_snapshot_hash TEXT NOT NULL CHECK (length(input_snapshot_hash)=64),
    created_at TEXT NOT NULL,
    UNIQUE (curve_key, horizon_minutes)
);
CREATE TRIGGER IF NOT EXISTS prospective_draft_curves_immutable_update
BEFORE UPDATE ON prospective_draft_curves
BEGIN
    SELECT RAISE(ABORT, 'prospective draft curve is immutable');
END;
CREATE TRIGGER IF NOT EXISTS prospective_draft_curves_immutable_delete
BEFORE DELETE ON prospective_draft_curves
BEGIN
    SELECT RAISE(ABORT, 'prospective draft curve is immutable');
END;
CREATE TRIGGER IF NOT EXISTS prospective_draft_landmarks_immutable_update
BEFORE UPDATE ON prospective_draft_landmarks
BEGIN
    SELECT RAISE(ABORT, 'prospective draft landmark is immutable');
END;
CREATE TRIGGER IF NOT EXISTS prospective_draft_landmarks_immutable_delete
BEFORE DELETE ON prospective_draft_landmarks
BEGIN
    SELECT RAISE(ABORT, 'prospective draft landmark is immutable');
END;
CREATE TABLE IF NOT EXISTS shadow_map_attempts (
    raybet_match_id TEXT NOT NULL,
    map_number INTEGER NOT NULL,
    order_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (raybet_match_id, map_number)
);
CREATE TABLE IF NOT EXISTS map_results (
    raybet_match_id TEXT NOT NULL,
    map_number INTEGER NOT NULL,
    dota_match_id INTEGER NOT NULL UNIQUE,
    winner_side TEXT NOT NULL,
    team_one_kills INTEGER,
    team_two_kills INTEGER,
    duration_seconds INTEGER,
    evidence_ref TEXT NOT NULL,
    settled_at TEXT NOT NULL,
    PRIMARY KEY (raybet_match_id, map_number)
);
CREATE TABLE IF NOT EXISTS research_live_predictions (
    prediction_key TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    raybet_match_id TEXT NOT NULL,
    map_number INTEGER NOT NULL CHECK (map_number BETWEEN 1 AND 10),
    observed_at TEXT NOT NULL,
    game_clock_seconds INTEGER NOT NULL CHECK (game_clock_seconds >= 0),
    game_minute REAL NOT NULL CHECK (game_minute >= 0.0),
    selected_side TEXT NOT NULL CHECK (selected_side IN ('team_one', 'team_two')),
    market_probability REAL NOT NULL CHECK (market_probability BETWEEN 0.0 AND 1.0),
    market_price REAL NOT NULL CHECK (market_price > 1.0),
    raw_model_probability REAL
        CHECK (raw_model_probability IS NULL OR raw_model_probability BETWEEN 0.0 AND 1.0),
    feature_hash TEXT CHECK (feature_hash IS NULL OR length(feature_hash)=64),
    model_hash TEXT CHECK (model_hash IS NULL OR length(model_hash)=64),
    calibration_hash TEXT CHECK (calibration_hash IS NULL OR length(calibration_hash)=64),
    transport_key TEXT NOT NULL REFERENCES odds_transport_observations(observation_key),
    transport_hash TEXT NOT NULL CHECK (length(transport_hash)=64),
    radiant_hero_ids_json TEXT NOT NULL,
    dire_hero_ids_json TEXT NOT NULL,
    radiant_team_side TEXT CHECK (radiant_team_side IN ('team_one', 'team_two')),
    strict_mapping_id INTEGER NOT NULL,
    clock_source TEXT NOT NULL CHECK (clock_source='vision'),
    clock_trust TEXT NOT NULL CHECK (clock_trust='trusted_vision'),
    manual_clock_event_id TEXT REFERENCES browser_events(event_id),
    manual_clock_seconds INTEGER
        CHECK (manual_clock_seconds IS NULL OR manual_clock_seconds >= 0),
    manual_clock_trust TEXT NOT NULL
        CHECK (manual_clock_trust IN ('not_observed', 'diagnostic_untrusted')),
    manual_clock_validation TEXT NOT NULL,
    actionability TEXT NOT NULL CHECK (actionability='research_only'),
    gate_status TEXT NOT NULL CHECK (gate_status IN ('unavailable', 'failed', 'passed')),
    gate_failures_json TEXT NOT NULL,
    input_context_hash TEXT NOT NULL CHECK (length(input_context_hash)=64),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_prediction_match_time
    ON research_live_predictions(raybet_match_id, map_number, observed_at);
CREATE TABLE IF NOT EXISTS research_price_labels (
    label_key TEXT PRIMARY KEY,
    prediction_key TEXT NOT NULL UNIQUE
        REFERENCES research_live_predictions(prediction_key),
    transport_key TEXT NOT NULL REFERENCES odds_transport_observations(observation_key),
    transport_hash TEXT NOT NULL CHECK (length(transport_hash)=64),
    observed_at TEXT NOT NULL,
    selected_side TEXT NOT NULL CHECK (selected_side IN ('team_one', 'team_two')),
    price REAL NOT NULL CHECK (price > 1.0),
    market_probability REAL NOT NULL CHECK (market_probability BETWEEN 0.0 AND 1.0),
    seconds_after_prediction REAL NOT NULL CHECK (seconds_after_prediction > 0.0),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_price_transport
    ON research_price_labels(transport_key, observed_at);
CREATE TABLE IF NOT EXISTS research_result_labels (
    label_key TEXT PRIMARY KEY,
    prediction_key TEXT NOT NULL UNIQUE
        REFERENCES research_live_predictions(prediction_key),
    winner_side TEXT NOT NULL CHECK (winner_side IN ('team_one', 'team_two')),
    selected_side_win INTEGER NOT NULL CHECK (selected_side_win IN (0, 1)),
    dota_match_id INTEGER NOT NULL,
    evidence_ref TEXT NOT NULL,
    settled_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS research_live_predictions_no_update
BEFORE UPDATE ON research_live_predictions
BEGIN
    SELECT RAISE(ABORT, 'research prediction is append-only');
END;
CREATE TRIGGER IF NOT EXISTS research_live_predictions_no_delete
BEFORE DELETE ON research_live_predictions
BEGIN
    SELECT RAISE(ABORT, 'research prediction is append-only');
END;
CREATE TRIGGER IF NOT EXISTS research_price_labels_no_update
BEFORE UPDATE ON research_price_labels
BEGIN
    SELECT RAISE(ABORT, 'research price label is append-only');
END;
CREATE TRIGGER IF NOT EXISTS research_price_labels_no_delete
BEFORE DELETE ON research_price_labels
BEGIN
    SELECT RAISE(ABORT, 'research price label is append-only');
END;
CREATE TRIGGER IF NOT EXISTS research_result_labels_no_update
BEFORE UPDATE ON research_result_labels
BEGIN
    SELECT RAISE(ABORT, 'research result label is append-only');
END;
CREATE TRIGGER IF NOT EXISTS research_result_labels_no_delete
BEFORE DELETE ON research_result_labels
BEGIN
    SELECT RAISE(ABORT, 'research result label is append-only');
END;
DROP TRIGGER IF EXISTS research_result_from_map_result;
CREATE TRIGGER research_result_from_map_result
AFTER INSERT ON map_results
BEGIN
    INSERT OR IGNORE INTO research_result_labels
        (label_key, prediction_key, winner_side, selected_side_win,
         dota_match_id, evidence_ref, settled_at, created_at)
    SELECT prediction_key || ':result', prediction_key, NEW.winner_side,
           CASE WHEN selected_side=NEW.winner_side THEN 1 ELSE 0 END,
           NEW.dota_match_id, NEW.evidence_ref, NEW.settled_at, NEW.settled_at
      FROM research_live_predictions
     WHERE raybet_match_id=NEW.raybet_match_id AND map_number=NEW.map_number
       AND julianday(observed_at) < julianday(NEW.settled_at);
END;
DROP TRIGGER IF EXISTS research_result_from_late_prediction;
CREATE TRIGGER research_result_from_late_prediction
AFTER INSERT ON research_live_predictions
BEGIN
    INSERT OR IGNORE INTO research_result_labels
        (label_key, prediction_key, winner_side, selected_side_win,
         dota_match_id, evidence_ref, settled_at, created_at)
    SELECT NEW.prediction_key || ':result', NEW.prediction_key, result.winner_side,
           CASE WHEN NEW.selected_side=result.winner_side THEN 1 ELSE 0 END,
           result.dota_match_id, result.evidence_ref, result.settled_at,
           NEW.created_at
      FROM map_results AS result
     WHERE result.raybet_match_id=NEW.raybet_match_id
       AND result.map_number=NEW.map_number
       AND julianday(NEW.observed_at) < julianday(result.settled_at);
END;
"""


def strict_mapping_context_block_reason(
    connection: sqlite3.Connection,
    *,
    strict_mapping_id: int,
    raybet_match_id: str,
    map_number: int,
    signal_transport_at: datetime | str,
) -> str | None:
    """Return a stable gate code for one causally bound strict mapping."""
    try:
        transport_at = (
            signal_transport_at
            if isinstance(signal_transport_at, datetime)
            else datetime.fromisoformat(str(signal_transport_at).replace("Z", "+00:00"))
        )
        if transport_at.tzinfo is None or transport_at.utcoffset() is None:
            return "strict_mapping_unverified"
        transport_at = transport_at.astimezone(timezone.utc)
        from .strict_eligibility import query_strict_live_eligibility

        eligibility = query_strict_live_eligibility(
            connection,
            raybet_match_id=raybet_match_id,
            map_number=map_number,
            transport_observed_at=transport_at,
        )
    except (sqlite3.Error, TypeError, ValueError, OverflowError):
        return "strict_mapping_gate_unavailable"
    if not eligibility.eligible:
        if eligibility.reason in {
            "mapping_invalidated",
            "automatic_exact_approval_invalidated",
        }:
            return "strict_mapping_invalidated"
        if eligibility.reason.endswith("_schema_missing"):
            return "strict_mapping_gate_unavailable"
        return "strict_mapping_unverified"
    if (
        eligibility.mapping is None
        or eligibility.mapping.mapping_id != strict_mapping_id
    ):
        return "strict_mapping_unverified"
    return None


def strict_order_mapping_block_reason(
    connection: sqlite3.Connection,
    order_key: str,
    *,
    require_order: bool = False,
) -> str | None:
    """Apply the shared strict gate to a persisted order's causal inputs."""
    try:
        order = connection.execute(
            """SELECT strict_mapping_id, raybet_match_id, signal_transport_at
                 FROM shadow_orders WHERE order_key=?""",
            (order_key,),
        ).fetchone()
    except sqlite3.Error:
        return "strict_mapping_gate_unavailable"
    if order is None:
        return "strict_mapping_unverified" if require_order else None
    if order["strict_mapping_id"] is None:
        return "strict_mapping_unverified"
    try:
        attempt = connection.execute(
            "SELECT map_number FROM shadow_map_attempts WHERE order_key=?",
            (order_key,),
        ).fetchone()
        if attempt is None:
            return "strict_mapping_unverified"
        impacted = connection.execute(
            """SELECT 1 FROM strict_live_mapping_impacts
                WHERE dependent_type='shadow_order' AND dependent_key=?
                LIMIT 1""",
            (order_key,),
        ).fetchone()
    except sqlite3.Error:
        return "strict_mapping_gate_unavailable"
    if impacted is not None:
        return "strict_mapping_invalidated"
    try:
        strict_mapping_id = int(order["strict_mapping_id"])
        map_number = int(attempt["map_number"])
    except (TypeError, ValueError, OverflowError):
        return "strict_mapping_unverified"
    if strict_mapping_id <= 0 or map_number <= 0:
        return "strict_mapping_unverified"
    return strict_mapping_context_block_reason(
        connection,
        strict_mapping_id=strict_mapping_id,
        raybet_match_id=str(order["raybet_match_id"]),
        map_number=map_number,
        signal_transport_at=order["signal_transport_at"],
    )


class LiveBettingStore:
    def __init__(
        self,
        path: str | Path,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self.path = str(path)
        self.connection = (
            connection
            if connection is not None
            else sqlite3.connect(self.path, timeout=5.0)
        )
        self._owns_connection = connection is None
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        journal_mode = str(
            self.connection.execute("PRAGMA journal_mode").fetchone()[0]
        ).lower()
        if journal_mode != "wal":
            self.connection.execute("PRAGMA journal_mode=WAL")
        self._transaction_depth = 0
        self._savepoint_sequence = 0

    def close(self) -> None:
        if self._owns_connection:
            self.connection.close()

    def __enter__(self) -> "LiveBettingStore":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def init_schema(self) -> None:
        self._reject_future_schema()
        self.connection.executescript(
            """DROP TRIGGER IF EXISTS shadow_orders_terminal_immutable;
               DROP TRIGGER IF EXISTS shadow_orders_immutable_delete;
               DROP TRIGGER IF EXISTS settlements_core_immutable;
               DROP TRIGGER IF EXISTS settlements_immutable_delete;"""
        )
        self.connection.executescript(SCHEMA_SQL)
        self._migrate_shadow_order_signal_fields()
        self._migrate_vision_map_identity_fields()
        self._migrate_vision_derived_invalidation_fields()
        self.connection.commit()
        columns = {row[1] for row in self.connection.execute(
            "PRAGMA table_info(vision_observations)"
        )}
        if "radiant_team_side" not in columns:
            self.connection.execute(
                "ALTER TABLE vision_observations ADD COLUMN radiant_team_side TEXT"
            )
        from .strict_eligibility import init_strict_live_eligibility_schema

        init_strict_live_eligibility_schema(self.connection)
        self._migrate_shadow_order_decision_lineage()
        self._init_ledger_immutability_triggers()
        self.connection.execute(
            """INSERT OR IGNORE INTO live_schema_version (version, applied_at)
               VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))""",
            (CURRENT_SCHEMA_VERSION,),
        )
        self.connection.commit()

    def _reject_future_schema(self) -> None:
        exists = self.connection.execute(
            """SELECT 1 FROM sqlite_master
                 WHERE type='table' AND name='live_schema_version'"""
        ).fetchone()
        if exists is None:
            return
        row = self.connection.execute(
            "SELECT MAX(version) FROM live_schema_version"
        ).fetchone()
        version = row[0] if row is not None else None
        if version is not None and int(version) > CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                f"database live schema version {version} is newer than supported "
                f"version {CURRENT_SCHEMA_VERSION}"
            )

    def _init_ledger_immutability_triggers(self) -> None:
        self.connection.executescript(
            """
            CREATE TRIGGER shadow_orders_terminal_immutable
            BEFORE UPDATE ON shadow_orders
            WHEN NOT (
                OLD.order_key IS NEW.order_key
                AND OLD.raybet_match_id IS NEW.raybet_match_id
                AND OLD.strict_mapping_id IS NEW.strict_mapping_id
                AND OLD.odds_id IS NEW.odds_id
                AND OLD.market_key IS NEW.market_key
                AND OLD.signaled_at IS NEW.signaled_at
                AND OLD.model_probability IS NEW.model_probability
                AND OLD.market_probability IS NEW.market_probability
                AND OLD.signal_price IS NEW.signal_price
                AND OLD.signal_transport_key IS NEW.signal_transport_key
                AND OLD.signal_transport_at IS NEW.signal_transport_at
                AND OLD.expires_at IS NEW.expires_at
                AND OLD.signal_odds_group_id IS NEW.signal_odds_group_id
                AND OLD.signal_outcome_key IS NEW.signal_outcome_key
                AND OLD.signal_identity_verified IS NEW.signal_identity_verified
                AND OLD.stake IS NEW.stake
                AND OLD.status='pending'
                AND OLD.fill_price IS NULL
                AND OLD.filled_at IS NULL
                AND OLD.rejection_reason IS NULL
                AND (
                    (
                        NEW.status='pending'
                        AND NEW.fill_price IS NULL
                        AND NEW.filled_at IS NULL
                        AND NEW.rejection_reason IS NULL
                    )
                    OR (
                        NEW.status='filled'
                        AND typeof(NEW.fill_price) IN ('integer', 'real')
                        AND NEW.fill_price>1.0
                        AND NEW.filled_at IS NOT NULL
                        AND NEW.filled_at!=''
                        AND NEW.rejection_reason IS NULL
                    )
                    OR (
                        NEW.status='rejected'
                        AND NEW.fill_price IS NULL
                        AND NEW.filled_at IS NULL
                        AND NEW.rejection_reason IS NOT NULL
                        AND NEW.rejection_reason!=''
                    )
                )
            )
            BEGIN
                SELECT RAISE(ABORT, 'shadow order terminal state is immutable');
            END;
            CREATE TRIGGER shadow_orders_immutable_delete
            BEFORE DELETE ON shadow_orders
            BEGIN
                SELECT RAISE(ABORT, 'shadow orders are immutable');
            END;
            CREATE TRIGGER settlements_core_immutable
            BEFORE UPDATE ON settlements
            WHEN NOT (
                OLD.order_key IS NEW.order_key
                AND OLD.result IS NEW.result
                AND OLD.return_units IS NEW.return_units
                AND OLD.settled_at IS NEW.settled_at
                AND OLD.evidence_ref IS NEW.evidence_ref
                AND (
                    OLD.review_required IS NEW.review_required
                    OR (OLD.review_required=0 AND NEW.review_required=1)
                )
            )
            BEGIN
                SELECT RAISE(ABORT, 'settlement core state is immutable');
            END;
            CREATE TRIGGER settlements_immutable_delete
            BEFORE DELETE ON settlements
            BEGIN
                SELECT RAISE(ABORT, 'settlements are immutable');
            END;
            """
        )

    def _migrate_shadow_order_signal_fields(self) -> None:
        """Add strict signal identity to databases created by earlier versions."""
        self.connection.execute(
            "DROP TRIGGER IF EXISTS shadow_orders_signal_identity_immutable"
        )
        columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(shadow_orders)")
        }
        additive_columns = {
            "strict_mapping_id": "INTEGER",
            "signal_transport_key": "TEXT NOT NULL DEFAULT ''",
            "signal_transport_at": "TEXT NOT NULL DEFAULT ''",
            "expires_at": "TEXT NOT NULL DEFAULT ''",
            "signal_odds_group_id": "TEXT",
            "signal_outcome_key": "TEXT",
            "signal_identity_verified": (
                "INTEGER NOT NULL DEFAULT 0 "
                "CHECK (signal_identity_verified IN (0, 1))"
            ),
        }
        for name, definition in additive_columns.items():
            if name not in columns:
                self.connection.execute(
                    f"ALTER TABLE shadow_orders ADD COLUMN {name} {definition}"
                )

        rows = self.connection.execute(
            """SELECT order_key, raybet_match_id, signaled_at
                 FROM shadow_orders
                WHERE signal_transport_key IS NULL OR signal_transport_key=''
                   OR signal_transport_at IS NULL OR signal_transport_at=''
                   OR expires_at IS NULL OR expires_at=''"""
        ).fetchall()
        for row in rows:
            signaled_at = datetime.fromisoformat(str(row["signaled_at"]))
            signal_time = self._iso(signaled_at)
            self.connection.execute(
                """UPDATE shadow_orders
                      SET signal_transport_key=?, signal_transport_at=?, expires_at=?
                    WHERE order_key=?""",
                (
                    f"legacy:{row['order_key']}",
                    signal_time,
                    self._iso(signaled_at + timedelta(seconds=15)),
                    str(row["order_key"]),
                ),
            )

        identity_rows = self.connection.execute(
            """SELECT order_key, raybet_match_id, odds_id, market_key,
                      signal_price, signal_transport_key, signal_transport_at
                 FROM shadow_orders
                WHERE signal_identity_verified!=1
                   OR signal_odds_group_id IS NULL OR signal_odds_group_id=''
                   OR signal_outcome_key IS NULL OR signal_outcome_key=''"""
        ).fetchall()
        for row in identity_rows:
            outcome = self.connection.execute(
                """SELECT outcome.odds_group_id, outcome.outcome_key,
                          outcome.price, outcome.status, outcome.supported,
                          outcome.market_type, outcome.period, outcome.side,
                          outcome.line
                     FROM odds_response_outcomes outcome
                     JOIN odds_transport_observations transport
                       ON transport.observation_key=outcome.observation_key
                    WHERE outcome.observation_key=?
                      AND outcome.raybet_match_id=? AND outcome.odds_id=?
                      AND transport.observed_at=?
                      AND transport.timing_status='on_time'
                      AND transport.processing_status='processed'""",
                (
                    str(row["signal_transport_key"]),
                    str(row["raybet_match_id"]),
                    str(row["odds_id"]),
                    str(row["signal_transport_at"]),
                ),
            ).fetchone()
            identity_is_proven = (
                outcome is not None
                and bool(str(outcome["odds_group_id"] or ""))
                and bool(str(outcome["outcome_key"]))
                and float(outcome["price"]) == float(row["signal_price"])
                and is_open(outcome["status"])
                and bool(outcome["supported"])
                and market_key(
                    str(outcome["market_type"]),
                    str(outcome["period"]),
                    outcome["side"],
                    outcome["line"],
                )
                == str(row["market_key"])
            )
            self.connection.execute(
                """UPDATE shadow_orders
                      SET signal_odds_group_id=?, signal_outcome_key=?,
                          signal_identity_verified=?
                    WHERE order_key=?""",
                (
                    outcome["odds_group_id"] if identity_is_proven else None,
                    str(outcome["outcome_key"])
                    if identity_is_proven
                    else None,
                    int(identity_is_proven),
                    str(row["order_key"]),
                ),
            )

        signal_columns = {
            str(row[1]): (int(row[3]), row[4])
            for row in self.connection.execute("PRAGMA table_info(shadow_orders)")
            if str(row[1]) in additive_columns
        }
        expected_columns = {
            "strict_mapping_id": (0, None),
            "signal_transport_key": (1, None),
            "signal_transport_at": (1, None),
            "expires_at": (1, None),
            "signal_odds_group_id": (0, None),
            "signal_outcome_key": (0, None),
            "signal_identity_verified": (1, None),
        }
        if signal_columns != expected_columns:
            self.connection.execute("DROP TABLE IF EXISTS shadow_orders_strict_migration")
            self.connection.execute(
                """CREATE TABLE shadow_orders_strict_migration (
                    order_key TEXT PRIMARY KEY,
                    raybet_match_id TEXT NOT NULL,
                    strict_mapping_id INTEGER,
                    odds_id TEXT NOT NULL,
                    market_key TEXT NOT NULL,
                    signaled_at TEXT NOT NULL,
                    model_probability REAL NOT NULL,
                    market_probability REAL NOT NULL,
                    signal_price REAL NOT NULL,
                    signal_transport_key TEXT NOT NULL,
                    signal_transport_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    signal_odds_group_id TEXT,
                    signal_outcome_key TEXT,
                    signal_identity_verified INTEGER NOT NULL
                        CHECK (signal_identity_verified IN (0, 1)),
                    stake REAL NOT NULL,
                    status TEXT NOT NULL,
                    fill_price REAL,
                    filled_at TEXT,
                    rejection_reason TEXT
                )"""
            )
            self.connection.execute(
                """INSERT INTO shadow_orders_strict_migration
                    (order_key, raybet_match_id, strict_mapping_id, odds_id,
                     market_key, signaled_at,
                     model_probability, market_probability, signal_price,
                     signal_transport_key, signal_transport_at, expires_at,
                     signal_odds_group_id, signal_outcome_key,
                     signal_identity_verified, stake, status, fill_price,
                     filled_at, rejection_reason)
                    SELECT order_key, raybet_match_id, strict_mapping_id,
                           odds_id, market_key, signaled_at,
                           model_probability, market_probability,
                           signal_price, signal_transport_key,
                           signal_transport_at, expires_at,
                           signal_odds_group_id, signal_outcome_key,
                           signal_identity_verified, stake, status, fill_price,
                           filled_at, rejection_reason
                      FROM shadow_orders"""
            )
            self.connection.execute("DROP TABLE shadow_orders")
            self.connection.execute(
                "ALTER TABLE shadow_orders_strict_migration RENAME TO shadow_orders"
            )

        self.connection.executescript(
            """
            DROP TRIGGER IF EXISTS shadow_orders_require_signal_insert;
            DROP TRIGGER IF EXISTS shadow_orders_require_signal_update;
            DROP TRIGGER IF EXISTS shadow_orders_signal_identity_immutable;
            CREATE TRIGGER IF NOT EXISTS shadow_orders_require_signal_insert
            BEFORE INSERT ON shadow_orders
            WHEN NEW.signal_transport_key IS NULL OR NEW.signal_transport_key=''
              OR NEW.signal_transport_at IS NULL OR NEW.signal_transport_at=''
              OR NEW.expires_at IS NULL OR NEW.expires_at=''
              OR NEW.signal_identity_verified!=1
              OR NEW.signal_odds_group_id IS NULL OR NEW.signal_odds_group_id=''
              OR NEW.signal_outcome_key IS NULL OR NEW.signal_outcome_key=''
            BEGIN
                SELECT RAISE(ABORT, 'shadow order signal identity is required');
            END;
            CREATE TRIGGER IF NOT EXISTS shadow_orders_require_signal_update
            BEFORE UPDATE ON shadow_orders
            WHEN NEW.signal_transport_key IS NULL OR NEW.signal_transport_key=''
              OR NEW.signal_transport_at IS NULL OR NEW.signal_transport_at=''
              OR NEW.expires_at IS NULL OR NEW.expires_at=''
            BEGIN
                SELECT RAISE(ABORT, 'shadow order signal identity is required');
            END;
            CREATE TRIGGER shadow_orders_signal_identity_immutable
            BEFORE UPDATE ON shadow_orders
            WHEN OLD.raybet_match_id IS NOT NEW.raybet_match_id
              OR OLD.strict_mapping_id IS NOT NEW.strict_mapping_id
              OR OLD.odds_id IS NOT NEW.odds_id
              OR OLD.market_key IS NOT NEW.market_key
              OR OLD.signaled_at IS NOT NEW.signaled_at
              OR OLD.signal_price IS NOT NEW.signal_price
              OR OLD.signal_transport_key IS NOT NEW.signal_transport_key
              OR OLD.signal_transport_at IS NOT NEW.signal_transport_at
              OR OLD.expires_at IS NOT NEW.expires_at
              OR OLD.signal_odds_group_id IS NOT NEW.signal_odds_group_id
              OR OLD.signal_outcome_key IS NOT NEW.signal_outcome_key
              OR OLD.signal_identity_verified IS NOT NEW.signal_identity_verified
            BEGIN
                SELECT RAISE(ABORT, 'shadow order signal identity is immutable');
            END;
            DROP TRIGGER IF EXISTS shadow_orders_require_strict_mapping_insert;
            CREATE TRIGGER shadow_orders_require_strict_mapping_insert
            BEFORE INSERT ON shadow_orders
            WHEN NEW.strict_mapping_id IS NULL
              OR typeof(NEW.strict_mapping_id)!='integer'
              OR NEW.strict_mapping_id<=0
            BEGIN
                SELECT RAISE(ABORT, 'shadow order strict mapping is required');
            END;
            """
        )

    @staticmethod
    def _stored_shadow_order(row: sqlite3.Row) -> ShadowOrder:
        market_parts = str(row["market_key"]).split("|")
        if len(market_parts) != 4:
            raise ValueError("stored shadow market key is invalid")
        line = float(market_parts[3]) if market_parts[3] else None
        market = Market(
            market_parts[0],
            market_parts[1],
            market_parts[2] or None,
            line,
            str(row["signal_outcome_key"] or ""),
            True,
        )
        return ShadowOrder(
            order_key=str(row["order_key"]),
            raybet_match_id=str(row["raybet_match_id"]),
            odds_id=str(row["odds_id"]),
            market=market,
            signaled_at=datetime.fromisoformat(str(row["signaled_at"])),
            model_probability=float(row["model_probability"]),
            market_probability=float(row["market_probability"]),
            signal_price=float(row["signal_price"]),
            signal_transport_key=str(row["signal_transport_key"]),
            signal_transport_at=datetime.fromisoformat(
                str(row["signal_transport_at"])
            ),
            expires_at=datetime.fromisoformat(str(row["expires_at"])),
            signal_odds_group_id=row["signal_odds_group_id"],
            signal_outcome_key=row["signal_outcome_key"],
            signal_identity_verified=bool(row["signal_identity_verified"]),
            stake=float(row["stake"]),
            status=str(row["status"]),
            fill_price=(
                float(row["fill_price"]) if row["fill_price"] is not None else None
            ),
            filled_at=(
                datetime.fromisoformat(str(row["filled_at"]))
                if row["filled_at"] is not None
                else None
            ),
            rejection_reason=row["rejection_reason"],
        )

    def _restore_shadow_order_signal_identity_trigger(self) -> None:
        self.connection.executescript(
            """DROP TRIGGER IF EXISTS shadow_orders_signal_identity_immutable;
               CREATE TRIGGER shadow_orders_signal_identity_immutable
               BEFORE UPDATE ON shadow_orders
               WHEN OLD.raybet_match_id IS NOT NEW.raybet_match_id
                 OR OLD.strict_mapping_id IS NOT NEW.strict_mapping_id
                 OR OLD.odds_id IS NOT NEW.odds_id
                 OR OLD.market_key IS NOT NEW.market_key
                 OR OLD.signaled_at IS NOT NEW.signaled_at
                 OR OLD.signal_price IS NOT NEW.signal_price
                 OR OLD.signal_transport_key IS NOT NEW.signal_transport_key
                 OR OLD.signal_transport_at IS NOT NEW.signal_transport_at
                 OR OLD.expires_at IS NOT NEW.expires_at
                 OR OLD.signal_odds_group_id IS NOT NEW.signal_odds_group_id
                 OR OLD.signal_outcome_key IS NOT NEW.signal_outcome_key
                 OR OLD.signal_identity_verified IS NOT NEW.signal_identity_verified
               BEGIN
                   SELECT RAISE(ABORT, 'shadow order signal identity is immutable');
               END;"""
        )

    def _migrate_shadow_order_decision_lineage(self) -> None:
        """Backfill only uniquely proven lineage and quarantine everything else."""
        rows = self.connection.execute(
            """SELECT orders.*, attempt.map_number,
                      lineage.decision_key AS recorded_decision_key
                 FROM shadow_orders AS orders
                 LEFT JOIN shadow_map_attempts AS attempt
                   ON attempt.order_key=orders.order_key
                 LEFT JOIN shadow_order_decision_lineage AS lineage
                   ON lineage.order_key=orders.order_key
                ORDER BY orders.order_key"""
        ).fetchall()
        quarantine_time = datetime.now(timezone.utc)
        quarantined_at = quarantine_time.isoformat()
        for row in rows:
            map_number = int(row["map_number"] or 0)
            strict_mapping_id = row["strict_mapping_id"]
            expected_mapping_id = (
                int(strict_mapping_id)
                if type(strict_mapping_id) is int and int(strict_mapping_id) > 0
                else None
            )
            try:
                order = self._stored_shadow_order(row)
                candidates = (
                    self._matching_strategy_decision_candidates(
                        order, map_number
                    )
                    if map_number > 0
                    else []
                )
            except (TypeError, ValueError, OverflowError):
                candidates = []
            recorded_key = row["recorded_decision_key"]
            proven = (
                len(candidates) == 1
                and (
                    expected_mapping_id is None
                    or expected_mapping_id == candidates[0][1]
                )
                and (
                    recorded_key is None
                    or str(recorded_key) == candidates[0][0]
                )
            )
            if proven:
                decision_key, mapping_id = candidates[0]
                if expected_mapping_id is None:
                    self.connection.execute(
                        "DROP TRIGGER shadow_orders_signal_identity_immutable"
                    )
                    try:
                        self.connection.execute(
                            """UPDATE shadow_orders SET strict_mapping_id=?
                                WHERE order_key=?""",
                            (mapping_id, str(row["order_key"])),
                        )
                    finally:
                        self._restore_shadow_order_signal_identity_trigger()
                if recorded_key is None:
                    self.connection.execute(
                        """INSERT INTO shadow_order_decision_lineage
                           (order_key, decision_key, recorded_at)
                           VALUES (?, ?, ?)""",
                        (
                            str(row["order_key"]),
                            decision_key,
                            str(row["signaled_at"]),
                        ),
                    )
                continue

            order_key = str(row["order_key"])
            if str(row["status"]) == "pending":
                self.connection.execute(
                    """UPDATE shadow_orders
                          SET status='rejected', fill_price=NULL, filled_at=NULL,
                              rejection_reason='decision_lineage_unavailable'
                        WHERE order_key=? AND status='pending'""",
                    (order_key,),
                )
                self.connection.execute(
                    """UPDATE shadow_map_attempts
                          SET status='rejected' WHERE order_key=? AND status='pending'""",
                    (order_key,),
                )
            self.connection.execute(
                """INSERT OR IGNORE INTO vision_derived_invalidations
                   (dependent_type, dependent_key, raybet_match_id, map_number,
                    reason, block_reason, recorded_at)
                   VALUES ('shadow_order', ?, ?, ?, ?, ?, ?)""",
                (
                    order_key,
                    str(row["raybet_match_id"]),
                    map_number,
                    "schema_v4_decision_lineage_unavailable",
                    "decision_lineage_unavailable",
                    quarantined_at,
                ),
            )
            self.connection.execute(
                "UPDATE settlements SET review_required=1 WHERE order_key=?",
                (order_key,),
            )
            outbox_rows = self.connection.execute(
                """SELECT outbox_id FROM notification_outbox
                    WHERE order_key=? AND status IN ('pending', 'leased')""",
                (order_key,),
            ).fetchall()
            for outbox_row in outbox_rows:
                outbox_id = int(outbox_row["outbox_id"])
                self.quarantine_notification(
                    outbox_id=outbox_id,
                    reason="decision_lineage_unavailable",
                    actor="schema_v4_migration",
                    now=quarantine_time,
                )

    def _migrate_vision_map_identity_fields(self) -> None:
        """Add team-side identity without guessing values for legacy anchors."""
        self.connection.execute(
            "DROP TRIGGER IF EXISTS vision_draft_anchor_identity_immutable"
        )
        self.connection.execute(
            "DROP TRIGGER IF EXISTS vision_draft_anchor_insert_valid"
        )
        anchor_columns = {
            str(row[1])
            for row in self.connection.execute(
                "PRAGMA table_info(vision_draft_anchors)"
            )
        }
        anchor_additions = {
            "radiant_team_side": (
                "TEXT CHECK (radiant_team_side IN ('team_one', 'team_two'))"
            ),
            "team_side_anchored_at": "TEXT",
            "team_side_source_frame_ref": "TEXT",
        }
        for name, definition in anchor_additions.items():
            if name not in anchor_columns:
                self.connection.execute(
                    f"ALTER TABLE vision_draft_anchors ADD COLUMN {name} {definition}"
                )

        conflict_columns = {
            str(row[1])
            for row in self.connection.execute(
                "PRAGMA table_info(vision_draft_conflicts)"
            )
        }
        if "observed_radiant_team_side" not in conflict_columns:
            self.connection.execute(
                """ALTER TABLE vision_draft_conflicts
                   ADD COLUMN observed_radiant_team_side TEXT
                   CHECK (observed_radiant_team_side IN ('team_one', 'team_two'))"""
            )

        self.connection.executescript(
            """
            CREATE TRIGGER vision_draft_anchor_insert_valid
            BEFORE INSERT ON vision_draft_anchors
            WHEN NEW.status!='anchored'
              OR NEW.conflict_at IS NOT NULL
              OR (
                    NEW.radiant_team_side IS NULL
                    AND (
                        NEW.team_side_anchored_at IS NOT NULL
                        OR NEW.team_side_source_frame_ref IS NOT NULL
                    )
              )
              OR (
                    NEW.radiant_team_side IS NOT NULL
                    AND (
                        NEW.team_side_anchored_at IS NULL
                        OR NEW.team_side_source_frame_ref IS NULL
                        OR NEW.team_side_source_frame_ref=''
                    )
              )
            BEGIN
                SELECT RAISE(ABORT, 'vision draft anchor identity is invalid');
            END;

            CREATE TRIGGER vision_draft_anchor_identity_immutable
            BEFORE UPDATE ON vision_draft_anchors
            WHEN NOT (
                (
                    OLD.status='anchored'
                    AND NEW.status='conflict'
                    AND OLD.raybet_match_id IS NEW.raybet_match_id
                    AND OLD.map_number IS NEW.map_number
                    AND OLD.draft_hash IS NEW.draft_hash
                    AND OLD.radiant_hero_ids IS NEW.radiant_hero_ids
                    AND OLD.dire_hero_ids IS NEW.dire_hero_ids
                    AND OLD.radiant_team_side IS NEW.radiant_team_side
                    AND OLD.team_side_anchored_at IS NEW.team_side_anchored_at
                    AND OLD.team_side_source_frame_ref
                        IS NEW.team_side_source_frame_ref
                    AND OLD.anchored_at IS NEW.anchored_at
                    AND OLD.source_frame_ref IS NEW.source_frame_ref
                    AND OLD.conflict_at IS NULL
                    AND NEW.conflict_at IS NOT NULL
                )
                OR
                (
                    OLD.status='anchored'
                    AND NEW.status='anchored'
                    AND OLD.raybet_match_id IS NEW.raybet_match_id
                    AND OLD.map_number IS NEW.map_number
                    AND OLD.draft_hash IS NEW.draft_hash
                    AND OLD.radiant_hero_ids IS NEW.radiant_hero_ids
                    AND OLD.dire_hero_ids IS NEW.dire_hero_ids
                    AND OLD.radiant_team_side IS NULL
                    AND (
                        NEW.radiant_team_side IS 'team_one'
                        OR NEW.radiant_team_side IS 'team_two'
                    )
                    AND OLD.team_side_anchored_at IS NULL
                    AND NEW.team_side_anchored_at IS NOT NULL
                    AND OLD.team_side_source_frame_ref IS NULL
                    AND NEW.team_side_source_frame_ref IS NOT NULL
                    AND NEW.team_side_source_frame_ref!=''
                    AND OLD.anchored_at IS NEW.anchored_at
                    AND OLD.source_frame_ref IS NEW.source_frame_ref
                    AND OLD.conflict_at IS NEW.conflict_at
                )
            )
            BEGIN
                SELECT RAISE(ABORT, 'vision draft anchor is immutable');
            END;
            """
        )

    def _migrate_vision_derived_invalidation_fields(self) -> None:
        """Add a stable gate code to invalidation rows from older schemas."""
        columns = {
            str(row[1])
            for row in self.connection.execute(
                "PRAGMA table_info(vision_derived_invalidations)"
            )
        }
        if "block_reason" not in columns:
            self.connection.execute(
                """ALTER TABLE vision_derived_invalidations
                   ADD COLUMN block_reason TEXT NOT NULL
                   DEFAULT 'vision_draft_conflict'"""
            )

    @staticmethod
    def json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        try:
            cursor = self.connection.execute(sql, parameters)
        except Exception:
            if self._transaction_depth == 0:
                self.connection.rollback()
            raise
        if self._transaction_depth == 0:
            self.connection.commit()
        return cursor

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Commit a unit of work atomically while supporting nested callers."""
        if self._transaction_depth:
            self._savepoint_sequence += 1
            name = f"transaction_{self._savepoint_sequence}"
            with self.savepoint(name):
                self._transaction_depth += 1
                try:
                    yield
                finally:
                    self._transaction_depth -= 1
            return

        self.connection.execute("BEGIN IMMEDIATE")
        self._transaction_depth = 1
        try:
            yield
        except Exception:
            self.connection.rollback()
            raise
        else:
            try:
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        finally:
            self._transaction_depth = 0

    @contextmanager
    def savepoint(self, name: str) -> Iterator[None]:
        """Create a named rollback boundary inside an active transaction."""
        if self._transaction_depth == 0:
            raise RuntimeError("savepoint requires an active transaction")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("invalid savepoint name")
        self.connection.execute(f"SAVEPOINT {name}")
        try:
            yield
        except Exception:
            self.connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
            self.connection.execute(f"RELEASE SAVEPOINT {name}")
            raise
        else:
            self.connection.execute(f"RELEASE SAVEPOINT {name}")

    @staticmethod
    def _event_value(event: Mapping[str, Any] | Any, name: str, default: Any = None) -> Any:
        if isinstance(event, Mapping):
            return event.get(name, default)
        return getattr(event, name, default)

    @staticmethod
    def _iso(value: datetime | str) -> str:
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                value = value.astimezone(timezone.utc)
            return value.isoformat()
        return str(value)

    def _draft_conflict_state(
        self, raybet_match_id: str, map_number: int,
    ) -> tuple[bool, str | None]:
        """Return whether a map has a draft conflict and its earliest cutoff.

        Conflict rows can arrive out of capture order.  Causal readers derive
        the cutoff from rows that conflict with the rebuilt canonical anchor
        and fail closed on missing schema or malformed timestamps.  If an
        operator froze a map without an intrinsic draft mismatch, every audit
        row remains effective.
        """
        try:
            anchor = self.connection.execute(
                """SELECT draft_hash, radiant_team_side, status, conflict_at
                     FROM vision_draft_anchors
                    WHERE raybet_match_id=? AND map_number=?""",
                (raybet_match_id, map_number),
            ).fetchone()
            rows = self.connection.execute(
                """SELECT captured_at, observed_draft_hash,
                          observed_radiant_team_side
                     FROM vision_draft_conflicts
                    WHERE raybet_match_id=? AND map_number=?
                    ORDER BY conflict_id""",
                (raybet_match_id, map_number),
            ).fetchall()
        except sqlite3.OperationalError:
            return True, None
        if anchor is None:
            return (True, None) if rows else (False, None)
        status = str(anchor["status"])
        if status not in {"anchored", "conflict"}:
            return True, None
        parsed_rows: list[tuple[datetime, str, bool]] = []
        for row in rows:
            value = str(row["captured_at"])
            try:
                timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return True, None
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                return True, None
            intrinsic = str(row["observed_draft_hash"]) != str(
                anchor["draft_hash"]
            ) or (
                anchor["radiant_team_side"] in {"team_one", "team_two"}
                and row["observed_radiant_team_side"]
                in {"team_one", "team_two"}
                and row["observed_radiant_team_side"]
                != anchor["radiant_team_side"]
            )
            normalized = timestamp.astimezone(timezone.utc)
            parsed_rows.append((normalized, normalized.isoformat(), intrinsic))
        if status == "anchored" and not parsed_rows:
            return False, None

        parsed: list[tuple[datetime, str]] = [
            (timestamp, value)
            for timestamp, value, intrinsic in parsed_rows
            if intrinsic
        ]
        if not parsed:
            parsed = [
                (timestamp, value) for timestamp, value, _intrinsic in parsed_rows
            ]
        if status == "conflict" and anchor["conflict_at"] is not None:
            value = str(anchor["conflict_at"])
            try:
                timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return True, None
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                return True, None
            normalized = timestamp.astimezone(timezone.utc)
            parsed.append((normalized, normalized.isoformat()))
        if not parsed:
            return True, None
        return True, min(parsed)[1]

    @staticmethod
    def _draft_event_key(captured_at: object, source_frame_ref: object) -> tuple[datetime, str] | None:
        """Return the deterministic event-time ordering key for one frame."""
        try:
            parsed = datetime.fromisoformat(str(captured_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc), str(source_frame_ref)

    def _restore_vision_anchor_identity_trigger(self) -> None:
        """Reinstall the immutable-anchor guard after an internal rebase."""
        self.connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS vision_draft_anchor_identity_immutable
            BEFORE UPDATE ON vision_draft_anchors
            WHEN NOT (
                (
                    OLD.status='anchored'
                    AND NEW.status='conflict'
                    AND OLD.raybet_match_id IS NEW.raybet_match_id
                    AND OLD.map_number IS NEW.map_number
                    AND OLD.draft_hash IS NEW.draft_hash
                    AND OLD.radiant_hero_ids IS NEW.radiant_hero_ids
                    AND OLD.dire_hero_ids IS NEW.dire_hero_ids
                    AND OLD.radiant_team_side IS NEW.radiant_team_side
                    AND OLD.team_side_anchored_at IS NEW.team_side_anchored_at
                    AND OLD.team_side_source_frame_ref
                        IS NEW.team_side_source_frame_ref
                    AND OLD.anchored_at IS NEW.anchored_at
                    AND OLD.source_frame_ref IS NEW.source_frame_ref
                    AND OLD.conflict_at IS NULL
                    AND NEW.conflict_at IS NOT NULL
                )
                OR
                (
                    OLD.status='anchored'
                    AND NEW.status='anchored'
                    AND OLD.raybet_match_id IS NEW.raybet_match_id
                    AND OLD.map_number IS NEW.map_number
                    AND OLD.draft_hash IS NEW.draft_hash
                    AND OLD.radiant_hero_ids IS NEW.radiant_hero_ids
                    AND OLD.dire_hero_ids IS NEW.dire_hero_ids
                    AND OLD.radiant_team_side IS NULL
                    AND (
                        NEW.radiant_team_side IS 'team_one'
                        OR NEW.radiant_team_side IS 'team_two'
                    )
                    AND OLD.team_side_anchored_at IS NULL
                    AND NEW.team_side_anchored_at IS NOT NULL
                    AND OLD.team_side_source_frame_ref IS NULL
                    AND NEW.team_side_source_frame_ref IS NOT NULL
                    AND NEW.team_side_source_frame_ref!=''
                    AND OLD.anchored_at IS NEW.anchored_at
                    AND OLD.source_frame_ref IS NEW.source_frame_ref
                    AND OLD.conflict_at IS NEW.conflict_at
                )
            )
            BEGIN
                SELECT RAISE(ABORT, 'vision draft anchor is immutable');
            END;
            """
        )

    def _rebuild_vision_draft_anchor(
        self, observation: Any, anchor: sqlite3.Row,
    ) -> bool:
        """Rebuild a draft anchor in deterministic browser event-time order.

        Browser capture time is the event order.  Rebuilding for every confirmed
        frame is necessary because a frame can arrive before an already-recorded
        conflict or team-side observation while still being later than the draft
        anchor.  All candidate facts remain append-only in the observation and
        conflict tables.
        """
        match_id = str(observation.raybet_match_id)
        map_number = int(observation.map_number)
        candidates: dict[tuple[str, str], dict[str, Any]] = {}

        def add_candidate(
            captured_at: object,
            source_frame_ref: object,
            draft_hash: object,
            radiant_hero_ids: object,
            dire_hero_ids: object,
            radiant_team_side: object,
        ) -> None:
            key = (str(captured_at), str(source_frame_ref))
            event_key = self._draft_event_key(*key)
            if event_key is None or not str(source_frame_ref).strip():
                return
            try:
                radiant = json.loads(str(radiant_hero_ids))
                dire = json.loads(str(dire_hero_ids))
            except (TypeError, ValueError, json.JSONDecodeError):
                return
            if not _valid_confirmed_vision_payload(
                radiant, dire, source_frame_ref
            ):
                return
            payload = self.json({"radiant": radiant, "dire": dire})
            calculated_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            if str(draft_hash) != calculated_hash:
                return
            side = radiant_team_side if radiant_team_side in {"team_one", "team_two"} else None
            candidates[key] = {
                "captured_at": event_key[0],
                "source_frame_ref": str(source_frame_ref),
                "draft_hash": calculated_hash,
                "radiant_json": self.json(radiant),
                "dire_json": self.json(dire),
                "radiant_team_side": side,
            }

        add_candidate(
            anchor["anchored_at"],
            anchor["source_frame_ref"],
            anchor["draft_hash"],
            anchor["radiant_hero_ids"],
            anchor["dire_hero_ids"],
            anchor["radiant_team_side"],
        )
        conflict_rows = self.connection.execute(
            """SELECT captured_at, source_frame_ref, observed_draft_hash,
                      radiant_hero_ids, dire_hero_ids,
                      observed_radiant_team_side
                 FROM vision_draft_conflicts
                WHERE raybet_match_id=? AND map_number=?""",
            (match_id, map_number),
        ).fetchall()
        conflict_keys = {
            (str(row["captured_at"]), str(row["source_frame_ref"]))
            for row in conflict_rows
        }
        for row in conflict_rows:
            add_candidate(
                row["captured_at"],
                row["source_frame_ref"],
                row["observed_draft_hash"],
                row["radiant_hero_ids"],
                row["dire_hero_ids"],
                row["observed_radiant_team_side"],
            )
        for row in self.connection.execute(
            """SELECT captured_at, source_frame_ref, radiant_hero_ids,
                      dire_hero_ids, radiant_team_side
                 FROM vision_observations
                WHERE raybet_match_id=? AND map_number=? AND confirmed=1""",
            (match_id, map_number),
        ).fetchall():
            key = (str(row["captured_at"]), str(row["source_frame_ref"]))
            if key not in conflict_keys:
                try:
                    payload = self.json({
                        "radiant": json.loads(str(row["radiant_hero_ids"])),
                        "dire": json.loads(str(row["dire_hero_ids"])),
                    })
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                add_candidate(
                    row["captured_at"],
                    row["source_frame_ref"],
                    hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                    row["radiant_hero_ids"],
                    row["dire_hero_ids"],
                    row["radiant_team_side"],
                )
        add_candidate(
            observation.captured_at.isoformat(),
            observation.source_frame_ref,
            hashlib.sha256(
                self.json({
                    "radiant": list(observation.radiant_hero_ids),
                    "dire": list(observation.dire_hero_ids),
                }).encode("utf-8")
            ).hexdigest(),
            self.json(list(observation.radiant_hero_ids)),
            self.json(list(observation.dire_hero_ids)),
            observation.radiant_team_side,
        )

        invalidated = {
            (str(row["captured_at"]), str(row["source_frame_ref"]))
            for row in self.connection.execute(
                """SELECT invalidation.captured_at,
                          invalidation.source_frame_ref
                     FROM vision_observation_invalidations AS invalidation
                     JOIN vision_observations AS observation
                       ON observation.raybet_match_id=
                          invalidation.raybet_match_id
                      AND observation.captured_at=invalidation.captured_at
                      AND observation.source_frame_ref=
                          invalidation.source_frame_ref
                    WHERE invalidation.raybet_match_id=?
                      AND observation.map_number=?""",
                (match_id, map_number),
            ).fetchall()
        }
        candidates = {
            key: value for key, value in candidates.items() if key not in invalidated
        }
        ordered = sorted(
            candidates.values(),
            key=lambda value: (value["captured_at"], value["source_frame_ref"]),
        )
        if not ordered:
            return False
        canonical = ordered[0]
        canonical_hash = canonical["draft_hash"]
        canonical_side: str | None = None
        side_source: dict[str, Any] | None = None
        conflict_candidates: list[dict[str, Any]] = []
        for candidate in ordered:
            if candidate["draft_hash"] != canonical_hash:
                conflict_candidates.append(candidate)
                continue
            side = candidate["radiant_team_side"]
            if side is None:
                continue
            if canonical_side is None:
                canonical_side = side
                side_source = candidate
            elif side != canonical_side:
                conflict_candidates.append(candidate)

        existing_conflict = bool(anchor["status"] == "conflict")
        conflict_cutoff = min(
            (candidate["captured_at"] for candidate in conflict_candidates),
            default=None,
        )
        if existing_conflict and conflict_cutoff is None:
            old_cutoff = self._draft_event_key(
                anchor["conflict_at"], anchor["source_frame_ref"]
            )
            if old_cutoff is not None:
                conflict_cutoff = old_cutoff[0]
            else:
                conflict_cutoff = min(
                    (value["captured_at"] for value in candidates.values()),
                    default=None,
                )
        has_conflict = conflict_cutoff is not None
        for candidate in conflict_candidates:
            self.connection.execute(
                """INSERT OR IGNORE INTO vision_draft_conflicts
                   (raybet_match_id, map_number, captured_at,
                    source_frame_ref, observed_draft_hash,
                    radiant_hero_ids, dire_hero_ids,
                    observed_radiant_team_side, reason, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    match_id,
                    map_number,
                    candidate["captured_at"].isoformat(),
                    candidate["source_frame_ref"],
                    candidate["draft_hash"],
                    candidate["radiant_json"],
                    candidate["dire_json"],
                    candidate["radiant_team_side"],
                    VISION_DRAFT_CONFLICT_REASON,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

        if has_conflict:
            cutoff = conflict_cutoff
            for candidate in ordered:
                if candidate["captured_at"] < cutoff:
                    continue
                if candidate["draft_hash"] != canonical_hash or candidate in conflict_candidates:
                    continue
                if (
                    candidate["captured_at"], candidate["source_frame_ref"]
                ) == (
                    canonical["captured_at"], canonical["source_frame_ref"]
                ):
                    continue
                self.connection.execute(
                    """INSERT OR IGNORE INTO vision_draft_conflicts
                       (raybet_match_id, map_number, captured_at,
                        source_frame_ref, observed_draft_hash,
                        radiant_hero_ids, dire_hero_ids,
                        observed_radiant_team_side, reason, recorded_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        match_id,
                        map_number,
                        candidate["captured_at"].isoformat(),
                        candidate["source_frame_ref"],
                        candidate["draft_hash"],
                        candidate["radiant_json"],
                        candidate["dire_json"],
                        candidate["radiant_team_side"],
                        VISION_DRAFT_CONFLICT_REASON,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )

        side_time = side_source["captured_at"].isoformat() if side_source else None
        side_ref = side_source["source_frame_ref"] if side_source else None
        status = "conflict" if has_conflict else "anchored"
        conflict_at = conflict_cutoff.isoformat() if conflict_cutoff else None
        self.connection.execute("DROP TRIGGER IF EXISTS vision_draft_anchor_identity_immutable")
        try:
            self.connection.execute(
                """UPDATE vision_draft_anchors
                      SET draft_hash=?, radiant_hero_ids=?, dire_hero_ids=?,
                          radiant_team_side=?, team_side_anchored_at=?,
                          team_side_source_frame_ref=?, anchored_at=?,
                          source_frame_ref=?, status=?, conflict_at=?
                    WHERE raybet_match_id=? AND map_number=?""",
                (
                    canonical["draft_hash"],
                    canonical["radiant_json"],
                    canonical["dire_json"],
                    canonical_side,
                    side_time,
                    side_ref,
                    canonical["captured_at"].isoformat(),
                    canonical["source_frame_ref"],
                    status,
                    conflict_at,
                    match_id,
                    map_number,
                ),
            )
        finally:
            self._restore_vision_anchor_identity_trigger()

        for row in self.connection.execute(
            """SELECT captured_at, source_frame_ref, radiant_hero_ids,
                      dire_hero_ids, radiant_team_side
                 FROM vision_observations
                WHERE raybet_match_id=? AND map_number=?""",
            (match_id, map_number),
        ).fetchall():
            key = (str(row["captured_at"]), str(row["source_frame_ref"]))
            candidate = candidates.get(key)
            if candidate is None or key in invalidated:
                continue
            trusted = candidate["draft_hash"] == canonical_hash
            if (
                trusted
                and canonical_side is not None
                and candidate["radiant_team_side"] is not None
                and candidate["radiant_team_side"] != canonical_side
            ):
                trusted = False
            if conflict_cutoff is not None and candidate["captured_at"] >= conflict_cutoff:
                trusted = False
            self.connection.execute(
                """UPDATE vision_observations SET confirmed=?
                    WHERE raybet_match_id=? AND captured_at=?
                      AND source_frame_ref=?""",
                (int(trusted), match_id, row["captured_at"], row["source_frame_ref"]),
            )

        if has_conflict:
            self._invalidate_draft_dependents(
                match_id,
                map_number,
                VISION_DRAFT_CONFLICT_REASON,
                conflict_at,
            )
        current_key = (
            observation.captured_at.isoformat(), str(observation.source_frame_ref)
        )
        current = candidates.get(current_key)
        if current is None:
            return False
        trusted = current["draft_hash"] == canonical_hash
        if (
            trusted
            and canonical_side is not None
            and current["radiant_team_side"] is not None
            and current["radiant_team_side"] != canonical_side
        ):
            trusted = False
        if conflict_cutoff is not None and current["captured_at"] >= conflict_cutoff:
            trusted = False
        return trusted

    def _draft_conflict_at_or_before(
        self,
        raybet_match_id: str,
        map_number: int,
        at: datetime | str | None,
    ) -> bool:
        if self._draft_conflict_effective_at(raybet_match_id, map_number, at):
            return True
        return self._vision_observation_invalidated_at_or_before(
            raybet_match_id, map_number, at
        )

    def _draft_conflict_effective_at(
        self,
        raybet_match_id: str,
        map_number: int,
        at: datetime | str | None,
    ) -> bool:
        """Apply a draft conflict only at or after its event-time cutoff."""
        conflicted, cutoff = self._draft_conflict_state(raybet_match_id, map_number)
        if conflicted:
            if cutoff is None or at is None:
                return True
            try:
                target = datetime.fromisoformat(
                    self._iso(at).replace("Z", "+00:00")
                )
            except (TypeError, ValueError):
                return True
            if target.tzinfo is None or target.utcoffset() is None:
                return True
            if datetime.fromisoformat(cutoff) <= target.astimezone(timezone.utc):
                return True
        return False

    def _vision_observation_invalidated_at_or_before(
        self,
        raybet_match_id: str,
        map_number: int,
        at: datetime | str | None,
    ) -> bool:
        """Fail closed when no trusted frame survives an observation audit.

        An invalidated frame does not permanently poison a map: a later
        confirmed frame can restore the causal stream.  Until such a frame is
        present at the requested event-time cutoff, new derived writes remain
        blocked.
        """
        try:
            rows = self.connection.execute(
                """SELECT invalidation.captured_at
                     FROM vision_observation_invalidations AS invalidation
                     JOIN vision_observations AS observation
                       ON observation.raybet_match_id=invalidation.raybet_match_id
                      AND observation.captured_at=invalidation.captured_at
                      AND observation.source_frame_ref=invalidation.source_frame_ref
                    WHERE invalidation.raybet_match_id=?
                      AND observation.map_number=?""",
                (raybet_match_id, map_number),
            ).fetchall()
        except sqlite3.OperationalError:
            # A missing or unreadable audit table cannot prove that the
            # lineage is clean.  All causal writers must fail closed until
            # schema repair/migration has completed.
            return True
        if not rows:
            return False
        if at is None:
            return True
        try:
            target = datetime.fromisoformat(
                self._iso(at).replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            return True
        if target.tzinfo is None or target.utcoffset() is None:
            return True
        target = target.astimezone(timezone.utc)
        invalidated: list[datetime] = []
        for row in rows:
            try:
                captured = datetime.fromisoformat(
                    str(row["captured_at"]).replace("Z", "+00:00")
                )
            except (TypeError, ValueError):
                return True
            if captured.tzinfo is None or captured.utcoffset() is None:
                return True
            captured = captured.astimezone(timezone.utc)
            if captured <= target:
                invalidated.append(captured)
        if not invalidated:
            return False
        latest_invalidated = max(invalidated)
        try:
            valid_rows = self.connection.execute(
                """SELECT observation.captured_at
                     FROM vision_observations AS observation
                    WHERE observation.raybet_match_id=?
                      AND observation.map_number=?
                      AND observation.confirmed=1
                      AND julianday(observation.captured_at)<=julianday(?)
                      AND NOT EXISTS (
                           SELECT 1
                             FROM vision_observation_invalidations AS invalidation
                            WHERE invalidation.raybet_match_id=observation.raybet_match_id
                              AND invalidation.captured_at=observation.captured_at
                              AND invalidation.source_frame_ref=observation.source_frame_ref
                      )
                    ORDER BY observation.captured_at DESC""",
                (raybet_match_id, map_number, target.isoformat()),
            ).fetchall()
        except sqlite3.OperationalError:
            return True
        for row in valid_rows:
            try:
                captured = datetime.fromisoformat(
                    str(row["captured_at"]).replace("Z", "+00:00")
                )
            except (TypeError, ValueError):
                return True
            if captured.tzinfo is None or captured.utcoffset() is None:
                return True
            if captured.astimezone(timezone.utc) > latest_invalidated:
                return False
        return True

    def _vision_derived_block_reason(self, order_key: str) -> str | None:
        try:
            row = self.connection.execute(
                """SELECT block_reason FROM vision_derived_invalidations
                    WHERE dependent_type='shadow_order' AND dependent_key=?
                    LIMIT 1""",
                (order_key,),
            ).fetchone()
        except sqlite3.OperationalError:
            # Legacy databases may not have the stable gate-code column yet.
            # The row itself is still a durable invalidation, so preserve the
            # conservative draft-conflict fence until migration runs.
            try:
                row = self.connection.execute(
                    """SELECT 1 FROM vision_derived_invalidations
                        WHERE dependent_type='shadow_order'
                          AND dependent_key=?
                        LIMIT 1""",
                    (order_key,),
                ).fetchone()
            except sqlite3.OperationalError:
                return "vision_draft_conflict"
            return "vision_draft_conflict" if row is not None else None
        if row is None:
            return None
        return str(row["block_reason"] or "vision_draft_conflict")

    def vision_block_reason_for_order(self, order_key: str) -> str | None:
        """Return the stable vision gate code that blocks one order lineage."""
        derived = self._vision_derived_block_reason(order_key)
        if derived is not None:
            return derived
        try:
            row = self.connection.execute(
                """SELECT orders.raybet_match_id, attempt.map_number,
                          orders.signal_transport_at
                     FROM shadow_orders AS orders
                     JOIN shadow_map_attempts AS attempt
                       ON attempt.order_key=orders.order_key
                    WHERE orders.order_key=?""",
                (order_key,),
            ).fetchone()
        except sqlite3.OperationalError:
            return "vision_draft_conflict"
        if row is None:
            return None
        if self._vision_observation_invalidated_at_or_before(
            str(row["raybet_match_id"]),
            int(row["map_number"]),
            row["signal_transport_at"],
        ):
            return "vision_observation_invalidated"
        if self._draft_conflict_at_or_before(
            str(row["raybet_match_id"]),
            int(row["map_number"]),
            row["signal_transport_at"],
        ):
            return "vision_draft_conflict"
        return None

    def _strict_mapping_context_block_reason(
        self,
        *,
        strict_mapping_id: int,
        raybet_match_id: str,
        map_number: int,
        signal_transport_at: datetime | str,
    ) -> str | None:
        return strict_mapping_context_block_reason(
            self.connection,
            strict_mapping_id=strict_mapping_id,
            raybet_match_id=raybet_match_id,
            map_number=map_number,
            signal_transport_at=signal_transport_at,
        )

    def _strict_mapping_block_reason_for_order(self, order_key: str) -> str | None:
        return strict_order_mapping_block_reason(self.connection, order_key)

    def order_block_reason(self, order_key: str) -> str | None:
        """Return the first stable safety gate that blocks an order lineage."""
        strict = self._strict_mapping_block_reason_for_order(order_key)
        if strict is not None:
            return strict
        return self.vision_block_reason_for_order(order_key)

    def _order_draft_conflict_effective_at(
        self, order_key: str, at: datetime | str | None
    ) -> bool:
        row = self.connection.execute(
            """SELECT attempt.raybet_match_id, attempt.map_number
                 FROM shadow_map_attempts AS attempt
                WHERE attempt.order_key=?""",
            (order_key,),
        ).fetchone()
        if row is None:
            return False
        return self._draft_conflict_effective_at(
            str(row["raybet_match_id"]), int(row["map_number"]), at
        )

    @staticmethod
    def _scalar(value: Any) -> Any:
        return value.value if isinstance(value, Enum) else value

    def upsert_provider_match(self, match: ProviderMatch, updated_at: datetime) -> None:
        self.execute(
            """INSERT INTO provider_matches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, provider_match_id) DO UPDATE SET
              tournament=excluded.tournament, team_one=excluded.team_one,
              team_two=excluded.team_two, scheduled_at=excluded.scheduled_at,
              best_of=excluded.best_of, status=excluded.status,
              raw_json=excluded.raw_json, updated_at=excluded.updated_at""",
            (match.provider, match.provider_match_id, match.tournament, match.team_one,
             match.team_two, match.scheduled_at.isoformat() if match.scheduled_at else None,
             match.best_of, match.status, self.json(sanitize_raybet_payload(match.raw)),
             updated_at.isoformat()),
        )

    def upsert_raybet_match(self, row: dict[str, Any], updated_at: datetime) -> None:
        safe_row = sanitize_raybet_payload(row)
        if not isinstance(safe_row, dict):
            raise ValueError("RayBet match payload must be an object")
        teams = sorted(
            safe_row.get("team") or [],
            key=lambda item: int(item.get("pos") or 0),
        )
        team_one = str(teams[0].get("team_name") or "") if teams else ""
        team_two = str(teams[1].get("team_name") or "") if len(teams) > 1 else ""
        round_name = str(safe_row.get("round") or "").lower()
        best_of = int(round_name[2:]) if round_name.startswith("bo") and round_name[2:].isdigit() else None
        self.execute(
            """INSERT INTO raybet_matches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(raybet_match_id) DO UPDATE SET
              tournament=excluded.tournament, team_one=excluded.team_one,
              team_two=excluded.team_two, scheduled_at=excluded.scheduled_at,
              best_of=excluded.best_of, status=excluded.status,
              live_url=excluded.live_url, raw_json=excluded.raw_json,
              updated_at=excluded.updated_at
            WHERE julianday(excluded.updated_at) IS NOT NULL
              AND (
                    julianday(raybet_matches.updated_at) IS NULL
                    OR julianday(excluded.updated_at) >=
                       julianday(raybet_matches.updated_at)
              )""",
            (
                str(safe_row.get("id")),
                str(safe_row.get("tournament_name") or ""),
                team_one,
                team_two,
                safe_row.get("start_time"),
                best_of,
                str(safe_row.get("status") or ""),
                safe_row.get("live_url"),
                self.json(safe_row),
                updated_at.isoformat(),
            ),
        )

    def insert_browser_raybet_match(
        self, row: dict[str, Any], updated_at: datetime
    ) -> bool:
        """Insert sanitized browser metadata without replacing direct-owned data."""
        safe_row = sanitize_raybet_payload(row)
        if not isinstance(safe_row, dict):
            raise ValueError("RayBet browser metadata must be an object")
        teams = sorted(
            safe_row.get("team") or [],
            key=lambda item: int(item.get("pos") or 0),
        )
        team_one = str(teams[0].get("team_name") or "") if teams else ""
        team_two = str(teams[1].get("team_name") or "") if len(teams) > 1 else ""
        round_name = str(safe_row.get("round") or "").lower()
        best_of = (
            int(round_name[2:])
            if round_name.startswith("bo") and round_name[2:].isdigit()
            else None
        )
        cursor = self.execute(
            """INSERT OR IGNORE INTO raybet_matches
            (raybet_match_id, tournament, team_one, team_two, scheduled_at, best_of,
             status, live_url, raw_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
            (
                str(safe_row.get("id")),
                str(safe_row.get("tournament_name") or ""),
                team_one,
                team_two,
                safe_row.get("start_time"),
                best_of,
                str(safe_row.get("status") or ""),
                self.json({}),
                updated_at.isoformat(),
            ),
        )
        return cursor.rowcount == 1

    def insert_browser_event(
        self,
        event: Mapping[str, Any] | Any,
        *,
        received_at: datetime,
        recognized: bool,
        processing_status: str = "pending",
        processing_reason: str | None = None,
    ) -> bool:
        captured_at = self._event_value(
            event, "captured_at_utc", self._event_value(event, "captured_at")
        )
        payload = self._event_value(event, "payload", {})
        cursor = self.execute(
            """INSERT OR IGNORE INTO browser_events
            (event_id, schema_version, capture_session_id, captured_at, received_at,
             transport, event_type, raybet_match_id, game_id, page_origin, page_path,
             source_path, payload_hash, payload_bytes, payload_json, capture_reason,
             extension_version, recognized, processing_status, processing_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(self._event_value(event, "event_id")),
                int(self._event_value(event, "schema_version")),
                str(self._event_value(event, "capture_session_id")),
                self._iso(captured_at),
                self._iso(received_at),
                str(self._scalar(self._event_value(event, "transport"))),
                str(self._scalar(self._event_value(event, "event_type"))),
                self._event_value(event, "raybet_match_id"),
                self._event_value(event, "game_id"),
                str(self._event_value(event, "page_origin")),
                str(self._event_value(event, "page_path")),
                str(self._event_value(event, "source_path")),
                str(self._event_value(event, "payload_hash")),
                int(self._event_value(event, "payload_bytes")),
                self.json(payload),
                self._event_value(event, "capture_reason"),
                str(self._event_value(event, "extension_version")),
                int(recognized),
                processing_status,
                processing_reason,
            ),
        )
        return cursor.rowcount == 1

    def browser_event_identity_matches(self, event: Mapping[str, Any] | Any) -> bool:
        """Check immutable retry identity before treating an event ID as duplicate."""
        event_id = str(self._event_value(event, "event_id"))
        row = self.connection.execute(
            """SELECT schema_version, capture_session_id, captured_at, transport,
                      event_type, raybet_match_id, game_id, page_origin, page_path,
                      source_path, payload_hash, payload_bytes, capture_reason,
                      extension_version
                 FROM browser_events WHERE event_id=?""",
            (event_id,),
        ).fetchone()
        if row is None:
            return False
        captured_at = self._event_value(
            event, "captured_at_utc", self._event_value(event, "captured_at")
        )
        expected = (
            int(self._event_value(event, "schema_version")),
            str(self._event_value(event, "capture_session_id")),
            self._iso(captured_at),
            str(self._scalar(self._event_value(event, "transport"))),
            str(self._scalar(self._event_value(event, "event_type"))),
            self._event_value(event, "raybet_match_id"),
            self._event_value(event, "game_id"),
            str(self._event_value(event, "page_origin")),
            str(self._event_value(event, "page_path")),
            str(self._event_value(event, "source_path")),
            str(self._event_value(event, "payload_hash")),
            int(self._event_value(event, "payload_bytes")),
            self._event_value(event, "capture_reason"),
            str(self._event_value(event, "extension_version")),
        )
        return tuple(row) == expected

    def update_browser_event_status(
        self, event_id: str, status: str, reason: str | None = None
    ) -> bool:
        cursor = self.execute(
            """UPDATE browser_events
               SET processing_status=?, processing_reason=? WHERE event_id=?""",
            (status, reason, event_id),
        )
        return cursor.rowcount == 1

    def observation_timing_status(
        self, raybet_match_id: str, observed_at: datetime
    ) -> str:
        newest = self.connection.execute(
            """SELECT observed_at FROM odds_transport_observations
               WHERE raybet_match_id=? AND timing_status!='late'
               ORDER BY observed_at DESC, observation_key DESC LIMIT 1""",
            (raybet_match_id,),
        ).fetchone()
        if newest and self._iso(observed_at) < str(newest["observed_at"]):
            return "late"
        return "on_time"

    def insert_transport_observation(
        self,
        *,
        observation_key: str,
        source: str,
        source_event_id: str | None,
        raybet_match_id: str,
        observed_at: datetime,
        normalized_state_hash: str,
        timing_status: str,
        processing_status: str,
        normalized_change_count: int,
    ) -> bool:
        cursor = self.execute(
            """INSERT OR IGNORE INTO odds_transport_observations
            (observation_key, source, source_event_id, raybet_match_id, observed_at,
             normalized_state_hash, timing_status, processing_status,
             normalized_change_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (observation_key, source, source_event_id, raybet_match_id,
             self._iso(observed_at), normalized_state_hash, timing_status,
             processing_status, normalized_change_count),
        )
        return cursor.rowcount == 1

    def store_odds_observation(
        self,
        *,
        source: str,
        observation_key: str,
        source_event_id: str | None,
        raybet_match_id: str,
        observed_at: datetime,
        normalized_state_hash: str,
        snapshots: Sequence[OddsSnapshot],
    ) -> tuple[str, int]:
        """Atomically retain one complete response and its semantic state changes."""
        seen_odds_ids: set[str] = set()
        for snapshot in snapshots:
            if snapshot.raybet_match_id != raybet_match_id:
                raise ValueError("response outcome match id mismatch")
            if snapshot.received_at != observed_at:
                raise ValueError("response outcome transport time mismatch")
            if snapshot.odds_id in seen_odds_ids:
                raise ValueError("duplicate odds id in one response")
            seen_odds_ids.add(snapshot.odds_id)

        with self.transaction():
            existing = self.connection.execute(
                """SELECT source, source_event_id, raybet_match_id, observed_at,
                          normalized_state_hash, timing_status,
                          normalized_change_count
                   FROM odds_transport_observations WHERE observation_key=?""",
                (observation_key,),
            ).fetchone()
            if existing:
                identity = (
                    str(existing["source"]),
                    existing["source_event_id"],
                    str(existing["raybet_match_id"]),
                    str(existing["observed_at"]),
                    str(existing["normalized_state_hash"]),
                )
                expected = (
                    source,
                    source_event_id,
                    raybet_match_id,
                    self._iso(observed_at),
                    normalized_state_hash,
                )
                if identity != expected:
                    raise ValueError("observation key already belongs to another response")
                persisted_outcomes = self.connection.execute(
                    """SELECT raybet_match_id, odds_id, odds_group_id, received_at,
                              price, status, market_type, period, side, line,
                              outcome_key, supported, last_update, raw_json
                         FROM odds_response_outcomes
                        WHERE observation_key=? ORDER BY odds_id""",
                    (observation_key,),
                ).fetchall()
                if not persisted_outcomes:
                    if snapshots:
                        raise ValueError(
                            "observation key response membership or payload differs"
                        )
                actual_outcomes = [tuple(row) for row in persisted_outcomes]
                expected_outcomes = sorted(
                    (self._response_outcome_values(snapshot) for snapshot in snapshots),
                    key=lambda values: str(values[1]),
                )
                if actual_outcomes != expected_outcomes:
                    raise ValueError(
                        "observation key response membership or payload differs"
                    )
                return str(existing["timing_status"]), 0

            timing_status = self.observation_timing_status(raybet_match_id, observed_at)
            processing_status = "audit_only" if timing_status == "late" else "processing"
            inserted = self.insert_transport_observation(
                observation_key=observation_key,
                source=source,
                source_event_id=source_event_id,
                raybet_match_id=raybet_match_id,
                observed_at=observed_at,
                normalized_state_hash=normalized_state_hash,
                timing_status=timing_status,
                processing_status=processing_status,
                normalized_change_count=0,
            )
            if not inserted:
                return timing_status, 0

            self._insert_response_outcomes(observation_key, snapshots)
            change_count = 0
            if timing_status != "late":
                change_count = sum(int(self.insert_odds(snapshot)) for snapshot in snapshots)
                processing_status = "processed"
            self.execute(
                """UPDATE odds_transport_observations
                   SET processing_status=?, normalized_change_count=?
                   WHERE observation_key=?""",
                (processing_status, change_count, observation_key),
            )
            return timing_status, change_count

    def _insert_response_outcomes(
        self, observation_key: str, snapshots: Sequence[OddsSnapshot]
    ) -> None:
        """Persist exact response membership, independently of semantic changes."""
        for snapshot in snapshots:
            self.execute(
                """INSERT OR IGNORE INTO odds_response_outcomes
                (observation_key, raybet_match_id, odds_id, odds_group_id,
                 received_at, price, status, market_type, period, side, line,
                 outcome_key, supported, last_update, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (observation_key, *self._response_outcome_values(snapshot)),
            )

    def _response_outcome_values(self, snapshot: OddsSnapshot) -> tuple[Any, ...]:
        market = snapshot.market
        return (
            snapshot.raybet_match_id,
            snapshot.odds_id,
            snapshot.odds_group_id,
            self._iso(snapshot.received_at),
            snapshot.price,
            None if snapshot.status is None else str(snapshot.status),
            market.market_type,
            market.period,
            market.side,
            market.line,
            market.outcome_key,
            int(market.supported),
            snapshot.last_update,
            json.dumps(
                sanitize_raybet_payload(snapshot.raw),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                default=str,
            ),
        )

    def upsert_match_link(
        self, raybet_match_id: str, provider: str, provider_match_id: str,
        confidence: float, status: str, reason: str, created_at: datetime,
    ) -> None:
        self.execute(
            """INSERT INTO match_links VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(raybet_match_id, provider) DO UPDATE SET
              provider_match_id=CASE WHEN match_links.status='accepted'
                THEN match_links.provider_match_id ELSE excluded.provider_match_id END,
              confidence=excluded.confidence, status=CASE WHEN match_links.status='accepted'
                THEN match_links.status ELSE excluded.status END, reason=excluded.reason""",
            (raybet_match_id, provider, provider_match_id, confidence, status, reason,
             created_at.isoformat()),
        )

    def insert_odds(self, snapshot: OddsSnapshot) -> bool:
        market = snapshot.market
        previous = self.connection.execute(
            """SELECT price, status, last_update FROM odds_snapshots
            WHERE raybet_match_id=? AND odds_id=? AND received_at<=?
            ORDER BY received_at DESC, id DESC LIMIT 1""",
            (snapshot.raybet_match_id, snapshot.odds_id,
             self._iso(snapshot.received_at)),
        ).fetchone()
        current = (snapshot.price, str(snapshot.status), snapshot.last_update)
        if previous and tuple(previous) == current:
            return False
        cursor = self.execute(
            """INSERT OR IGNORE INTO odds_snapshots
            (raybet_match_id, odds_id, odds_group_id, received_at, price, status,
             market_type, period, side, line, outcome_key, supported, last_update, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (snapshot.raybet_match_id, snapshot.odds_id, snapshot.odds_group_id,
              self._iso(snapshot.received_at), snapshot.price, str(snapshot.status),
             market.market_type, market.period, market.side, market.line,
             market.outcome_key, int(market.supported), snapshot.last_update,
             self.json(sanitize_raybet_payload(snapshot.raw))),
        )
        return cursor.rowcount == 1

    def next_fill_candidate(self, order: ShadowOrder) -> sqlite3.Row | None:
        """Return the target outcome only from the first eligible response."""
        return self.connection.execute(
            """WITH successor AS (
                   SELECT observation_key, raybet_match_id, observed_at
                     FROM odds_transport_observations
                    WHERE raybet_match_id=? AND observed_at>?
                      AND timing_status='on_time'
                      AND processing_status='processed'
                    ORDER BY observed_at, observation_key LIMIT 1
               )
               SELECT outcome.*, successor.observed_at AS transport_observed_at,
                      successor.observation_key AS transport_observation_key
                 FROM successor
                 JOIN odds_response_outcomes outcome
                   ON outcome.observation_key=successor.observation_key
                  AND outcome.raybet_match_id=successor.raybet_match_id
                WHERE outcome.odds_id=?""",
            (
                order.raybet_match_id,
                self._iso(order.signal_transport_at),
                order.odds_id,
            ),
        ).fetchone()

    def processed_transport_watermark(
        self, raybet_match_id: str, *, as_of: datetime
    ) -> datetime | None:
        """Return persisted event-time progress, never the worker wall clock."""
        row = self.connection.execute(
            """SELECT observed_at FROM odds_transport_observations
                 WHERE raybet_match_id=? AND observed_at<=?
                   AND timing_status='on_time'
                   AND processing_status='processed'
                 ORDER BY observed_at DESC, observation_key DESC LIMIT 1""",
            (raybet_match_id, self._iso(as_of)),
        ).fetchone()
        return (
            datetime.fromisoformat(str(row["observed_at"]))
            if row is not None
            else None
        )

    def _signal_identity_matches(self, order: ShadowOrder) -> bool:
        if not order.signal_identity_verified:
            return False
        row = self.connection.execute(
            """SELECT transport.raybet_match_id, transport.observed_at,
                      transport.timing_status, transport.processing_status,
                      outcome.odds_group_id, outcome.outcome_key,
                      outcome.price, outcome.status, outcome.supported,
                      outcome.market_type, outcome.period, outcome.side,
                      outcome.line
                 FROM odds_transport_observations AS transport
                 JOIN odds_response_outcomes AS outcome
                   ON outcome.observation_key=transport.observation_key
                WHERE transport.observation_key=?
                  AND outcome.raybet_match_id=? AND outcome.odds_id=?""",
            (
                order.signal_transport_key,
                order.raybet_match_id,
                order.odds_id,
            ),
        ).fetchone()
        if row is None:
            return False
        return (
            str(row["raybet_match_id"]) == order.raybet_match_id
            and str(row["observed_at"]) == self._iso(order.signal_transport_at)
            and str(row["timing_status"]) == "on_time"
            and str(row["processing_status"]) == "processed"
            and str(row["odds_group_id"] or "") == order.signal_odds_group_id
            and str(row["outcome_key"] or "") == order.signal_outcome_key
            and float(row["price"]) == order.signal_price
            and is_open(row["status"])
            and bool(row["supported"])
            and market_key(
                str(row["market_type"]),
                str(row["period"]),
                row["side"],
                row["line"],
            )
            == market_key(
                order.market.market_type,
                order.market.period,
                order.market.side,
                order.market.line,
            )
        )

    def process_pending_successor(
        self,
        order: ShadowOrder,
        *,
        watermark: datetime,
        max_slippage: float = 0.03,
    ) -> ShadowOrder | None:
        """Resolve a pending order from its exact first visible successor.

        A returned order was transitioned atomically with its map attempt. None
        means that the order remains pending or another worker already resolved it.
        """
        with self.transaction():
            current = self.connection.execute(
                """SELECT raybet_match_id, odds_id, signal_transport_key,
                          signal_transport_at, expires_at,
                          signal_odds_group_id, signal_outcome_key,
                          signal_identity_verified, status
                     FROM shadow_orders WHERE order_key=?""",
                (order.order_key,),
            ).fetchone()
            if current is None:
                raise ValueError("shadow order is not persisted")
            if str(current["status"]) != "pending":
                return None
            persisted_identity = (
                str(current["raybet_match_id"]),
                str(current["odds_id"]),
                str(current["signal_transport_key"]),
                str(current["signal_transport_at"]),
                str(current["expires_at"]),
                current["signal_odds_group_id"],
                current["signal_outcome_key"],
                bool(current["signal_identity_verified"]),
            )
            requested_identity = (
                order.raybet_match_id,
                order.odds_id,
                order.signal_transport_key,
                self._iso(order.signal_transport_at),
                self._iso(order.expires_at),
                order.signal_odds_group_id,
                order.signal_outcome_key,
                order.signal_identity_verified,
            )
            if persisted_identity != requested_identity:
                raise ValueError("shadow order does not match persisted signal identity")

            map_row = self.connection.execute(
                """SELECT raybet_match_id, map_number
                     FROM shadow_map_attempts
                    WHERE order_key=? AND status='pending'""",
                (order.order_key,),
            ).fetchone()
            block_reason = self.order_block_reason(order.order_key) if map_row else None
            draft_conflict = block_reason is not None
            signal_is_valid = not draft_conflict and self._signal_identity_matches(order)
            successor = None
            if signal_is_valid:
                successor = self.connection.execute(
                    """SELECT observation_key, raybet_match_id, observed_at
                         FROM odds_transport_observations
                        WHERE raybet_match_id=? AND observed_at>?
                          AND observed_at<=?
                          AND timing_status='on_time'
                          AND processing_status='processed'
                        ORDER BY observed_at, observation_key LIMIT 1""",
                    (
                        order.raybet_match_id,
                        self._iso(order.signal_transport_at),
                        self._iso(watermark),
                    ),
                ).fetchone()

            resolved: ShadowOrder | None = None
            if draft_conflict:
                resolved = replace(
                    order,
                    status="rejected",
                    rejection_reason=block_reason or "vision_draft_conflict",
                )
            elif not signal_is_valid:
                resolved = replace(
                    order,
                    status="rejected",
                    rejection_reason="signal_identity_unverified",
                )
            elif successor is not None:
                observed_at = datetime.fromisoformat(str(successor["observed_at"]))
                if observed_at > order.expires_at:
                    resolved = replace(
                        order,
                        status="rejected",
                        rejection_reason="fill_timeout",
                    )
                else:
                    outcome = self.connection.execute(
                        """SELECT * FROM odds_response_outcomes
                            WHERE observation_key=? AND raybet_match_id=?
                              AND odds_id=?""",
                        (
                            str(successor["observation_key"]),
                            order.raybet_match_id,
                            order.odds_id,
                        ),
                    ).fetchone()
                    if outcome is None:
                        resolved = replace(
                            order,
                            status="rejected",
                            rejection_reason="outcome_missing",
                        )
                    else:
                        resolved = attempt_fill(
                            order,
                            self._response_snapshot(outcome),
                            observed_at=observed_at,
                            max_slippage=max_slippage,
                            now=observed_at,
                        )

            if resolved is None or resolved.status == "pending":
                return None
            order_update = self.connection.execute(
                """UPDATE shadow_orders
                      SET status=?, fill_price=?, filled_at=?, rejection_reason=?
                    WHERE order_key=? AND status='pending'""",
                (
                    resolved.status,
                    resolved.fill_price,
                    self._iso(resolved.filled_at) if resolved.filled_at else None,
                    resolved.rejection_reason,
                    resolved.order_key,
                ),
            )
            if order_update.rowcount != 1:
                return None
            if not self.update_map_attempt(
                resolved.order_key, resolved.status, expected_status="pending"
            ):
                raise RuntimeError("pending order has no matching pending map attempt")
            if resolved.status == "filled":
                map_row = self.connection.execute(
                    """SELECT map_number FROM shadow_map_attempts
                        WHERE order_key=?""",
                    (resolved.order_key,),
                ).fetchone()
                if map_row is None or resolved.filled_at is None:
                    raise RuntimeError("filled order is missing map provenance")
                from .notifications import EVENT_FILLED, filled_order_payload

                self.enqueue_notification(
                    order_key=resolved.order_key,
                    event_type=EVENT_FILLED,
                    payload=filled_order_payload(
                        self.connection, resolved.order_key
                    ),
                    stats_cutoff_at=resolved.filled_at,
                    created_at=resolved.filled_at,
                )
            return resolved

    @staticmethod
    def _response_snapshot(row: sqlite3.Row) -> OddsSnapshot:
        market = Market(
            str(row["market_type"]),
            str(row["period"]),
            row["side"],
            row["line"],
            str(row["outcome_key"]),
            bool(row["supported"]),
        )
        return OddsSnapshot(
            str(row["raybet_match_id"]),
            str(row["odds_id"]),
            row["odds_group_id"],
            datetime.fromisoformat(str(row["received_at"])),
            float(row["price"]),
            row["status"],
            market,
            row["last_update"],
            json.loads(str(row["raw_json"])),
        )

    def insert_frame(self, frame: LiveFrame) -> None:
        self.execute(
            """INSERT OR IGNORE INTO live_frames VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (frame.provider, frame.provider_match_id, frame.provider_game_id,
             frame.sequence or "", frame.source_at.isoformat() if frame.source_at else None,
             frame.received_at.isoformat(), frame.game_time, frame.team_one_kills,
             frame.team_two_kills, frame.team_one_gold, frame.team_two_gold, frame.state,
             self.json(frame.raw)),
        )

    def insert_event(self, event: LiveEvent) -> None:
        self.execute(
            """INSERT OR IGNORE INTO live_events VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event.provider, event.provider_event_id, event.provider_match_id,
             event.provider_game_id, event.event_type,
             event.source_at.isoformat() if event.source_at else None,
             event.received_at.isoformat(), event.game_time, event.team, event.player,
             event.value, self.json(event.raw)),
        )

    def insert_order(self, order: ShadowOrder) -> bool:
        """Reject the legacy writer that cannot bind an order to a map mapping."""
        del order
        return False

    def update_order(self, order: ShadowOrder) -> None:
        """Reject the legacy updater that bypasses successor verification."""
        del order
        raise RuntimeError(
            "legacy order updater is disabled; use process_pending_successor"
        )

    def record_collector(
        self, collector: str, *, success_at: datetime | None = None,
        error_at: datetime | None = None, error: str | None = None,
        cursor: str | None = None, gap: bool = False,
    ) -> None:
        self.execute(
            """INSERT INTO collector_runs
            (collector, last_success_at, last_error_at, last_error, cursor, gap_detected)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(collector) DO UPDATE SET
              last_success_at=COALESCE(excluded.last_success_at, collector_runs.last_success_at),
              last_error_at=COALESCE(excluded.last_error_at, collector_runs.last_error_at),
              last_error=excluded.last_error, cursor=COALESCE(excluded.cursor, collector_runs.cursor),
              gap_detected=excluded.gap_detected""",
            (collector, success_at.isoformat() if success_at else None,
             error_at.isoformat() if error_at else None, error, cursor, int(gap)),
        )

    def insert_vision_observation(self, observation: Any) -> bool:
        captured_at = observation.captured_at.isoformat()
        radiant_json = self.json(list(observation.radiant_hero_ids))
        dire_json = self.json(list(observation.dire_hero_ids))
        radiant_team_side = observation.radiant_team_side
        if radiant_team_side not in {None, "team_one", "team_two"}:
            raise ValueError(
                "radiant_team_side must be team_one, team_two, or null"
            )
        stored_confirmed = _valid_confirmed_vision_payload(
            observation.radiant_hero_ids,
            observation.dire_hero_ids,
            observation.source_frame_ref,
        ) and bool(observation.is_confirmed)
        with self.transaction():
            if stored_confirmed and observation.map_number is not None:
                draft_payload = self.json(
                    {
                        "radiant": list(observation.radiant_hero_ids),
                        "dire": list(observation.dire_hero_ids),
                    }
                )
                draft_hash = hashlib.sha256(draft_payload.encode("utf-8")).hexdigest()
                anchor = self.connection.execute(
                    """SELECT draft_hash, radiant_hero_ids, dire_hero_ids,
                              radiant_team_side, team_side_anchored_at,
                              team_side_source_frame_ref, anchored_at,
                              source_frame_ref, status, conflict_at
                         FROM vision_draft_anchors
                        WHERE raybet_match_id=? AND map_number=?""",
                    (observation.raybet_match_id, observation.map_number),
                ).fetchone()
                if anchor is None:
                    self.connection.execute(
                        """INSERT INTO vision_draft_anchors
                           (raybet_match_id, map_number, draft_hash,
                            radiant_hero_ids, dire_hero_ids,
                            radiant_team_side, team_side_anchored_at,
                            team_side_source_frame_ref, anchored_at,
                            source_frame_ref, status, conflict_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                   'anchored', NULL)""",
                        (
                            observation.raybet_match_id,
                            observation.map_number,
                            draft_hash,
                            radiant_json,
                            dire_json,
                            radiant_team_side,
                            captured_at if radiant_team_side is not None else None,
                            observation.source_frame_ref
                            if radiant_team_side is not None
                            else None,
                            captured_at,
                            observation.source_frame_ref,
                        ),
                    )
                else:
                    stored_confirmed = self._rebuild_vision_draft_anchor(
                        observation, anchor
                    )
            cursor = self.connection.execute(
                """INSERT OR IGNORE INTO vision_observations
                (raybet_match_id, map_number, captured_at, game_clock_seconds,
                 is_paused, radiant_hero_ids, dire_hero_ids, radiant_team_side,
                 clock_confidence, draft_confidence, source_frame_ref, screen_state,
                 confirmed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observation.raybet_match_id,
                    observation.map_number,
                    captured_at,
                    observation.game_clock_seconds,
                    None
                    if observation.is_paused is None
                    else int(observation.is_paused),
                    radiant_json,
                    dire_json,
                    radiant_team_side,
                    observation.clock_confidence,
                    observation.draft_confidence,
                    observation.source_frame_ref,
                    observation.screen_state,
                    int(stored_confirmed),
                ),
            )
            return cursor.rowcount == 1

    def _invalidate_vision_dependents(
        self,
        raybet_match_id: str,
        map_number: int,
        reason: str,
        conflict_at: str | None,
        *,
        block_reason: str,
        block_actor: str,
    ) -> None:
        """Append fail-closed invalidations for a causal vision cutoff."""
        if not block_reason.strip():
            raise ValueError("block_reason is required")
        if not block_actor.strip():
            raise ValueError("block_actor is required")
        recorded_time = datetime.now(timezone.utc)
        recorded_at = recorded_time.isoformat()
        dependent_queries = (
            (
                "odds_alignment",
                """SELECT alignment.odds_snapshot_id
                     FROM odds_alignments AS alignment
                     JOIN odds_snapshots AS snapshot
                       ON snapshot.id=alignment.odds_snapshot_id
                    WHERE alignment.raybet_match_id=?
                      AND alignment.map_number=?
                      AND (
                            julianday(?) IS NULL
                            OR julianday(snapshot.received_at) IS NULL
                            OR julianday(snapshot.received_at)>=julianday(?)
                            OR julianday(alignment.observation_captured_at) IS NULL
                            OR julianday(alignment.observation_captured_at)>=julianday(?)
                      )""",
            ),
            (
                "strategy_decision",
                """SELECT decision_key FROM strategy_decisions
                    WHERE raybet_match_id=? AND map_number=?
                      AND (
                            julianday(?) IS NULL
                            OR julianday(decided_at) IS NULL
                            OR julianday(decided_at)>=julianday(?)
                      )""",
            ),
            (
                "research_prediction",
                """SELECT prediction_key FROM research_live_predictions
                    WHERE raybet_match_id=? AND map_number=?
                      AND (
                            julianday(?) IS NULL
                            OR julianday(observed_at) IS NULL
                            OR julianday(observed_at)>=julianday(?)
                      )""",
            ),
        )
        for dependent_type, query in dependent_queries:
            try:
                rows = self.connection.execute(
                    query,
                    (
                        (raybet_match_id, map_number, conflict_at, conflict_at, conflict_at)
                        if dependent_type == "odds_alignment"
                        else (raybet_match_id, map_number, conflict_at, conflict_at)
                    ),
                ).fetchall()
            except sqlite3.OperationalError:
                if dependent_type == "strategy_decision":
                    raise
                continue
            self.connection.executemany(
                """INSERT OR IGNORE INTO vision_derived_invalidations
                   (dependent_type, dependent_key, raybet_match_id, map_number,
                    reason, block_reason, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    (
                        dependent_type,
                        str(row[0]),
                        raybet_match_id,
                        map_number,
                        reason,
                        block_reason,
                        recorded_at,
                    )
                    for row in rows
                ),
            )
        rows = self.connection.execute(
            """SELECT orders.order_key
                 FROM shadow_orders AS orders
                 JOIN shadow_map_attempts AS attempt
                   ON attempt.order_key=orders.order_key
                WHERE attempt.raybet_match_id=? AND attempt.map_number=?
                  AND (
                        julianday(?) IS NULL
                        OR julianday(orders.signal_transport_at) IS NULL
                        OR julianday(orders.signal_transport_at)>=julianday(?)
                  )""",
            (raybet_match_id, map_number, conflict_at, conflict_at),
        ).fetchall()
        order_keys = [str(row[0]) for row in rows]
        self.connection.executemany(
            """INSERT OR IGNORE INTO vision_derived_invalidations
               (dependent_type, dependent_key, raybet_match_id, map_number,
                reason, block_reason, recorded_at)
                VALUES ('shadow_order', ?, ?, ?, ?, ?, ?)""",
            (
                (
                    order_key,
                    raybet_match_id,
                    map_number,
                    reason,
                    block_reason,
                    recorded_at,
                )
                for order_key in order_keys
            ),
        )
        for order_key in order_keys:
            self.connection.execute(
                """UPDATE settlements
                      SET review_required=1
                    WHERE order_key=?""",
                (order_key,),
            )
            self.connection.execute(
                """UPDATE settlement_reconciliations
                          SET status='manual_review',
                              reason=CASE WHEN status='manual_review'
                                          THEN reason
                                          ELSE ? END,
                              updated_at=?
                        WHERE raybet_match_id=? AND map_number=?""",
                (block_reason, recorded_at, raybet_match_id, map_number),
            )
            outbox_rows = self.connection.execute(
                """SELECT outbox_id FROM notification_outbox
                    WHERE order_key=? AND event_type IN ('filled', 'settled')
                      AND status IN ('pending', 'leased')""",
                (order_key,),
            ).fetchall()
            for outbox_row in outbox_rows:
                outbox_id = int(outbox_row[0])
                self.quarantine_notification(
                    outbox_id=outbox_id,
                    reason=block_reason,
                    actor=block_actor,
                    now=recorded_time,
                )

    def _invalidate_draft_dependents(
        self,
        raybet_match_id: str,
        map_number: int,
        reason: str,
        conflict_at: str | None,
    ) -> None:
        """Append fail-closed invalidations for a draft conflict."""
        self._invalidate_vision_dependents(
            raybet_match_id,
            map_number,
            reason,
            conflict_at,
            block_reason="vision_draft_conflict",
            block_actor="vision_conflict",
        )
        self._review_settlements_after_draft_conflict(
            raybet_match_id, map_number, conflict_at
        )

    def _review_settlements_after_draft_conflict(
        self,
        raybet_match_id: str,
        map_number: int,
        conflict_at: str | None,
    ) -> None:
        """Quarantine results observed after a draft conflict event-time."""
        recorded_time = datetime.now(timezone.utc)
        recorded_at = recorded_time.isoformat()
        rows = self.connection.execute(
            """SELECT settlement.order_key
                 FROM settlements AS settlement
                 JOIN shadow_orders AS orders
                   ON orders.order_key=settlement.order_key
                 JOIN shadow_map_attempts AS attempt
                   ON attempt.order_key=orders.order_key
                WHERE attempt.raybet_match_id=? AND attempt.map_number=?
                  AND (
                        julianday(?) IS NULL
                        OR julianday(settlement.settled_at) IS NULL
                        OR julianday(settlement.settled_at)>=julianday(?)
                  )""",
            (raybet_match_id, map_number, conflict_at, conflict_at),
        ).fetchall()
        order_keys = [str(row["order_key"]) for row in rows]
        for order_key in order_keys:
            self.connection.execute(
                """UPDATE settlements SET review_required=1
                    WHERE order_key=?""",
                (order_key,),
            )
            outbox_rows = self.connection.execute(
                """SELECT outbox_id FROM notification_outbox
                    WHERE order_key=? AND event_type='settled'
                      AND status IN ('pending', 'leased')""",
                (order_key,),
            ).fetchall()
            for outbox_row in outbox_rows:
                outbox_id = int(outbox_row["outbox_id"])
                self.quarantine_notification(
                    outbox_id=outbox_id,
                    reason="vision_draft_conflict",
                    actor="vision_conflict",
                    now=recorded_time,
                )
        self.connection.execute(
            """UPDATE settlement_reconciliations
                  SET status='manual_review',
                      reason=CASE WHEN status='manual_review'
                                  THEN reason
                                  ELSE 'vision_draft_conflict' END,
                      updated_at=?
                WHERE raybet_match_id=? AND map_number=?
                  AND (
                        julianday(?) IS NULL
                        OR julianday(first_observed_at) IS NULL
                        OR julianday(first_observed_at)>=julianday(?)
                  )""",
            (recorded_at, raybet_match_id, map_number, conflict_at, conflict_at),
        )

    def insert_alignment(self, alignment: Any) -> bool:
        values = (
            alignment.odds_snapshot_id,
            alignment.raybet_match_id,
            alignment.map_number,
            alignment.game_clock_seconds,
            alignment.observation_captured_at.isoformat()
            if alignment.observation_captured_at
            else None,
            alignment.method,
            alignment.lag_seconds,
            int(alignment.usable),
            alignment.reason,
        )
        with self.transaction():
            existing = self.connection.execute(
                "SELECT * FROM odds_alignments WHERE odds_snapshot_id=?",
                (alignment.odds_snapshot_id,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != values:
                    raise ValueError("odds alignment identity conflict")
                return False
            if alignment.map_number is not None:
                snapshot = self.connection.execute(
                    "SELECT received_at FROM odds_snapshots WHERE id=?",
                    (alignment.odds_snapshot_id,),
                ).fetchone()
                blocked = self._draft_conflict_at_or_before(
                    str(alignment.raybet_match_id),
                    int(alignment.map_number),
                    alignment.observation_captured_at,
                )
                if snapshot is not None:
                    blocked = blocked or self._draft_conflict_at_or_before(
                        str(alignment.raybet_match_id),
                        int(alignment.map_number),
                        snapshot["received_at"],
                    )
                elif self._draft_conflict_state(
                    str(alignment.raybet_match_id), int(alignment.map_number)
                )[0]:
                    blocked = True
                if blocked:
                    return False
            self.connection.execute(
                """INSERT INTO odds_alignments VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
            return True

    def insert_decision(self, decision: Any) -> bool:
        values = (
            decision.decision_key,
            decision.raybet_match_id,
            decision.map_number,
            decision.decided_at.isoformat(),
            decision.underdog_side,
            decision.market_probability,
            decision.model_probability,
            decision.edge,
            decision.data_quality,
            int(decision.eligible),
            decision.reason,
            self.json(decision.contributions),
            decision.input_ref,
            decision.strategy_version,
        )
        with self.transaction():
            existing = self.connection.execute(
                "SELECT * FROM strategy_decisions WHERE decision_key=?",
                (decision.decision_key,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != values:
                    raise ValueError("strategy decision identity conflict")
                return False
            if self._draft_conflict_at_or_before(
                str(decision.raybet_match_id),
                int(decision.map_number),
                decision.decided_at,
            ):
                return False
            self.connection.execute(
                """INSERT INTO strategy_decisions VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
            return True

    def insert_research_prediction(self, prediction: Any) -> bool:
        """Append one non-actionable live prediction without touching order tables."""
        with self.transaction():
            if self._draft_conflict_at_or_before(
                str(prediction.raybet_match_id),
                int(prediction.map_number),
                prediction.observed_at,
            ):
                return False
            cursor = self.connection.execute(
                """INSERT INTO research_live_predictions
               (prediction_key, schema_version, raybet_match_id, map_number,
                observed_at, game_clock_seconds, game_minute, selected_side,
                market_probability, market_price, raw_model_probability,
                feature_hash, model_hash, calibration_hash, transport_key,
                transport_hash, radiant_hero_ids_json, dire_hero_ids_json,
                radiant_team_side, strict_mapping_id, clock_source, clock_trust,
                manual_clock_event_id, manual_clock_seconds, manual_clock_trust,
                manual_clock_validation, actionability, gate_status,
                gate_failures_json, input_context_hash, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(prediction_key) DO NOTHING""",
                (
                    prediction.prediction_key,
                    prediction.schema_version,
                    prediction.raybet_match_id,
                    prediction.map_number,
                    self._iso(prediction.observed_at),
                    prediction.game_clock_seconds,
                    prediction.game_minute,
                    prediction.selected_side,
                    prediction.market_probability,
                    prediction.market_price,
                    prediction.raw_model_probability,
                    prediction.feature_hash,
                    prediction.model_hash,
                    prediction.calibration_hash,
                    prediction.transport_key,
                    prediction.transport_hash,
                    self.json(list(prediction.radiant_hero_ids)),
                    self.json(list(prediction.dire_hero_ids)),
                    prediction.radiant_team_side,
                    prediction.strict_mapping_id,
                    prediction.clock_source,
                    prediction.clock_trust,
                    prediction.manual_clock_event_id,
                    prediction.manual_clock_seconds,
                    prediction.manual_clock_trust,
                    prediction.manual_clock_validation,
                    prediction.actionability,
                    prediction.gate_status,
                    self.json(list(prediction.gate_failures)),
                    prediction.input_context_hash,
                    self._iso(prediction.created_at),
                ),
            )
            return cursor.rowcount == 1

    def insert_research_price_label(self, label: Any) -> bool:
        cursor = self.execute(
            """INSERT INTO research_price_labels
               (label_key, prediction_key, transport_key, transport_hash,
                observed_at, selected_side, price, market_probability,
                seconds_after_prediction, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(prediction_key) DO NOTHING""",
            (
                label.label_key,
                label.prediction_key,
                label.transport_key,
                label.transport_hash,
                self._iso(label.observed_at),
                label.selected_side,
                label.price,
                label.market_probability,
                label.seconds_after_prediction,
                self._iso(label.created_at),
            ),
        )
        return cursor.rowcount == 1

    def reserve_map_attempt(
        self, raybet_match_id: str, map_number: int, order_key: str,
        status: str, created_at: datetime,
    ) -> bool:
        cursor = self.execute(
            """INSERT OR IGNORE INTO shadow_map_attempts VALUES (?, ?, ?, ?, ?)""",
            (raybet_match_id, map_number, order_key, status, created_at.isoformat()),
        )
        return cursor.rowcount == 1

    def update_map_attempt(
        self,
        order_key: str,
        status: str,
        *,
        expected_status: str | None = None,
    ) -> bool:
        if expected_status is None:
            cursor = self.execute(
                "UPDATE shadow_map_attempts SET status=? WHERE order_key=?",
                (status, order_key),
            )
        else:
            cursor = self.execute(
                """UPDATE shadow_map_attempts SET status=?
                    WHERE order_key=? AND status=?""",
                (status, order_key, expected_status),
            )
        return cursor.rowcount == 1

    def has_map_attempt(self, raybet_match_id: str, map_number: int) -> bool:
        row = self.connection.execute(
            """SELECT 1 FROM shadow_map_attempts
               WHERE raybet_match_id=? AND map_number=?""",
            (raybet_match_id, map_number),
        ).fetchone()
        return row is not None

    def pending_order_has_draft_conflict(self, order_key: str) -> bool:
        return self.pending_order_block_reason(order_key) is not None

    def pending_order_block_reason(self, order_key: str) -> str | None:
        row = self.connection.execute(
            """SELECT orders.raybet_match_id, attempt.map_number,
                      orders.signal_transport_at
                 FROM shadow_orders AS orders
                 JOIN shadow_map_attempts AS attempt
                   ON attempt.order_key=orders.order_key
                WHERE attempt.order_key=? AND attempt.status='pending'
            """,
            (order_key,),
        ).fetchone()
        if row is None:
            return None
        strict = self._strict_mapping_block_reason_for_order(order_key)
        if strict is not None:
            return strict
        derived = self._vision_derived_block_reason(order_key)
        if derived is not None:
            return derived
        if self._vision_observation_invalidated_at_or_before(
            str(row["raybet_match_id"]),
            int(row["map_number"]),
            row["signal_transport_at"],
        ):
            return "vision_observation_invalidated"
        if self._draft_conflict_at_or_before(
            str(row["raybet_match_id"]),
            int(row["map_number"]),
            row["signal_transport_at"],
        ):
            return "vision_draft_conflict"
        return None

    def reject_pending_order(
        self,
        order: ShadowOrder,
        *,
        reason: str,
    ) -> ShadowOrder | None:
        """Atomically reject one persisted pending order without scheduling mail."""
        if not reason.strip():
            raise ValueError("rejection reason is required")
        resolved = replace(
            order,
            status="rejected",
            fill_price=None,
            filled_at=None,
            rejection_reason=reason,
        )
        with self.transaction():
            cursor = self.connection.execute(
                """UPDATE shadow_orders
                      SET status='rejected', fill_price=NULL, filled_at=NULL,
                          rejection_reason=?
                    WHERE order_key=? AND status='pending'""",
                (reason, order.order_key),
            )
            if cursor.rowcount != 1:
                return None
            if not self.update_map_attempt(
                order.order_key, "rejected", expected_status="pending"
            ):
                raise RuntimeError("pending order has no matching pending map attempt")
        return resolved

    def _matching_strategy_decision_candidates(
        self,
        order: ShadowOrder,
        map_number: int,
        strict_mapping_id: int | None = None,
    ) -> list[tuple[str, int]]:
        """Find decisions that cryptographically own an order."""
        try:
            rows = self.connection.execute(
                """SELECT decision_key, strategy_version, input_ref,
                          contributions_json
                     FROM strategy_decisions
                    WHERE raybet_match_id=? AND map_number=? AND decided_at=?
                      AND underdog_side=? AND eligible=1
                      AND model_probability=? AND market_probability=?
                    ORDER BY decision_key""",
                (
                    order.raybet_match_id,
                    map_number,
                    self._iso(order.signal_transport_at),
                    order.signal_outcome_key,
                    order.model_probability,
                    order.market_probability,
                ),
            ).fetchall()
        except sqlite3.Error:
            return []
        matches: list[tuple[str, int]] = []
        for row in rows:
            identity = "|".join(
                (
                    order.raybet_match_id,
                    order.odds_id,
                    order.signal_odds_group_id or "",
                    order.signal_outcome_key or "",
                    market_key(
                        order.market.market_type,
                        order.market.period,
                        order.market.side,
                        order.market.line,
                    ),
                    str(row["strategy_version"]),
                    str(row["input_ref"]),
                )
            )
            if hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32] != order.order_key:
                continue
            try:
                contributions = json.loads(str(row["contributions_json"]))
                mapping_id = contributions["__inputs__"][
                    "strict_live_eligibility"
                ]["mapping_refs"]["strict_mapping_id"]
                mapping_id = int(mapping_id)
                if mapping_id <= 0 or (
                    strict_mapping_id is not None
                    and mapping_id != strict_mapping_id
                ):
                    continue
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            matches.append((str(row["decision_key"]), mapping_id))
        return matches

    def _matching_strategy_decision_key(
        self,
        order: ShadowOrder,
        map_number: int,
        strict_mapping_id: int,
    ) -> str | None:
        matches = self._matching_strategy_decision_candidates(
            order, map_number, strict_mapping_id
        )
        return matches[0][0] if len(matches) == 1 else None

    def insert_map_order(
        self,
        order: ShadowOrder,
        map_number: int,
        *,
        strict_mapping_id: int,
        decision_key: str | None = None,
    ) -> bool:
        """Atomically reserve a map and persist its only shadow order."""
        if (
            isinstance(strict_mapping_id, bool)
            or not isinstance(strict_mapping_id, int)
            or strict_mapping_id <= 0
        ):
            raise ValueError("strict_mapping_id must be a positive integer")
        if (
            order.status != "pending"
            or order.fill_price is not None
            or order.filled_at is not None
            or order.rejection_reason is not None
        ):
            return False
        numeric_values = (
            order.model_probability,
            order.market_probability,
            order.signal_price,
            order.stake,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in numeric_values
        ):
            return False
        if (
            not 0.0 <= float(order.model_probability) <= 1.0
            or not 0.0 <= float(order.market_probability) <= 1.0
            or float(order.signal_price) <= 1.0
            or float(order.stake) != 1.0
        ):
            return False
        if not self._signal_identity_matches(order):
            return False
        if decision_key is not None:
            decision_key = str(decision_key).strip()
            if not decision_key:
                raise ValueError("decision_key must be non-empty when provided")
        with self.transaction():
            if self._strict_mapping_context_block_reason(
                strict_mapping_id=strict_mapping_id,
                raybet_match_id=order.raybet_match_id,
                map_number=map_number,
                signal_transport_at=order.signal_transport_at,
            ) is not None:
                return False
            if self._draft_conflict_at_or_before(
                order.raybet_match_id,
                map_number,
                order.signal_transport_at,
            ):
                return False
            matched_decision_key = self._matching_strategy_decision_key(
                order, map_number, strict_mapping_id
            )
            if matched_decision_key is None or (
                decision_key is not None and decision_key != matched_decision_key
            ):
                return False
            decision_key = matched_decision_key
            reserved = self.connection.execute(
                """INSERT OR IGNORE INTO shadow_map_attempts
                   VALUES (?, ?, ?, ?, ?)""",
                (order.raybet_match_id, map_number, order.order_key,
                 order.status, order.signaled_at.isoformat()),
            )
            if reserved.rowcount != 1:
                return False
            self.connection.execute(
                """INSERT INTO shadow_orders
                (order_key, raybet_match_id, strict_mapping_id, odds_id,
                 market_key, signaled_at,
                 model_probability, market_probability, signal_price,
                 signal_transport_key, signal_transport_at, expires_at,
                 signal_odds_group_id, signal_outcome_key,
                 signal_identity_verified, stake, status, fill_price, filled_at,
                 rejection_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (order.order_key, order.raybet_match_id, strict_mapping_id,
                 order.odds_id,
                 market_key(order.market.market_type, order.market.period,
                            order.market.side, order.market.line),
                 order.signaled_at.isoformat(), order.model_probability,
                 order.market_probability, order.signal_price,
                 order.signal_transport_key, self._iso(order.signal_transport_at),
                 self._iso(order.expires_at), order.signal_odds_group_id,
                 order.signal_outcome_key, int(order.signal_identity_verified),
                 order.stake, order.status,
                 order.fill_price,
                 self._iso(order.filled_at) if order.filled_at else None,
                  order.rejection_reason),
            )
            self.connection.execute(
                """INSERT INTO shadow_order_decision_lineage
                   (order_key, decision_key, recorded_at)
                   VALUES (?, ?, ?)""",
                (order.order_key, decision_key, self._iso(order.signaled_at)),
            )
            return True

    def insert_map_result(self, result: Any) -> bool:
        cursor = self.execute(
            """INSERT OR IGNORE INTO map_results VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (result.raybet_match_id, result.map_number, result.dota_match_id,
             result.winner_side, result.team_one_kills, result.team_two_kills,
             result.duration_seconds, result.evidence_ref,
             result.settled_at.isoformat()),
        )
        return cursor.rowcount == 1

    def record_settlement_reconciliation(
        self,
        *,
        raybet_match_id: str,
        map_number: int,
        dota_match_id: int,
        raybet_status: str,
        raybet_winner_side: str | None,
        opendota_winner_side: str,
        raybet_evidence_ref: str,
        opendota_evidence_ref: str,
        raybet_facts: Mapping[str, object],
        opendota_facts: Mapping[str, object],
        status: str,
        reason: str,
        observed_at: datetime,
    ) -> sqlite3.Row:
        """Persist both source facts and a sticky fail-closed resolution."""
        observed = self._iso(observed_at)
        raybet_facts_json = self.json(raybet_facts)
        opendota_facts_json = self.json(opendota_facts)
        with self.transaction():
            existing = self.connection.execute(
                """SELECT * FROM settlement_reconciliations
                    WHERE raybet_match_id=? AND map_number=?""",
                (raybet_match_id, map_number),
            ).fetchone()
            lineage = self.connection.execute(
                """SELECT orders.order_key, orders.signal_transport_at
                     FROM shadow_orders AS orders
                     JOIN shadow_map_attempts AS attempt
                       ON attempt.order_key=orders.order_key
                    WHERE attempt.raybet_match_id=? AND attempt.map_number=?""",
                (raybet_match_id, map_number),
            ).fetchall()
            blocked_reason = None
            for row in lineage:
                candidate = self.order_block_reason(str(row["order_key"]))
                if candidate is not None:
                    blocked_reason = candidate
                    break
            settlement_event_at = (
                existing["first_observed_at"]
                if existing is not None and existing["status"] == "confirmed"
                else observed
            )
            if self._draft_conflict_effective_at(
                raybet_match_id, map_number, settlement_event_at
            ):
                blocked_reason = "vision_draft_conflict"
            if status != "manual_review" and blocked_reason is not None:
                status, reason = "manual_review", blocked_reason
            evidence_rows = (
                (
                    "raybet",
                    raybet_status,
                    raybet_winner_side,
                    raybet_evidence_ref,
                    raybet_facts_json,
                ),
                (
                    "opendota",
                    "confirmed",
                    opendota_winner_side,
                    opendota_evidence_ref,
                    opendota_facts_json,
                ),
            )
            for source, source_status, winner, evidence_ref, facts_json in evidence_rows:
                cursor = self.connection.execute(
                    """INSERT OR IGNORE INTO settlement_result_evidence
                       (raybet_match_id, map_number, dota_match_id, source, status,
                        winner_side, evidence_ref, facts_json, observed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        raybet_match_id,
                        map_number,
                        dota_match_id,
                        source,
                        source_status,
                        winner,
                        evidence_ref,
                        facts_json,
                        observed,
                    ),
                )
                if cursor.rowcount == 0:
                    existing_evidence = self.connection.execute(
                        """SELECT status, winner_side, facts_json
                             FROM settlement_result_evidence
                            WHERE raybet_match_id=? AND map_number=?
                              AND source=? AND evidence_ref=?""",
                        (raybet_match_id, map_number, source, evidence_ref),
                    ).fetchone()
                    if existing_evidence is None or tuple(existing_evidence) != (
                        source_status,
                        winner,
                        facts_json,
                    ):
                        raise ValueError("settlement evidence reference was reused")

            linked_elsewhere = self.connection.execute(
                """SELECT raybet_match_id, map_number
                     FROM settlement_reconciliations
                    WHERE dota_match_id=?
                      AND (raybet_match_id!=? OR map_number!=?)
                    UNION
                   SELECT raybet_match_id, map_number
                     FROM map_results
                    WHERE dota_match_id=?
                      AND (raybet_match_id!=? OR map_number!=?)""",
                (
                    dota_match_id,
                    raybet_match_id,
                    map_number,
                    dota_match_id,
                    raybet_match_id,
                    map_number,
                ),
            ).fetchall()
            link_conflict = bool(linked_elsewhere)
            if link_conflict:
                self.connection.execute(
                    """UPDATE settlement_reconciliations
                          SET status='manual_review',
                              reason=CASE
                                WHEN status='manual_review' THEN reason
                                ELSE 'opendota_match_link_conflict'
                              END,
                              updated_at=?
                        WHERE dota_match_id=?
                          AND (raybet_match_id!=? OR map_number!=?)""",
                    (observed, dota_match_id, raybet_match_id, map_number),
                )
                for linked in linked_elsewhere:
                    self.connection.execute(
                        """UPDATE settlements SET review_required=1
                            WHERE order_key IN (
                                SELECT order_key FROM shadow_map_attempts
                                 WHERE raybet_match_id=? AND map_number=?
                            )""",
                        (linked["raybet_match_id"], linked["map_number"]),
                    )

            effective_status = "manual_review" if link_conflict else status
            effective_reason = (
                "opendota_match_link_conflict" if link_conflict else reason
            )
            effective_dota_match_id = dota_match_id
            effective_raybet_winner = raybet_winner_side
            effective_opendota_winner = opendota_winner_side
            effective_raybet_ref = raybet_evidence_ref
            effective_opendota_ref = opendota_evidence_ref
            if existing is not None and existing["status"] == "manual_review":
                effective_status = "manual_review"
                effective_reason = str(existing["reason"])
                effective_dota_match_id = int(existing["dota_match_id"])
                effective_raybet_winner = existing["raybet_winner_side"]
                effective_opendota_winner = str(existing["opendota_winner_side"])
                effective_raybet_ref = str(existing["raybet_evidence_ref"])
                effective_opendota_ref = str(existing["opendota_evidence_ref"])
            elif existing is not None and existing["status"] == "confirmed" and (
                effective_status != "confirmed"
                or existing["raybet_winner_side"] != raybet_winner_side
                or existing["opendota_winner_side"] != opendota_winner_side
                or int(existing["dota_match_id"]) != dota_match_id
            ):
                effective_status = "manual_review"
                effective_reason = (
                    "opendota_match_link_conflict"
                    if link_conflict
                    else (
                        reason
                        if reason in {
                            "stored_map_result_conflict",
                            "map_result_persistence_conflict",
                        }
                        else "source_result_changed"
                    )
                )
                effective_dota_match_id = int(existing["dota_match_id"])
                effective_raybet_winner = existing["raybet_winner_side"]
                effective_opendota_winner = str(existing["opendota_winner_side"])
                effective_raybet_ref = str(existing["raybet_evidence_ref"])
                effective_opendota_ref = str(existing["opendota_evidence_ref"])

            self.connection.execute(
                """INSERT INTO settlement_reconciliations
                   (raybet_match_id, map_number, dota_match_id,
                    raybet_winner_side, opendota_winner_side,
                    raybet_evidence_ref, opendota_evidence_ref, status, reason,
                    first_observed_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(raybet_match_id, map_number) DO UPDATE SET
                     dota_match_id=excluded.dota_match_id,
                     raybet_winner_side=excluded.raybet_winner_side,
                     opendota_winner_side=excluded.opendota_winner_side,
                     raybet_evidence_ref=excluded.raybet_evidence_ref,
                     opendota_evidence_ref=excluded.opendota_evidence_ref,
                     status=excluded.status,
                     reason=excluded.reason,
                     updated_at=excluded.updated_at""",
                (
                    raybet_match_id,
                    map_number,
                    effective_dota_match_id,
                    effective_raybet_winner,
                    effective_opendota_winner,
                    effective_raybet_ref,
                    effective_opendota_ref,
                    effective_status,
                    effective_reason,
                    observed,
                    observed,
                ),
            )
            if effective_status == "manual_review":
                self.connection.execute(
                    """UPDATE settlements SET review_required=1
                        WHERE order_key IN (
                            SELECT order_key FROM shadow_map_attempts
                             WHERE raybet_match_id=? AND map_number=?
                        )""",
                    (raybet_match_id, map_number),
                )
            row = self.connection.execute(
                """SELECT * FROM settlement_reconciliations
                    WHERE raybet_match_id=? AND map_number=?""",
                (raybet_match_id, map_number),
            ).fetchone()
            assert row is not None
            return row

    def enqueue_notification(
        self,
        *,
        order_key: str,
        event_type: str,
        payload: Mapping[str, Any],
        stats_cutoff_at: datetime,
        created_at: datetime,
    ) -> bool:
        from .notifications import enqueue

        return enqueue(
            self.connection,
            order_key=order_key,
            event_type=event_type,
            payload=payload,
            stats_cutoff_at=stats_cutoff_at,
            created_at=created_at,
        )

    def quarantine_notification(
        self,
        *,
        outbox_id: int,
        reason: str,
        actor: str,
        now: datetime,
    ) -> bool:
        from .notifications import quarantine_outbox

        return quarantine_outbox(
            self.connection,
            outbox_id=outbox_id,
            reason=reason,
            actor=actor,
            now=now,
        )

    def insert_settlement(
        self, order_key: str, result: str, return_units: float,
        settled_at: datetime, evidence_ref: str, review_required: bool = False,
    ) -> bool:
        with self.transaction():
            order = None
            if not review_required:
                order = self.connection.execute(
                    """SELECT orders.raybet_match_id, orders.market_key,
                              orders.fill_price, orders.filled_at,
                              orders.status AS order_status,
                              attempt.status AS attempt_status
                         FROM shadow_orders AS orders
                         JOIN shadow_map_attempts AS attempt
                           ON attempt.order_key=orders.order_key
                        WHERE orders.order_key=?""",
                    (order_key,),
                ).fetchone()
                if (
                    order is None
                    or str(order["order_status"]) != "filled"
                    or str(order["attempt_status"]) != "filled"
                    or order["filled_at"] is None
                    or isinstance(order["fill_price"], bool)
                    or not isinstance(order["fill_price"], (int, float))
                    or float(order["fill_price"]) <= 1.0
                ):
                    return False
            if not review_required and self._order_draft_conflict_effective_at(
                order_key, settled_at
            ):
                result = "review"
                return_units = 0.0
                review_required = True
            if not review_required:
                if self.order_block_reason(order_key) is not None:
                    return False
            cursor = self.connection.execute(
                """INSERT OR IGNORE INTO settlements VALUES (?, ?, ?, ?, ?, ?)""",
                (order_key, result, return_units, settled_at.isoformat(), evidence_ref,
                 int(review_required)),
            )
            if cursor.rowcount != 1:
                return False
            if not review_required:
                assert order is not None
                from .notifications import EVENT_SETTLED, settled_order_payload

                self.enqueue_notification(
                    order_key=order_key,
                    event_type=EVENT_SETTLED,
                    payload=settled_order_payload(
                        self.connection,
                        order_key,
                        result=result,
                        return_units=return_units,
                        settled_at=settled_at,
                        evidence_ref=evidence_ref,
                    ),
                    stats_cutoff_at=settled_at,
                    created_at=settled_at,
                )
            return True
