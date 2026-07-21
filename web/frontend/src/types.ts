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

export interface FreshnessState {
  status: ReadinessStatus;
  observed_at?: string | null;
  age_seconds?: number | null;
  count?: number;
  total_count?: number;
  reasons?: string[];
  component?: string;
}

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
  source_frame_ref?: string;
  frame_digest?: string | null;
  frame_url?: string | null;
}

export interface StrategyDecision {
  decision_key?: string;
  observed_at?: string;
  decided_at: string;
  map_number: number;
  underdog_side?: string;
  market_probability: number;
  model_probability: number;
  edge: number;
  data_quality?: number;
  eligible: number;
  reason: string;
  strategy_version: string;
  input_ref?: string;
  contributions_json?: string;
  contributions?: Record<string, number>;
  conservative_contributions?: Record<string, number>;
  inputs?: Record<string, unknown>;
  draft_authority?: Record<string, unknown>;
  vision_authority?: Record<string, unknown>;
}

export type AnalysisSectionStatus =
  | "available"
  | "waiting"
  | "unavailable"
  | "review";

export interface AnalysisSection<T> {
  status: AnalysisSectionStatus;
  reason: string;
  data: T | null;
}

export interface OddsAnalysisData {
  point_count: number;
  periods: string[];
  latest_observed_at: string | null;
}

export interface VisionAnalysisData {
  map_number: number;
  captured_at: string;
  game_clock_seconds: number;
  clock_confidence: number;
  draft_confidence: number;
  source_frame_ref: string;
}

export interface StrategyAnalysisData {
  decisions: StrategyDecision[];
  displayed_count: number;
  scanned_count: number;
  count_scope: "recent_scanned_window";
  has_more: boolean;
  truncated: boolean;
  excluded_decision_count: number;
  /** Per-reason counts overlap; never sum them as a unique decision count. */
  excluded: {
    vision_invalidated: number;
    mapping_impacted: number;
    draft_conflicted: number;
    invalid_payload: number;
  };
}

export interface LineupHero {
  hero_id: number;
  hero_name?: string | null;
}

export interface LineupSide {
  hero_ids: number[];
  /** Optional future enrichment. Hero IDs remain the display authority. */
  heroes?: LineupHero[];
}

export interface LineupCurvePoint {
  landmark_key: string;
  horizon_minutes: number;
  radiant_probability: number;
  quality: number;
  support: number;
  uncertainty: number | null;
  model_version: string;
  validation_status: string;
  conditional: boolean;
  active: boolean;
}

export interface LineupCurveData {
  curve_key: string;
  first_usable_at: string;
  active_horizon_minutes: number;
  points: LineupCurvePoint[];
}

export interface LivePlayerIdentity {
  steam_account_id: number | null;
  side: "radiant" | "dire";
  position: number;
  hero_id: number;
  status: "resolved" | "selected_unresolved" | "unavailable";
}

export interface LivePlayerIdentityData {
  players: LivePlayerIdentity[];
}

export interface RoshLineupScoresData {
  pure_lineup_score: number;
  player_adjusted_lineup_score: number | null;
  effective_lineup_score: number;
  mode: "pure" | "player_adjusted";
  player_coverage: number;
  player_coverage_count: number;
  stake_multiplier: number;
  formula_version: string;
  source_as_of: string;
  score_key: string;
  player_identity_hash: string;
  evidence_hash: string;
  stake_cap: number;
}

export interface LineupAnalysisData {
  as_of: string;
  map_number: number;
  radiant_team_side: "team_one" | "team_two";
  radiant: LineupSide;
  dire: LineupSide;
  evidence: {
    draft_hash: string;
    anchor_source_frame_ref: string;
    anchored_at: string;
    strict_mapping_id: number;
  };
  scores: AnalysisSection<RoshLineupScoresData>;
  active_curve: AnalysisSection<LineupCurveData>;
  players: AnalysisSection<LivePlayerIdentityData>;
}

export interface MatchAnalysis {
  odds: AnalysisSection<OddsAnalysisData>;
  vision: AnalysisSection<VisionAnalysisData>;
  strategy: AnalysisSection<StrategyAnalysisData>;
  lineup: AnalysisSection<LineupAnalysisData>;
}

export interface WatchLink {
  kind: "public_stream" | "match_page" | "none";
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
  /** Deprecated compatibility field. It is never a playable-link authority. */
  live_url?: string | null;
  /** Optional so a new frontend fails closed against an older backend. */
  watch_link?: WatchLink;
  updated_at: string;
  latest_odds_activity_at?: string | null;
  lifecycle: Lifecycle;
  /**
   * The backend archive boundary is explicit.  A missing value is invalid
   * rather than an implicit ended/history signal.
   */
  history_eligible: boolean;
  winner: WinnerQuote | null;
  latest_vision: VisionPoint | null;
  latest_decision: StrategyDecision | null;
  readiness: Record<"odds" | "mapping" | "vision" | "model" | "strategy", FreshnessState>;
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
  winner_timeline: WinnerTimelinePoint[];
  decisions: StrategyDecision[];
  vision: VisionPoint[];
  markets: MarketQuote[];
  /** Optional for fail-closed compatibility with older monitor backends. */
  analysis?: MatchAnalysis;
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
  category: "operational" | "paper_signal";
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
  component:
    | "raybet_collector"
    | "shadow_monitor"
    | "vision_supervisor"
    | "draft_publisher"
    | "mail_worker";
  label: string;
  status: "running" | "stopped" | "identity_mismatch";
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
  component: string;
  action: "start" | "stop" | "restart";
  result: string;
  pid: number | null;
  detail: string | null;
  request_id: string;
  idempotent: boolean;
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

export interface MonitorSnapshot {
  generated_at: string;
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

export interface MonitorLifecycleCounts {
  total: number;
  upcoming: number;
  live: number;
  degraded: number;
  ended: number;
}

export type ConnectionState = "connecting" | "live" | "fallback" | "offline";

export type IntelligenceStateLabel =
  | "comeback"
  | "throw"
  | "stomp"
  | "stomp_loss"
  | "advantage"
  | "disadvantage"
  | "even"
  | "state_unscorable";

export type IntelligenceAvailabilityMode =
  | "reconstructed_walk_forward"
  | "prospective";

export interface IntelligencePagination {
  page: number;
  page_size: number;
  total: number;
  total_pages: number;
}

export interface IntelligenceTeamState {
  match_id?: number;
  team_id: number | null;
  side: "radiant" | "dire";
  label: IntelligenceStateLabel;
  duration_seconds: number | null;
  max_lead: number | null;
  max_deficit: number | null;
  ahead_fraction?: number | null;
  behind_fraction?: number | null;
  even_fraction?: number | null;
  signed_auc?: number | null;
  absolute_auc?: number | null;
  crossings?: unknown[];
  first_significant_lead_at?: number | null;
  first_significant_deficit_at?: number | null;
  closeout_seconds?: number | null;
  objective_conversion?: Record<string, unknown>;
  curve_coverage: number;
  source_versions?: Record<string, unknown>;
  label_version: string;
  created_at?: string;
}

export interface IntelligenceMatchSummary {
  match_id: number;
  radiant_team_id: number | null;
  dire_team_id: number | null;
  radiant_team_name: string | null;
  dire_team_name: string | null;
  radiant_win: boolean | null;
  duration: number | null;
  start_time: number | null;
  leagueid: number | null;
  league_name: string | null;
  radiant_score: number | null;
  dire_score: number | null;
  radiant_state?: IntelligenceTeamState | null;
  dire_state?: IntelligenceTeamState | null;
}

export interface IntelligencePlayerPerformance {
  player_slot: number;
  account_id: number | null;
  player_name: string | null;
  team_id: number | null;
  side: "radiant" | "dire" | null;
  hero_id: number | null;
  hero_name: string | null;
  performance?: {
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
    lane_efficiency: number | null;
    kda: number | null;
  } | null;
}

export interface IntelligencePlayerMapScore extends IntelligencePlayerPerformance {
  position: number | null;
  execution_score: number | null;
  result_adjusted_score: number | null;
  coverage: number | null;
  role_confidence: number | null;
  ranking_eligible: boolean;
  benchmark_cutoff: string | null;
  score_version: string;
  component_facts: unknown[] | Record<string, unknown>;
  component_scores: unknown[] | Record<string, unknown>;
  weights: unknown[] | Record<string, unknown>;
  explanation: Record<string, unknown>;
}

export interface IntelligenceMatchRatingValues {
  execution_score: number;
  result_adjusted_score: number;
  coverage: number;
}

export interface IntelligenceMatchRating {
  rating_version: string;
  rounding: "decimal-half-up-2dp";
  source_score_version: string;
  benchmark_cutoff: string;
  player_count: 10;
  overall: IntelligenceMatchRatingValues;
  radiant: IntelligenceMatchRatingValues;
  dire: IntelligenceMatchRatingValues;
}

export interface IntelligenceDraftPrediction {
  model_version: string;
  model_kind: "pure_draft" | "context_adjusted";
  horizon_minutes: number;
  availability_mode: IntelligenceAvailabilityMode;
  assignment_version: string;
  score_version: string;
  training_cutoff: string;
  model_status: string;
  prediction_cutoff: string;
  cutoff_source: string;
  probability: number | null;
  uncertainty: number | null;
  support: number;
  eventual_radiant_win: number | null;
  status: "predicted" | "insufficient_evidence" | "settled";
}

export interface IntelligenceFlatMatchDetail extends IntelligenceMatchSummary {
  states?: {
    radiant: IntelligenceTeamState | null;
    dire: IntelligenceTeamState | null;
  };
  player_performance?: IntelligencePlayerPerformance[];
  player_scores: IntelligencePlayerMapScore[];
  match_rating: IntelligenceMatchRating | null;
  draft_predictions: IntelligenceDraftPrediction[];
}

export interface IntelligenceNestedMatchDetail {
  match: IntelligenceMatchSummary;
  radiant_state: IntelligenceTeamState | null;
  dire_state: IntelligenceTeamState | null;
  player_performance?: IntelligencePlayerPerformance[];
  player_scores: IntelligencePlayerMapScore[];
  match_rating: IntelligenceMatchRating | null;
  draft_predictions: IntelligenceDraftPrediction[];
}

export type IntelligenceMatchDetail =
  | IntelligenceFlatMatchDetail
  | IntelligenceNestedMatchDetail;

export type ExactPostmatchStatus = "available" | "unavailable" | "review";

export interface ExactPostmatchEvent {
  game_time_seconds: number;
  event_type: "economy" | "objective" | "teamfight" | "buyback";
  side: "radiant" | "dire" | null;
  label: string;
  radiant_gold_adv: number | null;
  radiant_xp_adv: number | null;
  team_one_probability: number | null;
  team_two_probability: number | null;
  details: Record<string, unknown>;
}

export interface ExactPostmatchPayload {
  match: IntelligenceMatchSummary;
  states: {
    radiant: IntelligenceTeamState | null;
    dire: IntelligenceTeamState | null;
  };
  player_performance: IntelligencePlayerPerformance[];
  player_scores: IntelligencePlayerMapScore[];
  events: ExactPostmatchEvent[];
  event_availability: {
    gold_advantage: boolean;
    xp_advantage: boolean;
    objectives: boolean;
    teamfights: boolean;
    buybacks: boolean;
    odds_game_clock_alignment: boolean;
    missing_reasons: string[];
  };
}

export interface ExactPostmatchMappingTeam {
  side: "team_one" | "team_two";
  team_id: number;
  team_name: string;
}

export interface ExactPostmatchMapping {
  mapping_id: number;
  event_id: string;
  acceptance_mode: string;
  mapping_version: string;
  canonical_teams: ExactPostmatchMappingTeam[];
  accepted_at: string;
  recorded_at: string;
}

export interface ExactPostmatchAttribution {
  raybet_match_id: string;
  map_number: number;
  checked_at: string;
  status: ExactPostmatchStatus;
  reason: string;
  mapping: ExactPostmatchMapping | null;
  reconciliation: Record<string, unknown> | null;
  odds_timeline: WinnerTimelinePoint[];
  postmatch: ExactPostmatchPayload | null;
  warnings?: string[];
}

export interface IntelligenceDraftQualitySlice {
  model_kind: "pure_draft" | "context_adjusted";
  horizon_minutes: number;
  availability_mode: IntelligenceAvailabilityMode;
  assignment_version: string;
  score_version: string;
  availability_status: "available" | "missing";
  is_reconstructed: boolean;
  support: number;
  eligible_targets: number;
  predicted: number;
  insufficient_evidence: number;
  brier_score: number | null;
  log_loss: number | null;
  ece_5_bin: number | null;
  ece_90_upper: number | null;
  status: "passed" | "failed" | "unsupported" | "missing" | "provisional";
  gate_failures: string[];
}

export interface IntelligenceOverview {
  versions: {
    player_score: string;
    team_state: string;
    team_profile: string;
    draft_score: string;
    draft_model: string;
    draft_backtest: string;
    draft_features: string;
  };
  coverage: Record<string, number>;
  team_state_distribution: Partial<Record<IntelligenceStateLabel, number>>;
  draft_cohorts?: Array<{
    availability_mode: IntelligenceAvailabilityMode;
    assignment_version: string;
    score_version: string;
  }>;
  draft_quality_slices?: IntelligenceDraftQualitySlice[];
  draft_quality?:
    | IntelligenceDraftQualitySlice[]
    | {
        slices: IntelligenceDraftQualitySlice[];
        availability?: Partial<Record<IntelligenceAvailabilityMode, boolean>>;
      };
  availability?: Partial<Record<IntelligenceAvailabilityMode, boolean>>;
}

export interface IntelligencePlayerRanking {
  rank: number;
  account_id: number;
  player_name: string | null;
  position: number;
  map_count: number;
  average_execution_score: number;
  average_result_adjusted_score: number;
  average_coverage: number;
  average_role_confidence: number;
  score_version: string;
  /** Legacy/detail payloads may expose one cutoff per row. */
  benchmark_cutoff?: string | null;
  /** Aggregated rankings expose all cutoffs contributing to the row. */
  benchmark_cutoffs?: string[];
  benchmark_cutoff_min?: string | null;
  benchmark_cutoff_max?: string | null;
}

export interface IntelligenceTeamProfile {
  team_id: number;
  team_name: string | null;
  team_tag: string | null;
  logo_url: string | null;
  profile_cutoff: string;
  profile_version: string;
  opportunity_counts: unknown[] | Record<string, unknown>;
  posterior_rates: unknown[] | Record<string, unknown>;
  duration_quantiles: unknown[] | Record<string, unknown>;
  weighting: Record<string, unknown>;
  effective_sample_size: number;
  created_at: string;
  state_counts: Partial<Record<IntelligenceStateLabel, number>>;
}

export interface IntelligenceMatchPage {
  data: IntelligenceMatchSummary[];
  pagination: IntelligencePagination;
}

export interface IntelligencePlayerPage {
  data: IntelligencePlayerRanking[];
  pagination: IntelligencePagination;
}

export interface IntelligenceTeamPage {
  data: IntelligenceTeamProfile[];
  pagination?: IntelligencePagination;
}
