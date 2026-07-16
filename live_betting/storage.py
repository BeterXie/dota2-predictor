"""SQLite persistence for live collection and shadow orders."""

from __future__ import annotations

import hashlib
import json
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
from .strategy import attempt_fill, is_open


CURRENT_SCHEMA_VERSION = 1


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
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (raybet_match_id, map_number, captured_at, source_frame_ref),
    FOREIGN KEY (raybet_match_id, map_number)
        REFERENCES vision_draft_anchors(raybet_match_id, map_number)
);
CREATE TRIGGER IF NOT EXISTS vision_draft_anchor_identity_immutable
BEFORE UPDATE ON vision_draft_anchors
WHEN OLD.raybet_match_id IS NOT NEW.raybet_match_id
  OR OLD.map_number IS NOT NEW.map_number
  OR OLD.draft_hash IS NOT NEW.draft_hash
  OR OLD.radiant_hero_ids IS NOT NEW.radiant_hero_ids
  OR OLD.dire_hero_ids IS NOT NEW.dire_hero_ids
  OR OLD.anchored_at IS NOT NEW.anchored_at
  OR OLD.source_frame_ref IS NOT NEW.source_frame_ref
  OR OLD.status='conflict'
  OR NEW.status!='conflict'
  OR NEW.conflict_at IS NULL
BEGIN
    SELECT RAISE(ABORT, 'vision draft anchor is immutable');
END;
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
        self.connection.executescript(SCHEMA_SQL)
        self._migrate_shadow_order_signal_fields()
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

        self.connection.execute(
            """UPDATE shadow_orders
                  SET strict_mapping_id=(
                      SELECT CAST(json_extract(
                                 decision.contributions_json,
                                 '$.__inputs__.strict_live_eligibility.mapping_refs.strict_mapping_id'
                             ) AS INTEGER)
                        FROM shadow_map_attempts AS attempt
                        JOIN strategy_decisions AS decision
                          ON decision.raybet_match_id=attempt.raybet_match_id
                         AND decision.map_number=attempt.map_number
                       WHERE attempt.order_key=shadow_orders.order_key
                         AND decision.decided_at=shadow_orders.signaled_at
                         AND decision.model_probability=shadow_orders.model_probability
                         AND decision.market_probability=shadow_orders.market_probability
                         AND decision.eligible=1
                         AND json_valid(decision.contributions_json)
                         AND CAST(json_extract(
                                 decision.contributions_json,
                                 '$.__inputs__.strict_live_eligibility.mapping_refs.strict_mapping_id'
                             ) AS INTEGER)>0
                       ORDER BY decision.decision_key
                       LIMIT 1
                  )
                WHERE strict_mapping_id IS NULL"""
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
            """
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
             match.best_of, match.status, self.json(match.raw), updated_at.isoformat()),
        )

    def upsert_raybet_match(self, row: dict[str, Any], updated_at: datetime) -> None:
        teams = sorted(row.get("team") or [], key=lambda item: int(item.get("pos") or 0))
        team_one = str(teams[0].get("team_name") or "") if teams else ""
        team_two = str(teams[1].get("team_name") or "") if len(teams) > 1 else ""
        round_name = str(row.get("round") or "").lower()
        best_of = int(round_name[2:]) if round_name.startswith("bo") and round_name[2:].isdigit() else None
        self.execute(
            """INSERT INTO raybet_matches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(raybet_match_id) DO UPDATE SET
              tournament=excluded.tournament, team_one=excluded.team_one,
              team_two=excluded.team_two, scheduled_at=excluded.scheduled_at,
              best_of=excluded.best_of, status=excluded.status,
              live_url=excluded.live_url, raw_json=excluded.raw_json,
              updated_at=excluded.updated_at""",
            (str(row.get("id")), str(row.get("tournament_name") or ""), team_one, team_two,
             row.get("start_time"), best_of, str(row.get("status") or ""),
             row.get("live_url"), self.json(row), updated_at.isoformat()),
        )

    def insert_browser_raybet_match(
        self, row: dict[str, Any], updated_at: datetime
    ) -> bool:
        """Insert sanitized browser metadata without replacing direct-owned data."""
        teams = sorted(row.get("team") or [], key=lambda item: int(item.get("pos") or 0))
        team_one = str(teams[0].get("team_name") or "") if teams else ""
        team_two = str(teams[1].get("team_name") or "") if len(teams) > 1 else ""
        round_name = str(row.get("round") or "").lower()
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
            (str(row.get("id")), str(row.get("tournament_name") or ""),
             team_one, team_two, row.get("start_time"), best_of,
             str(row.get("status") or ""), self.json({}), updated_at.isoformat()),
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
                snapshot.raw,
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
             self.json(snapshot.raw)),
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

            signal_is_valid = self._signal_identity_matches(order)
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
            if not signal_is_valid:
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
                from .notifications import EVENT_FILLED, simulation_payload

                self.enqueue_notification(
                    order_key=resolved.order_key,
                    event_type=EVENT_FILLED,
                    payload=simulation_payload(
                        EVENT_FILLED,
                        {
                            "raybet_match_id": resolved.raybet_match_id,
                            "map_number": int(map_row["map_number"]),
                            "selected_side": resolved.market.side,
                            "signal_price": resolved.signal_price,
                            "fill_price": resolved.fill_price,
                            "model_probability": resolved.model_probability,
                            "market_probability": resolved.market_probability,
                            "edge": resolved.model_probability
                            - resolved.market_probability,
                            "signal_transport_at": resolved.signal_transport_at,
                            "filled_at": resolved.filled_at,
                            "order_key": resolved.order_key,
                        },
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
        if not self._signal_identity_matches(order):
            return False
        cursor = self.execute(
            """INSERT OR IGNORE INTO shadow_orders
            (order_key, raybet_match_id, odds_id, market_key, signaled_at,
             model_probability, market_probability, signal_price,
             signal_transport_key, signal_transport_at, expires_at,
             signal_odds_group_id, signal_outcome_key,
             signal_identity_verified, stake, status, fill_price, filled_at,
             rejection_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (order.order_key, order.raybet_match_id, order.odds_id,
             market_key(order.market.market_type, order.market.period,
                        order.market.side, order.market.line),
             order.signaled_at.isoformat(), order.model_probability,
             order.market_probability, order.signal_price,
             order.signal_transport_key, self._iso(order.signal_transport_at),
             self._iso(order.expires_at), order.signal_odds_group_id,
             order.signal_outcome_key, int(order.signal_identity_verified),
             order.stake, order.status,
             order.fill_price, self._iso(order.filled_at) if order.filled_at else None,
             order.rejection_reason),
        )
        return cursor.rowcount == 1

    def update_order(self, order: ShadowOrder) -> None:
        self.execute(
            """UPDATE shadow_orders SET status=?, fill_price=?, filled_at=?,
            rejection_reason=? WHERE order_key=?""",
            (order.status, order.fill_price,
             order.filled_at.isoformat() if order.filled_at else None,
             order.rejection_reason, order.order_key),
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
        stored_confirmed = bool(observation.is_confirmed)
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
                    """SELECT draft_hash, status, conflict_at
                         FROM vision_draft_anchors
                        WHERE raybet_match_id=? AND map_number=?""",
                    (observation.raybet_match_id, observation.map_number),
                ).fetchone()
                if anchor is None:
                    self.connection.execute(
                        """INSERT INTO vision_draft_anchors
                           (raybet_match_id, map_number, draft_hash,
                            radiant_hero_ids, dire_hero_ids, anchored_at,
                            source_frame_ref, status, conflict_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, 'anchored', NULL)""",
                        (
                            observation.raybet_match_id,
                            observation.map_number,
                            draft_hash,
                            radiant_json,
                            dire_json,
                            captured_at,
                            observation.source_frame_ref,
                        ),
                    )
                elif anchor["status"] == "conflict" or anchor["draft_hash"] != draft_hash:
                    reason = (
                        "map_draft_already_in_conflict"
                        if anchor["status"] == "conflict"
                        else "confirmed_draft_identity_changed"
                    )
                    self.connection.execute(
                        """INSERT OR IGNORE INTO vision_draft_conflicts
                           (raybet_match_id, map_number, captured_at,
                            source_frame_ref, observed_draft_hash,
                            radiant_hero_ids, dire_hero_ids, reason, recorded_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            observation.raybet_match_id,
                            observation.map_number,
                            captured_at,
                            observation.source_frame_ref,
                            draft_hash,
                            radiant_json,
                            dire_json,
                            reason,
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
                    if anchor["status"] != "conflict":
                        self.connection.execute(
                            """UPDATE vision_draft_anchors
                                  SET status='conflict', conflict_at=?
                                WHERE raybet_match_id=? AND map_number=?
                                  AND status='anchored'""",
                            (
                                captured_at,
                                observation.raybet_match_id,
                                observation.map_number,
                            ),
                        )
                    conflict_cutoff = (
                        captured_at
                        if anchor["status"] != "conflict"
                        else anchor["conflict_at"]
                    )
                    self._invalidate_draft_dependents(
                        observation.raybet_match_id,
                        int(observation.map_number),
                        reason,
                        conflict_cutoff,
                    )
                    stored_confirmed = False
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
                    observation.radiant_team_side,
                    observation.clock_confidence,
                    observation.draft_confidence,
                    observation.source_frame_ref,
                    observation.screen_state,
                    int(stored_confirmed),
                ),
            )
            return cursor.rowcount == 1

    def _invalidate_draft_dependents(
        self,
        raybet_match_id: str,
        map_number: int,
        reason: str,
        conflict_at: str | None,
    ) -> None:
        """Append fail-closed invalidations when a map draft is conflicted."""
        recorded_at = datetime.now(timezone.utc).isoformat()
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
                    (raybet_match_id, map_number, conflict_at, conflict_at),
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            self.connection.executemany(
                """INSERT OR IGNORE INTO vision_derived_invalidations
                   (dependent_type, dependent_key, raybet_match_id, map_number,
                    reason, recorded_at) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    (
                        dependent_type,
                        str(row[0]),
                        raybet_match_id,
                        map_number,
                        reason,
                        recorded_at,
                    )
                    for row in rows
                ),
            )
        try:
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
        except sqlite3.OperationalError:
            return
        self.connection.executemany(
            """INSERT OR IGNORE INTO vision_derived_invalidations
               (dependent_type, dependent_key, raybet_match_id, map_number,
                reason, recorded_at) VALUES ('shadow_order', ?, ?, ?, ?, ?)""",
            (
                (str(row[0]), raybet_match_id, map_number, reason, recorded_at)
                for row in rows
            ),
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
            self.connection.execute(
                """INSERT INTO strategy_decisions VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
            return True

    def insert_research_prediction(self, prediction: Any) -> bool:
        """Append one non-actionable live prediction without touching order tables."""
        cursor = self.execute(
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
        row = self.connection.execute(
            """SELECT 1 FROM shadow_orders AS orders
                 JOIN shadow_map_attempts AS attempt
                   ON attempt.order_key=orders.order_key
                 JOIN vision_draft_anchors AS anchor
                   ON anchor.raybet_match_id=attempt.raybet_match_id
                  AND anchor.map_number=attempt.map_number
                WHERE attempt.order_key=? AND attempt.status='pending'
                  AND anchor.status='conflict'
                  AND (
                        anchor.conflict_at IS NULL
                        OR julianday(anchor.conflict_at) IS NULL
                        OR julianday(orders.signal_transport_at) IS NULL
                        OR julianday(anchor.conflict_at)<=
                           julianday(orders.signal_transport_at)
                  )""",
            (order_key,),
        ).fetchone()
        return row is not None

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

    def insert_map_order(
        self,
        order: ShadowOrder,
        map_number: int,
        *,
        strict_mapping_id: int,
    ) -> bool:
        """Atomically reserve a map and persist its only shadow order."""
        if isinstance(strict_mapping_id, bool) or strict_mapping_id <= 0:
            raise ValueError("strict_mapping_id must be a positive integer")
        if not self._signal_identity_matches(order):
            return False
        with self.transaction():
            conflicted = self.connection.execute(
                """SELECT 1 FROM vision_draft_anchors
                     WHERE raybet_match_id=? AND map_number=?
                       AND status='conflict'
                       AND (
                             conflict_at IS NULL
                             OR julianday(conflict_at) IS NULL
                             OR julianday(?) IS NULL
                             OR julianday(conflict_at)<=julianday(?)
                       )""",
                (
                    order.raybet_match_id,
                    map_number,
                    self._iso(order.signal_transport_at),
                    self._iso(order.signal_transport_at),
                ),
            ).fetchone()
            if conflicted is not None:
                return False
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

            existing = self.connection.execute(
                """SELECT * FROM settlement_reconciliations
                    WHERE raybet_match_id=? AND map_number=?""",
                (raybet_match_id, map_number),
            ).fetchone()
            linked_elsewhere = self.connection.execute(
                """SELECT raybet_match_id, map_number
                     FROM settlement_reconciliations
                    WHERE dota_match_id=?
                      AND (raybet_match_id!=? OR map_number!=?)""",
                (dota_match_id, raybet_match_id, map_number),
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
                    else "source_result_changed"
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

    def insert_settlement(
        self, order_key: str, result: str, return_units: float,
        settled_at: datetime, evidence_ref: str, review_required: bool = False,
    ) -> bool:
        with self.transaction():
            cursor = self.connection.execute(
                """INSERT OR IGNORE INTO settlements VALUES (?, ?, ?, ?, ?, ?)""",
                (order_key, result, return_units, settled_at.isoformat(), evidence_ref,
                 int(review_required)),
            )
            if cursor.rowcount != 1:
                return False
            if not review_required:
                order = self.connection.execute(
                    """SELECT raybet_match_id, market_key, fill_price
                         FROM shadow_orders WHERE order_key=?""",
                    (order_key,),
                ).fetchone()
                if order is not None:
                    from .notifications import EVENT_SETTLED, simulation_payload

                    self.enqueue_notification(
                        order_key=order_key,
                        event_type=EVENT_SETTLED,
                        payload=simulation_payload(
                            EVENT_SETTLED,
                            {
                                "raybet_match_id": str(order["raybet_match_id"]),
                                "result": result,
                                "return_units": return_units,
                                "fill_price": order["fill_price"],
                                "evidence_ref": evidence_ref,
                                "settled_at": settled_at,
                                "order_key": order_key,
                            },
                        ),
                        stats_cutoff_at=settled_at,
                        created_at=settled_at,
                    )
            return True
