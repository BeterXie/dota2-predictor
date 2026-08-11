export type Lifecycle = "upcoming" | "live" | "degraded" | "ended";

export type ReadinessStatus =
  | "ready"
  | "delayed"
  | "stale"
  | "missing"
  | "invalid"
  | "unconfirmed"
  | "degraded"
  | "unhealthy"
  | "stopped";

export interface WinnerQuote {
  observed_at: string;
  period?: string;
  complete: boolean;
  prices?: Record<"team_one" | "team_two", number>;
  probabilities?: Record<"team_one" | "team_two", number>;
}

export interface VisionPoint {
  captured_at: string;
  observed_at?: string;
  map_number: number | null;
  game_clock_seconds: number | null;
  is_paused?: number | null;
  screen_state: string;
  confirmed: number;
  clock_confidence: number;
  draft_confidence: number;
  radiant_hero_ids?: number[];
  dire_hero_ids?: number[];
  source_frame_ref?: string;
  frame_digest?: string | null;
  frame_url?: string | null;
}

export interface LiveDraftSlot {
  team_id: number;
  side: "radiant" | "dire";
  position: number;
  hero_id: number;
  player_id: number | null;
}

export interface LiveDraftMapping {
  raybet_match_id: string;
  map_number: number;
  version: number;
  source: "manual" | "manual_correction";
  is_locked: boolean;
  created_by: string;
  actor?: string;
  evidence_source_url?: string | null;
  authority_version?: "sourced-manual-draft-v1" | null;
  created_at: string;
  slots: LiveDraftSlot[];
  prediction_automation?: LiveDraftPredictionResponse;
  decision_checkpoint?: MapDecisionCheckpoint | null;
  decision_checkpoint_status?: "available" | "blocked";
  decision_checkpoint_missing_reason?: string | null;
}

export interface LiveDraftProspectivePrediction {
  prediction_hash: string;
  version: "live-draft-prospective-bridge-v1";
  identity: {
    raybet_match_id: string;
    map_number: number;
    mapping_version: number;
    mapping_hash: string;
  };
  operator_locked_at: string;
  confirmed_at: string;
  record_status: "paired" | "p0_only";
  p0_probability: number;
  p1_probability: number | null;
  pure_rosh_score: number | null;
  standardized_rosh_score: number | null;
  rosh_logit_contribution: number | null;
  missing_reason: string | null;
  candidate_hash: string;
  causal_evidence: {
    game_clock_seconds: number | null;
    vision_frame_timestamp: string | null;
    draft_state_marker: string | null;
    live_state_input_used: boolean;
    causal_status: "eligible" | "unverified" | "ineligible";
    causal_reason: string | null;
  };
  created_at: string;
}

export interface MapDecisionCheckpoint {
  checkpoint_id: number;
  raybet_match_id: string;
  map_number: number;
  mapping_version: number | null;
  phase: "pregame" | "live";
  checkpoint_minute: number;
  strategy_version: "map-decision-shadow-v1";
  decision: "bet_team_a" | "bet_team_b" | "skip";
  assumed_stake_units: 1;
  observed_price: number | null;
  model_probability_team_one: number | null;
  model_probability_team_two: number | null;
  market_probability_team_one: number | null;
  market_probability_team_two: number | null;
  selected_edge: number | null;
  odds_observation_key: string | null;
  odds_group_id: string | null;
  odds_observed_at: string | null;
  odds_age_seconds: number | null;
  odds_max_age_seconds: number;
  vision_snapshot_id: number | null;
  vision_source_frame_ref: string | null;
  vision_captured_at: string | null;
  vision_game_time_seconds: number | null;
  vision_networth_lead: number | null;
  vision_radiant_kills: number | null;
  vision_dire_kills: number | null;
  vision_age_seconds: number | null;
  vision_max_age_seconds: number | null;
  odds_vision_gap_seconds: number | null;
  odds_vision_gap_max_seconds: number | null;
  vision_trusted: boolean;
  vision_replay: false;
  input_versions: Record<string, unknown>;
  feature_availability: Record<string, unknown>;
  reason: string;
  decided_at: string;
  created_at: string;
  evaluation_eligible: boolean;
  evaluation_exclusion_reason: string | null;
  settlement: {
    settlement_id: number;
    dota_match_id: number;
    winner_side: "team_one" | "team_two";
    outcome: "win" | "loss" | "skip";
    gross_return_units: number;
    profit_units: number;
    result_source: "confirmed_map_result";
    result_recorded_at: string;
    settled_at: string;
  } | null;
}

export interface LiveDraftPredictionResponse {
  status: "available" | "not_found" | "blocked" | "created" | "unchanged";
  prediction: LiveDraftProspectivePrediction | null;
  missing_reason?: string | null;
  decision_checkpoint?: MapDecisionCheckpoint;
  decision_checkpoints?: MapDecisionCheckpoint[];
}

export interface LiveDraftContextTeam {
  match_side: "team_one" | "team_two";
  team_id: number;
  team_name: string;
}

export interface LiveDraftContext {
  status: "ready" | "unavailable";
  reason: string;
  source: string;
  teams: LiveDraftContextTeam[];
}

export interface CanonicalTeam {
  team_id: number;
  team_name: string;
  tag: string | null;
}

export interface LiveGameSnapshot {
  snapshot_id: number;
  raybet_match_id: string;
  map_number: number;
  game_time_seconds: number;
  radiant_networth: number;
  dire_networth: number;
  networth_lead: number;
  radiant_kills: number | null;
  dire_kills: number | null;
  vision_confidence: number;
  screenshot_path: string | null;
  source: "vision" | "manual_correction";
  captured_at: string;
  created_by: string | null;
  created_at: string;
}

export interface LiveHudObservation {
  status: "available" | "unavailable";
  source: "vision_hud" | null;
  observation_file: string;
  captured_at: string;
  map_number: number | null;
  game_clock_seconds: number | null;
  is_paused: boolean | null;
  screen_state: string;
  clock_confidence: number;
  draft_confidence: number;
  hud_confidence: number;
  draft_confirmed: boolean;
  radiant_hero_count: number;
  dire_hero_count: number;
  radiant_hero_ids: number[];
  dire_hero_ids: number[];
  radiant_kills: number | null;
  dire_kills: number | null;
  radiant_net_worth: number | null;
  dire_net_worth: number | null;
  net_worth_advantage_side: "radiant" | "dire" | null;
  net_worth_advantage_min: number | null;
  net_worth_advantage_max: number | null;
  unavailable_reason: string | null;
}

export interface VisionRuntimeStatus {
  worker_status: string;
  freshness: "fresh" | "delayed" | "stale" | "missing";
  observed_at: string | null;
  map_number: number | null;
  capture_state?: string | null;
  reason?: string | null;
  blocker_code?: string | null;
  replay_gate_status?: string | null;
  screen_state?: string | null;
}

export interface WatchLink {
  kind: "public_stream" | "stream_resolver" | "match_page" | "none";
  availability: "available" | "unavailable";
  url: string | null;
  reason: string;
}

export interface FreshnessReadiness {
  status: ReadinessStatus;
  observed_at: string | null;
  age_seconds: number | null;
}

export interface MatchReadiness {
  odds: FreshnessReadiness;
  mapping: {
    status: ReadinessStatus;
    count: number;
    total_count: number;
    reasons: string[];
  };
  vision: FreshnessReadiness & {
    reason?: "waiting_for_watch_window" | "stream_probe_pending";
    watch_starts_at?: string | null;
  };
}

export interface MonitorMatch {
  raybet_match_id: string;
  display_name?: string | null;
  observation_file?: string | null;
  tournament: string;
  team_one: string;
  team_two: string;
  scheduled_at: string | null;
  best_of: number | null;
  provider_status: string;
  live_url?: string | null;
  watch_link?: WatchLink;
  updated_at: string;
  latest_odds_activity_at?: string | null;
  lifecycle: Lifecycle;
  history_eligible: boolean;
  current_map_number?: number | null;
  winner: WinnerQuote | null;
  prematch_winner?: WinnerQuote | null;
  latest_vision: VisionPoint | null;
  readiness?: MatchReadiness;
}

export interface WinnerTimelinePoint {
  observed_at: string;
  period: string;
  prices: Record<"team_one" | "team_two", number>;
  probabilities: Record<"team_one" | "team_two", number>;
  status: Record<"team_one" | "team_two", string>;
  game_clock_seconds?: number | null;
  map_number?: number | null;
}

export interface OddsCoveragePhase {
  status: "available" | "missing" | "pending";
  complete_snapshot_count: number;
  observation_count: number;
  first_observed_at: string | null;
  last_observed_at: string | null;
  gap_count: number;
  longest_gap_seconds: number | null;
  periods: Array<{
    period: string;
    complete_snapshot_count: number;
    observation_count: number;
    first_observed_at: string;
    last_observed_at: string;
    gap_count: number;
    longest_gap_seconds: number | null;
  }>;
}

export interface OddsCoverageSummary {
  source: "raybet_direct";
  gap_threshold_seconds: number;
  prematch: OddsCoveragePhase;
  live: OddsCoveragePhase;
  closing: {
    status: "available" | "missing" | "pending" | "unconfirmed";
    observed_at: string | null;
    prices: Record<"team_one" | "team_two", number> | null;
    probabilities: Record<"team_one" | "team_two", number> | null;
  };
}

export interface MarketQuote {
  odds_id: string;
  odds_group_id: string | null;
  received_at: string;
  price: number;
  status: string;
  market_type: string;
  period: string;
  side: string | null;
  line: number | null;
  outcome_key: string;
  supported: number;
}

export type PostmatchAvailability = "available" | "partial" | "missing";

export interface PostmatchHistoricalAverage {
  sample_size: number;
  source: "opendota_collected_history";
  cutoff: "before_match_start";
  sample_start_date: string;
  sample_end_date: string;
  kills: number | null;
  deaths: number | null;
  assists: number | null;
  gold_per_min: number | null;
  xp_per_min: number | null;
  net_worth: number | null;
  last_hits: number | null;
  hero_damage: number | null;
  tower_damage: number | null;
}

export interface PostmatchPlayer {
  player_slot: number;
  account_id: number | null;
  player_name: string | null;
  player_name_source: "opendota_name" | "opendota_personaname" | null;
  side: "radiant" | "dire";
  team_id: number | null;
  hero_id: number;
  hero_name: string;
  hero_key: string;
  kills: number | null;
  deaths: number | null;
  assists: number | null;
  gold_per_min: number | null;
  xp_per_min: number | null;
  net_worth: number | null;
  last_hits: number | null;
  denies: number | null;
  hero_damage: number | null;
  hero_healing: number | null;
  tower_damage: number | null;
  level: number | null;
  position: number | null;
  position_source: "stratz" | null;
  historical_average: PostmatchHistoricalAverage | null;
  items: number[];
}

export interface PostmatchDraftAction {
  order: number;
  is_pick: boolean;
  side: "radiant" | "dire";
  hero_id: number;
  hero_name: string;
  hero_key: string;
}

export interface PostmatchGame {
  map_number: number;
  official_match_id: string;
  identity_reason: "confirmed_map_result" | "raybet_explicit_map_time_unique";
  identity_evidence: {
    method: "confirmed_settlement_reconciliation" | "raybet_explicit_map_time_unique";
    official_source: "confirmed_map_result" | "registered_opendota_match";
    raybet_source?: string;
    raybet_map_time?: string;
    official_start_time?: string;
    delta_seconds?: number;
    maximum_delta_seconds?: number;
    official_series_id?: number;
    league_id?: number;
  };
  status: "available" | "linked_not_ingested";
  source: "opendota";
  enrichment: {
    provider: "stratz";
    status: "available" | "partial" | "not_available" | "invalid" | "blocked";
    reason: string;
    observed_at: string | null;
  };
  fetched_at: string | null;
  result: {
    radiant_team_id: number | null;
    dire_team_id: number | null;
    radiant_team_name: string | null;
    dire_team_name: string | null;
    radiant_win: boolean;
    duration_seconds: number;
    start_time: number | null;
    league_id: number | null;
    league_name: string | null;
    radiant_score: number | null;
    dire_score: number | null;
  } | null;
  players: PostmatchPlayer[];
  draft: PostmatchDraftAction[];
  advantages: {
    gold: Array<{ minute: number; value: number }>;
    xp: Array<{ minute: number; value: number }>;
  };
  objectives: Array<{
    time_seconds: number | null;
    type: string;
    unit: string;
    key: string;
    player_slot: number | null;
  }>;
  teamfights: Array<{
    start_time: number | null;
    end_time: number | null;
    last_death: number | null;
    deaths: number | null;
    kills: number;
    damage: number;
    healing: number;
    gold_delta: number;
    xp_delta: number;
  }>;
  availability: Record<
    | "result"
    | "players"
    | "player_names"
    | "historical_averages"
    | "positions"
    | "draft"
    | "gold_advantage"
    | "xp_advantage"
    | "objectives"
    | "teamfights",
    PostmatchAvailability
  >;
}

export interface PostmatchDetail {
  status: "available" | "partial" | "waiting" | "review";
  reason: string;
  identity_source?: "map_results" | "raybet_explicit_map_time" | "waiting";
  sources: {
    canonical: {
      provider: "opendota";
      role: "canonical_postmatch";
      status: string;
      reason: string;
    };
    enhancement: {
      provider: "stratz";
      role: "optional_enrichment";
      status: string;
      reason: string;
    };
  };
  games: PostmatchGame[];
  unresolved_maps: Array<{
    map_number: number;
    status: string;
    reason: string;
    official_match_id: string | null;
    updated_at: string | null;
  }>;
}

export type MatchGameState = "scheduled" | "live" | "ended" | "unconfirmed";

export interface MatchGameDetail {
  game_id: string;
  map_number: number;
  period: string;
  official_match_id: string | null;
  link_status: "confirmed" | "unlinked";
  link_reason: string;
  play_evidence: Array<
    | "locked_draft_mapping"
    | "verified_game_frame"
    | "trusted_game_snapshot"
    | "raybet_final_market"
    | "official_map_result"
    | "provider_live_map"
  >;
  state: MatchGameState;
  winner: WinnerQuote | null;
  prematch_winner?: WinnerQuote | null;
  winner_timeline: WinnerTimelinePoint[];
  odds_coverage: OddsCoverageSummary;
  vision: VisionPoint[];
  latest_vision: VisionPoint | null;
  latest_capture: VisionPoint | null;
  draft_mapping: LiveDraftMapping | null;
  game_snapshots: LiveGameSnapshot[];
  latest_game_snapshot: LiveGameSnapshot | null;
  latest_hud_observation: LiveHudObservation | null;
  vision_runtime: VisionRuntimeStatus | null;
  markets: MarketQuote[];
  postmatch: PostmatchDetail;
  decision_checkpoints: MapDecisionCheckpoint[];
}

export interface MarketOnlyMapEvidence {
  market_id: string;
  map_number: number;
  period: string;
  status: "market_only";
  reason: "no_play_evidence";
  prematch_winner: WinnerQuote | null;
  winner_timeline: WinnerTimelinePoint[];
  odds_coverage: OddsCoverageSummary;
  markets: MarketQuote[];
}

export interface MatchDetail extends MonitorMatch {
  draft_context: LiveDraftContext | null;
  postmatch: PostmatchDetail;
  games: MatchGameDetail[];
  market_evidence: MarketOnlyMapEvidence[];
}

export type GameWorkspaceDetail = MatchDetail & MatchGameDetail;

export interface HealthItem {
  component: string;
  status: ReadinessStatus | "healthy" | "starting";
  reported_status: string | null;
  freshness: "fresh" | "delayed" | "stale" | "missing";
  age_seconds: number | null;
  last_heartbeat_at: string | null;
  last_success_at: string | null;
  last_error_at: string | null;
  last_error: string | null;
  details: Record<string, unknown>;
}

export interface AlertIncident {
  incident_id: number;
  dedupe_key: string;
  episode: number;
  category: "operational";
  severity: "warning" | "critical";
  title: string;
  body: string;
  first_detected_at: string;
  opened_at: string;
  last_detected_at: string;
  acknowledged_at: string | null;
  acknowledged_by: string | null;
  source: Record<string, unknown>;
  occurrence_count: number;
}

export interface ControlComponent {
  component: "raybet_collector" | "vision_supervisor";
  label: string;
  status: "running" | "stopped" | "identity_mismatch" | "identity_unverifiable";
  pid: number | null;
  started_at: string | null;
  detail: string | null;
  control_allowed: boolean;
}

export interface ControlSession {
  csrf_token: string;
  expires_in: number;
  client_host: string;
  components: ControlComponent[];
}

export interface ControlResult {
  ok: boolean;
  component: ControlComponent["component"];
  action: "start" | "stop" | "restart";
  status: string;
  pid: number | null;
  detail: string | null;
}

export interface MappingRecord {
  mapping_id: number;
  map_number: number;
  event_id: string;
  raybet_team_ids: [number, number];
  canonical_teams: [{ id: number; name: string }, { id: number; name: string }];
  acceptance_mode: "manual_exact" | "automatic_exact";
  automatic_approval_id: number | null;
  accepted_by: string;
  accepted_at: string;
  recorded_at: string;
  evidence: Record<string, unknown>;
  evidence_hash: string;
  invalidation: {
    invalidation_id: number;
    reason: string;
    invalidated_by: string;
    invalidated_at: string;
  } | null;
  evidence_approval_id: number | null;
}

export interface MonitorCapability {
  required: boolean;
  status: string;
}

export interface MonitorLifecycleCounts {
  total: number;
  live: number;
  upcoming: number;
  degraded: number;
  ended: number;
}

export interface MonitorSnapshot {
  generated_at: string;
  market_source_policy?: string;
  capabilities?: Record<string, MonitorCapability>;
  cursor: string;
  mapping_revision: string;
  health: HealthItem[];
  matches: MonitorMatch[];
  alerts: AlertIncident[];
  summary: MonitorLifecycleCounts & {
    live_view: MonitorLifecycleCounts;
    history_view: MonitorLifecycleCounts;
    unhealthy_components: number;
    active_alerts: number;
  };
}

export interface MonitorHistoryPage {
  items: MonitorMatch[];
  next_cursor: string | null;
  has_more: boolean;
}

export interface PrematchHero {
  hero_id: number;
  localized_name: string;
  hero_key: string;
  image_url: string;
}

export type PrematchHeroGrid = Record<"str" | "agi" | "int" | "all", PrematchHero[]>;

export interface VisionCalibrationLabel {
  label_id: string;
  event_id: string;
  event_relative_path: string;
  layout: string | null;
  profile_id: string;
  hero_ids: number[];
  raybet_match_id: string | null;
  map_number: number | null;
  note: string | null;
  updated_at: string;
}

export interface VisionSlotDiagnostic {
  side: "radiant" | "dire";
  slot: number;
  accepted: boolean;
  best_hero_id: number | null;
  best_score: number;
  margin: number;
  reason: string;
}

export interface VisionCalibrationEvent {
  event_id: string;
  relative_path: string;
  captured_at: string;
  layout: string | null;
  profile_id: string;
  reason: string;
  blocker_code: string | null;
  screen_state: string | null;
  replay_gate_status: string | null;
  layout_state: string | null;
  quality_reason: string | null;
  quality_usable: boolean | null;
  crop_count: number;
  frame_url: string;
  crop_urls: string[];
  slot_diagnostics: VisionSlotDiagnostic[];
  label: VisionCalibrationLabel | null;
}

export interface VisionCalibrationCandidate {
  candidate_id: string;
  label_id: string;
  layout: string | null;
  profile_id: string;
  hero_ids: number[];
  added_variant_count: number;
  base_candidate_id?: string | null;
  base_feature_sha256: string;
  created_at: string;
  feature_sha256: string;
  production_feature_sha256: string;
  promoted: boolean;
}

export interface VisionCalibrationEvaluation {
  evaluation_id: string;
  label_id: string;
  candidate_id: string;
  observation_file: string;
  raybet_match_id?: string;
  map_number?: number;
  layout_profile: string;
  mode: "perception" | "runtime";
  created_at: string;
  total_files: number;
  trackable_frames: number;
  best_candidate_accuracy: number;
  accepted_precision: number;
  final_locked_slots: number;
  final_correct_locked_slots: number;
  wrong_lock_count: number;
  lock_latency_seconds: number | null;
  exact_post_lock_rate: number;
  candidate_feature_sha256: string;
}

export interface VisionMatchSummary {
  match_id: string;
  observation_file: string | null;
  status: string;
  status_label: string;
  phase: string;
  observation_count: number;
  evidence_frame_count: number;
  manifest_event_count: number;
  periodic_count: number;
  draft_started: boolean;
  game_started: boolean;
  ended_final: boolean;
  first_captured_at: string | null;
  last_captured_at: string | null;
  latest_screen_state: string | null;
  layout_profile: string | null;
  maps: number[];
  capture_status: string | null;
  heartbeat_fresh: boolean;
  raybet_match_id?: string;
  official_match_id?: string | null;
  display_name?: string;
}

export interface VisionCalibrationBootstrap {
  events: VisionCalibrationEvent[];
  profiles: VisionCalibrationProfile[];
  candidates: VisionCalibrationCandidate[];
  evaluations: VisionCalibrationEvaluation[];
  match_summaries: VisionMatchSummary[];
  observation_files: Array<{
    name: string;
    bytes: number;
    raybet_match_id?: string;
    official_match_id?: string | null;
    display_name?: string;
  }>;
  observation_root: string;
  layout_profiles: string[];
  production_feature_path: string;
  candidate_boundary: string;
}

export interface VisionCalibrationProfile {
  profile_id: string;
  layout: string | null;
  event_count: number;
  labeled_event_count: number;
  candidate_count: number;
  latest_captured_at: string | null;
}
