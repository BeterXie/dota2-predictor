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
}

export interface MonitorMatch {
  raybet_match_id: string;
  tournament: string;
  team_one: string;
  team_two: string;
  scheduled_at: string | null;
  best_of: number | null;
  provider_status: string;
  live_url: string | null;
  updated_at: string;
  lifecycle: Lifecycle;
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

export interface MonitorSnapshot {
  generated_at: string;
  cursor: string;
  health: HealthItem[];
  matches: MonitorMatch[];
  summary: {
    total: number;
    upcoming: number;
    live: number;
    degraded: number;
    ended: number;
    unhealthy_components: number;
  };
}

export type ConnectionState = "connecting" | "live" | "fallback" | "offline";
