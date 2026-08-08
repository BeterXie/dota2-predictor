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
  created_at: string;
  slots: LiveDraftSlot[];
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

export interface LiveDraftPredictionResponse {
  status: "available" | "not_found" | "blocked" | "created" | "unchanged";
  prediction: LiveDraftProspectivePrediction | null;
  missing_reason?: string | null;
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

export interface WatchLink {
  kind: "public_stream" | "stream_resolver" | "match_page" | "none";
  availability: "available" | "unavailable";
  url: string | null;
  reason: string;
}

export interface MonitorMatch {
  raybet_match_id: string;
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
  latest_vision: VisionPoint | null;
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

export interface MatchDetail extends MonitorMatch {
  prematch_winner?: WinnerQuote | null;
  winner_timeline: WinnerTimelinePoint[];
  vision: VisionPoint[];
  latest_capture?: VisionPoint | null;
  draft_mapping?: LiveDraftMapping | null;
  draft_context?: LiveDraftContext | null;
  game_snapshots?: LiveGameSnapshot[];
  latest_game_snapshot?: LiveGameSnapshot | null;
  markets: MarketQuote[];
}

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
  created_at: string;
  feature_sha256: string;
  production_feature_sha256: string;
  promoted: false;
}

export interface VisionCalibrationEvaluation {
  evaluation_id: string;
  label_id: string;
  candidate_id: string;
  observation_file: string;
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

export interface VisionCalibrationBootstrap {
  events: VisionCalibrationEvent[];
  profiles: VisionCalibrationProfile[];
  candidates: VisionCalibrationCandidate[];
  evaluations: VisionCalibrationEvaluation[];
  observation_files: Array<{ name: string; bytes: number }>;
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
