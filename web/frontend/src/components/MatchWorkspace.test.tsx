import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import goldenAvailableAnalysis from "../../../../tests/fixtures/monitor-analysis-available.json";
import goldenNoSignalAnalysis from "../../../../tests/fixtures/monitor-analysis-no-signal.json";
import { formatDateTime } from "../format";
import type {
  AnalysisSection,
  AnalysisSectionStatus,
  LineupAnalysisData,
  LivePlayerIdentityData,
  MatchAnalysis,
  MatchDetail,
  MonitorMatch,
  LiveDraftProspectivePrediction,
  RoshLineupScoresData,
  StrategyAnalysisData,
  StrategyDecision,
  VisionAnalysisData,
} from "../types";

const createLiveDraftPredictionMock = vi.hoisted(() => vi.fn());
const fetchLiveDraftPredictionMock = vi.hoisted(() => vi.fn());

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    createLiveDraftPrediction: createLiveDraftPredictionMock,
    fetchLiveDraftPrediction: fetchLiveDraftPredictionMock,
  };
});
vi.mock("@fluentui/react-components", () => ({
  Badge: ({ children }: { children: ReactNode }) => <span>{children}</span>,
  Button: ({
    "aria-label": ariaLabel,
    as,
    children,
    disabled,
    href,
    onClick,
    type,
  }: {
    "aria-label"?: string;
    as?: string;
    children: ReactNode;
    disabled?: boolean;
    href?: string;
    onClick?: () => void;
    type?: "button" | "submit" | "reset";
  }) => as === "a"
    ? <a aria-label={ariaLabel} href={href}>{children}</a>
    : (
      <button
        aria-label={ariaLabel}
        disabled={disabled}
        onClick={onClick}
        type={type}
      >
        {children}
      </button>
    ),
  Dialog: ({ children }: { children: ReactNode }) => <>{children}</>,
  DialogBody: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DialogContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DialogSurface: ({ children }: { children: ReactNode }) => <div role="dialog">{children}</div>,
  DialogTitle: ({ action, children }: { action?: ReactNode; children: ReactNode }) => (
    <header><h2>{children}</h2>{action}</header>
  ),
  Skeleton: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SkeletonItem: () => <div />,
}));
vi.mock("./ProbabilityChart", () => ({
  ProbabilityChart: ({
    onPeriodChange,
    selectedPeriod,
  }: {
    onPeriodChange: (value: string) => void;
    selectedPeriod: string | null;
  }) => (
    <button
      data-selected-period={selectedPeriod}
      onClick={() => onPeriodChange("map_1")}
    >
      probability-chart
    </button>
  ),
}));
vi.mock("./PostmatchIntelligencePanel", () => ({
  PostmatchIntelligencePanel: ({ mapNumber }: { mapNumber: number | null }) => (
    <div>postmatch-map-{mapNumber ?? "none"}</div>
  ),
}));

import { MatchWorkspace } from "./MatchWorkspace";

const FRAME_REF = `vision-frame:sha256:${"a".repeat(64)}`;
const DECISION_KEY = "b".repeat(32);
const INPUT_REF = "c".repeat(24);
const CURVE_KEY = "d".repeat(64);
const LANDMARK_10 = "e".repeat(64);
const LANDMARK_20 = "f".repeat(64);
const ROSH_SCORE_KEY = "1".repeat(64);
const ROSH_EVIDENCE_HASH = "2".repeat(64);
const ROSH_PLAYER_IDENTITY_HASH = "3".repeat(64);

const match: MonitorMatch = {
  raybet_match_id: "match-1",
  tournament: "Test event",
  team_one: "Radiant",
  team_two: "Dire",
  scheduled_at: "2026-07-16T12:00:00+00:00",
  best_of: 3,
  provider_status: "2",
  live_url: null,
  updated_at: "2026-07-16T12:00:00+00:00",
  lifecycle: "live",
  history_eligible: false,
  winner: null,
  latest_vision: null,
  latest_decision: null,
  readiness: {
    odds: { status: "ready" },
    mapping: { status: "ready" },
    vision: { status: "ready" },
    model: { status: "missing" },
    strategy: { status: "ready" },
  },
};

function detail(
  confirmed: number,
  visionStatus: MonitorMatch["readiness"]["vision"]["status"],
): MatchDetail {
  return {
    ...match,
    latest_vision: {
      captured_at: "2026-07-16T12:00:00+00:00",
      observed_at: "2026-07-16T12:00:00+00:00",
      map_number: 1,
      game_clock_seconds: 120,
      screen_state: "game",
      confirmed,
      clock_confidence: 0.99,
      draft_confidence: 0.99,
    },
    readiness: {
      ...match.readiness,
      vision: { status: visionStatus },
    },
    winner_timeline: [],
    decisions: [],
    vision: [],
    markets: [],
  };
}

function detailWithFrame(): MatchDetail {
  const value = detail(1, "ready");
  const latest = value.latest_vision!;
  latest.source_frame_ref = FRAME_REF;
  latest.frame_digest = "a".repeat(64);
  latest.frame_url = `/api/monitor/matches/match-1/vision-frames/${"a".repeat(64)}.jpg`;
  value.vision = [latest];
  return value;
}

function analysisSection<T>(
  status: AnalysisSectionStatus,
  reason: string,
  data: T | null,
): AnalysisSection<T> {
  return { status, reason, data };
}

function strategyDecision(overrides: Partial<StrategyDecision> = {}): StrategyDecision {
  return {
    decision_key: DECISION_KEY,
    decided_at: "2026-07-16T12:02:00+00:00",
    map_number: 1,
    underdog_side: "team_two",
    market_probability: 0.35,
    model_probability: 0.39196993045504913,
    edge: 0.041969930455049154,
    data_quality: 0.8,
    eligible: 1,
    reason: "eligible",
    strategy_version: "comeback-shadow-v2",
    input_ref: INPUT_REF,
    contributions: {
      team_style: 0.12,
      player_form: -0.03,
      draft_curve: 0.08,
      late_game_style: 0,
      market_movement: 0.01,
    },
    conservative_contributions: {
      team_style: 0.08,
      player_form: -0.03,
      draft_curve: 0.04,
      late_game_style: 0,
      market_movement: 0.01,
    },
    inputs: {
      conservative_contributions: {
        team_style: 0.08,
        player_form: -0.03,
        draft_curve: 0.04,
        late_game_style: 0,
        market_movement: 0.01,
      },
      conservative_probability: 0.3730769263273363,
      independent_positive: true,
      vision: {
        captured_at: "2026-07-16T12:01:58+00:00",
        game_clock_seconds: 720,
        source_frame_ref: FRAME_REF,
      },
      draft_landmark: { model_version: "draft-model-v3" },
    },
    draft_authority: { curve_key: CURVE_KEY },
    vision_authority: { source_frame_ref: FRAME_REF },
    ...overrides,
  };
}

function roshStrategyDecision(
  selected = true,
  selectedMinute = 30,
  gameClockSeconds = 30 * 60,
): StrategyDecision {
  const marketProbability = 0.35;
  const contributions = {
    team_style: 0.12,
    player_form: -0.03,
    draft_curve: 0,
    lineup_rosh: selected ? 0.08 : 0,
    late_game_style: 0,
    market_movement: 0.01,
  };
  const conservative = {
    team_style: 0.08,
    player_form: -0.03,
    draft_curve: 0,
    lineup_rosh: selected ? 0.04 : 0,
    late_game_style: 0,
    market_movement: 0.01,
  };
  const probability = (values: Record<string, number>) => {
    const logit = Math.log(marketProbability / (1 - marketProbability));
    const score = logit + Object.values(values).reduce((sum, value) => sum + value, 0);
    return 1 / (1 + Math.exp(-score));
  };
  const modelProbability = probability(contributions);
  return strategyDecision({
    decision_key: selected ? "6".repeat(32) : "7".repeat(32),
    input_ref: selected ? "8".repeat(24) : "9".repeat(24),
    market_probability: marketProbability,
    model_probability: modelProbability,
    edge: modelProbability - marketProbability,
    eligible: selected ? 1 : 0,
    reason: selected ? "eligible" : "rosh_lineup_draft_mismatch",
    strategy_version: "comeback-shadow-v4-controlled-entry",
    contributions,
    conservative_contributions: conservative,
    inputs: {
      conservative_contributions: conservative,
      conservative_probability: probability(conservative),
      independent_positive: true,
      vision: {
        captured_at: "2026-07-16T12:01:58+00:00",
        game_clock_seconds: gameClockSeconds,
        source_frame_ref: FRAME_REF,
        radiant_team_side: "team_one",
      },
      market: {
        underdog_side: "team_two",
      },
      draft_landmark: { model_version: "draft-model-v3" },
      rosh_lineup_score: {
        score_key: ROSH_SCORE_KEY,
        draft_hash: "d".repeat(64),
        player_identity_hash: ROSH_PLAYER_IDENTITY_HASH,
        pure_score: -2.2,
        player_adjusted_score: -2.75,
        effective_score: -2.75,
        mode: "player_adjusted",
        player_coverage: 1,
        player_coverage_count: 10,
        stake_cap: 1,
        stake_multiplier: selected ? 1 : 0,
        formula_version: "dematus-rosh-v1",
        source_name: "stratz",
        source_week: 1_752_643_200,
        cache_week_start: 1_752_643_200,
        source_as_of: "2026-07-16T11:58:00+00:00",
        evidence_hash: ROSH_EVIDENCE_HASH,
        draft_matches_observation: selected,
        selected_table: selected ? "minute_table" : null,
        selected_minute: selected ? selectedMinute : null,
        selected_score: selected ? -2.75 : null,
        match_percentage: selected ? 76 : null,
        actual_stake_multiplier: selected ? 1 : 0,
      },
      comeback_state: {
        controllable: true,
        reason: "controlled_deficit",
        source_status: "available",
        source: "vision_hud",
        confidence: 0.96,
        underdog_side: "team_two",
        underdog_kills: 18,
        opponent_kills: 22,
        kill_deficit: 4,
        underdog_net_worth: null,
        opponent_net_worth: null,
        net_worth_deficit: null,
        net_worth_advantage_side: "radiant",
        net_worth_deficit_min: 5_000,
        net_worth_deficit_max: 5_999,
        unavailable_reason: null,
      },
      entry_window: {
        minimum_clock_seconds: 1200,
        maximum_clock_seconds: 2700,
        game_clock_seconds: gameClockSeconds,
        inside: true,
      },
      comeback_entry: {
        eligible: selected,
        reason: selected ? "eligible" : "rosh_direction_unavailable",
        rosh_underdog_probability: selected ? 0.5275 : null,
        policy: {
          minimum_clock_seconds: 1200,
          maximum_clock_seconds: 2700,
          minimum_kill_deficit: 2,
          maximum_kill_deficit: 10,
          minimum_net_worth_deficit: 1000,
          maximum_net_worth_deficit: 10_000,
          minimum_vision_confidence: 0.9,
        },
      },
    },
  });
}

function fixtureStrategyDecision(
  analysis: MatchAnalysis,
  eligible: 0 | 1,
): StrategyDecision {
  const decision = analysis.strategy.data?.decisions.find(
    (candidate) => candidate.eligible === eligible,
  );
  if (!decision) throw new Error(`fixture strategy decision eligible=${eligible} not found`);
  return decision;
}

function scoredBlockedDecision(analysis: MatchAnalysis): StrategyDecision {
  return {
    ...structuredClone(fixtureStrategyDecision(analysis, 1)),
    decision_key: "4".repeat(32),
    input_ref: "5".repeat(24),
    eligible: 0,
    reason: "edge_below_threshold",
  };
}

function lineupData(overrides: Partial<LineupAnalysisData> = {}): LineupAnalysisData {
  return {
    as_of: "2026-07-16T12:02:00+00:00",
    map_number: 1,
    radiant_team_side: "team_one",
    radiant: {
      hero_ids: [1, 2, 3, 4, 5],
      heroes: [{ hero_id: 1, hero_name: "Anti-Mage" }],
    },
    dire: { hero_ids: [6, 7, 8, 9, 10] },
    evidence: {
      draft_hash: "d".repeat(64),
      anchor_source_frame_ref: FRAME_REF,
      anchored_at: "2026-07-16T12:01:58+00:00",
      strict_mapping_id: 42,
    },
    scores: analysisSection<RoshLineupScoresData>("waiting", "rosh_lineup_score_pending", null),
    active_curve: analysisSection("available", "active_curve_available", {
      curve_key: CURVE_KEY,
      first_usable_at: "2026-07-16T12:01:59+00:00",
      active_horizon_minutes: 10,
      points: [
        {
          landmark_key: LANDMARK_10,
          horizon_minutes: 10,
          radiant_probability: 0.62,
          quality: 0.91,
          support: 180,
          uncertainty: 0.04,
          model_version: "draft-model-v3",
          validation_status: "passed",
          conditional: false,
          active: true,
        },
        {
          landmark_key: LANDMARK_20,
          horizon_minutes: 20,
          radiant_probability: 0.57,
          quality: 0.88,
          support: 160,
          uncertainty: 0.05,
          model_version: "draft-model-v3",
          validation_status: "passed",
          conditional: true,
          active: false,
        },
      ],
    }),
    players: analysisSection<LivePlayerIdentityData>(
      "unavailable",
      "live_player_identity_unavailable",
      null,
    ),
    ...overrides,
  };
}

function matchAnalysis(overrides: Partial<MatchAnalysis> = {}): MatchAnalysis {
  const decision = strategyDecision();
  return {
    odds: analysisSection("available", "odds_available", {
      point_count: 20,
      periods: ["map_1"],
      latest_observed_at: "2026-07-16T12:02:00+00:00",
    }),
    vision: analysisSection("available", "trusted_vision_available", {
      map_number: 1,
      captured_at: "2026-07-16T12:01:58+00:00",
      game_clock_seconds: 720,
      clock_confidence: 0.98,
      draft_confidence: 0.96,
      source_frame_ref: FRAME_REF,
    }),
    strategy: analysisSection<StrategyAnalysisData>("available", "strategy_available", {
      decisions: [decision],
      displayed_count: 1,
      scanned_count: 1,
      count_scope: "recent_scanned_window",
      has_more: false,
      truncated: false,
      excluded_decision_count: 0,
      excluded: {
        vision_invalidated: 0,
        mapping_impacted: 0,
        draft_conflicted: 0,
        invalid_payload: 0,
      },
    }),
    lineup: analysisSection("available", "lineup_available", lineupData()),
    ...overrides,
  };
}

function detailWithAnalysis(analysis = matchAnalysis()): MatchDetail {
  const value = detail(1, "ready");
  value.analysis = analysis;
  value.decisions = analysis.strategy.data?.decisions || [];
  value.vision = value.latest_vision ? [value.latest_vision] : [];
  value.winner_timeline = [{
    observed_at: "2026-07-16T12:02:00+00:00",
    period: "map_1",
    prices: { team_one: 1.8, team_two: 2.1 },
    probabilities: { team_one: 0.54, team_two: 0.46 },
    status: { team_one: "open", team_two: "open" },
  }];
  return value;
}

function detailWithLockedDraft(isLocked = true): MatchDetail {
  const value = detail(0, "ready");
  value.draft_mapping = {
    raybet_match_id: "match-1",
    map_number: 3,
    version: 1,
    source: "manual_correction",
    is_locked: isLocked,
    created_by: "operator",
    created_at: "2026-07-16T12:01:00+00:00",
    slots: Array.from({ length: 10 }, (_, index) => ({
      team_id: index < 5 ? 11 : 22,
      side: index < 5 ? "radiant" as const : "dire" as const,
      position: (index % 5) + 1,
      hero_id: index + 1,
      player_id: 100 + index,
    })),
  };
  return value;
}

function liveDraftPrediction(): LiveDraftProspectivePrediction {
  return {
    prediction_hash: "a".repeat(64),
    version: "live-draft-prospective-bridge-v1",
    identity: {
      raybet_match_id: "match-1",
      map_number: 3,
      mapping_version: 1,
      mapping_hash: "b".repeat(64),
    },
    operator_locked_at: "2026-07-16T12:01:00+00:00",
    confirmed_at: "2026-07-16T12:01:01+00:00",
    record_status: "paired",
    p0_probability: 0.55,
    p1_probability: 0.61,
    pure_rosh_score: 5.2,
    standardized_rosh_score: 0.5,
    rosh_logit_contribution: 0.33,
    missing_reason: null,
    candidate_hash: "84c4506f63b7c5b745b32373b0cb405383f837c60eae3231cc3d688a0b36e09d",
    causal_evidence: {
      game_clock_seconds: null,
      vision_frame_timestamp: null,
      draft_state_marker: "draft_complete",
      live_state_input_used: false,
      causal_status: "eligible",
      causal_reason: null,
    },
    created_at: "2026-07-16T12:01:02+00:00",
  };
}

function analysisWithSingleDecision(decision: StrategyDecision): MatchAnalysis {
  const analysis = matchAnalysis();
  analysis.strategy.data!.decisions = [decision];
  analysis.strategy.data!.displayed_count = 1;
  analysis.strategy.data!.scanned_count = 1;
  return analysis;
}

function sourceRow(label: string): HTMLElement {
  const row = screen.getAllByText(label)
    .map((element) => element.closest(".source-status-row"))
    .find((element): element is HTMLElement => element instanceof HTMLElement);
  if (!(row instanceof HTMLElement)) throw new Error(`source row not found: ${label}`);
  return row;
}

describe("MatchWorkspace", () => {
  beforeEach(() => {
    fetchLiveDraftPredictionMock.mockReset();
    fetchLiveDraftPredictionMock.mockResolvedValue({ status: "not_found", prediction: null });
    createLiveDraftPredictionMock.mockReset();
  });
  afterEach(cleanup);

  it("never auto-runs official-v2 and requires explicit confirmation", async () => {
    render(
      <MatchWorkspace
        csrfToken="csrf"
        detail={detailWithLockedDraft()}
        error={null}
        loading={false}
        match={match}
        replay={false}
      />,
    );

    await waitFor(() => expect(fetchLiveDraftPredictionMock).toHaveBeenCalledWith(
      "match-1", 3, 1, expect.any(AbortSignal),
    ));
    expect(createLiveDraftPredictionMock).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "生成阵容预测" })).toBeDisabled();
    expect(screen.queryByText("stratz-rosh-web-2026-07-28-v2")).not.toBeInTheDocument();
  });

  it("creates and displays the frozen paired P0/P1 only after confirmation", async () => {
    createLiveDraftPredictionMock.mockResolvedValueOnce({
      status: "created",
      prediction: liveDraftPrediction(),
    });
    render(
      <MatchWorkspace
        csrfToken="csrf"
        detail={detailWithLockedDraft()}
        error={null}
        loading={false}
        match={match}
        replay={false}
      />,
    );

    fireEvent.click(screen.getByText(/本次预测只使用已锁定阵容/));
    fireEvent.click(screen.getByRole("button", { name: "生成阵容预测" }));
    await waitFor(() => expect(createLiveDraftPredictionMock).toHaveBeenCalledWith(
      "match-1", 3, 1, "csrf", null,
    ));
    expect(await screen.findByText("55.0%")).toBeInTheDocument();
    expect(screen.getByText("61.0%")).toBeInTheDocument();
    expect(screen.getByText("eligible")).toBeInTheDocument();
  });

  it("does not run prematch Rosh analysis until the live draft is locked", () => {
    render(
      <MatchWorkspace
        detail={detailWithLockedDraft(false)}
        error={null}
        loading={false}
        match={match}
        replay={false}
      />,
    );

    expect(createLiveDraftPredictionMock).not.toHaveBeenCalled();
    expect(screen.getByText("请先确认并锁定阵容。")).toBeInTheDocument();
  });

  it("shows the stable seed blocker without calling an alternate prediction path", async () => {
    createLiveDraftPredictionMock.mockResolvedValueOnce({
      status: "blocked",
      prediction: null,
      missing_reason: "prospective_team_rating_seed_unavailable",
    });
    render(
      <MatchWorkspace
        csrfToken="csrf"
        detail={detailWithLockedDraft()}
        error={null}
        loading={false}
        match={match}
        replay={false}
      />,
    );

    fireEvent.click(screen.getByText(/本次预测只使用已锁定阵容/));
    fireEvent.click(screen.getByRole("button", { name: "生成阵容预测" }));
    expect(await screen.findByText("prospective_team_rating_seed_unavailable"))
      .toBeInTheDocument();
  });

  it("keeps the decision surface primary and technical evidence collapsed", () => {
    const view = render(
      <MatchWorkspace
        detail={detailWithAnalysis()}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    const decisionHero = view.container.querySelector(".match-decision-hero");
    const advanced = view.container.querySelector(".advanced-analysis");
    expect(decisionHero).toContainElement(view.container.querySelector(".strategy-overview"));
    expect(advanced).not.toHaveAttribute("open");
    expect(screen.getByText("查看阵容、策略记录与原始证据")).toBeInTheDocument();

    fireEvent.click(screen.getByText("查看阵容、策略记录与原始证据"));
    expect(advanced).toHaveAttribute("open");
    expect(screen.getByLabelText("Radiant 阵容")).toBeInTheDocument();
  });

  it("separates the locked manual draft from dynamic Vision state", () => {
    const value = detail(0, "ready");
    value.draft_mapping = {
      raybet_match_id: "match-1",
      map_number: 1,
      version: 2,
      source: "manual_correction",
      is_locked: true,
      created_by: "operator",
      created_at: "2026-07-16T12:01:00+00:00",
      slots: Array.from({ length: 10 }, (_, index) => ({
        team_id: index < 5 ? 11 : 22,
        side: index < 5 ? "radiant" as const : "dire" as const,
        position: (index % 5) + 1,
        hero_id: index + 1,
        player_id: 100 + index,
      })),
    };
    value.draft_context = {
      status: "ready",
      reason: "draft_context_ready",
      source: "strict_mapping",
      teams: [
        {
          match_side: "team_one",
          team_id: 11,
          team_name: "Radiant Club",
          roster_match_id: 9001,
          players: Array.from({ length: 5 }, (_, index) => ({
            player_id: 100 + index,
            player_name: `Radiant Player ${index + 1}`,
            position: index + 1,
            confidence: 1,
            position_source: "historical_pattern",
          })),
        },
        {
          match_side: "team_two",
          team_id: 22,
          team_name: "Dire Club",
          roster_match_id: 9002,
          players: Array.from({ length: 5 }, (_, index) => ({
            player_id: 105 + index,
            player_name: `Dire Player ${index + 1}`,
            position: index + 1,
            confidence: 1,
            position_source: "historical_pattern",
          })),
        },
      ],
    };
    value.latest_game_snapshot = {
      snapshot_id: 1,
      raybet_match_id: "match-1",
      map_number: 1,
      game_time_seconds: 1420,
      radiant_networth: 42300,
      dire_networth: 38100,
      networth_lead: 4200,
      radiant_kills: 18,
      dire_kills: 13,
      vision_confidence: 0.94,
      screenshot_path: null,
      source: "vision",
      captured_at: "2026-07-16T12:02:00+00:00",
      created_by: null,
      created_at: "2026-07-16T12:02:00+00:00",
    };
    value.game_snapshots = [value.latest_game_snapshot];

    render(
      <MatchWorkspace
        csrfToken="csrf"
        detail={value}
        error={null}
        loading={false}
        match={match}
        replay={false}
      />,
    );

    expect(screen.getByText("本局阵容映射")).toBeInTheDocument();
    expect(screen.getByText(/版本 2 · 已人工锁定/)).toBeInTheDocument();
    expect(screen.getByText("天辉 · Radiant Club")).toBeInTheDocument();
    expect(screen.getByText(/Radiant Player 1 \/ Radiant Player 2/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "选择 天辉 1 号位英雄" }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "天辉 1 号位选手" }))
      .not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "编辑阵容" }));

    const editor = screen.getByRole("dialog", { name: "阵容录入" });
    expect(within(editor).getByRole("button", { name: "选择 天辉 1 号位英雄" }))
      .toBeInTheDocument();
    expect(within(editor).getByRole("combobox", { name: "天辉 1 号位选手" }))
      .toHaveValue("100");
    expect(within(editor).getByRole("option", { name: "Radiant Player 1 · 默认" }))
      .toBeInTheDocument();
    expect(screen.queryByText("队伍 ID")).not.toBeInTheDocument();
    expect(screen.queryByText("英雄 ID")).not.toBeInTheDocument();
    expect(screen.getByText("Vision 实时状态")).toBeInTheDocument();
    expect(screen.getByText("42.3K")).toBeInTheDocument();
    expect(screen.getByText("天辉 +4,200")).toBeInTheDocument();
  });

  it("clears manual Vision values and disables an incomplete correction", () => {
    const value = detail(0, "missing");
    value.latest_vision = null;

    render(
      <MatchWorkspace
        csrfToken="csrf"
        detail={value}
        error={null}
        loading={false}
        match={match}
        replay={false}
      />,
    );

    const radiantNetworth = screen.getByRole("spinbutton", { name: "天辉总经济" });
    const direNetworth = screen.getByRole("spinbutton", { name: "夜魇总经济" });
    const radiantKills = screen.getByRole("spinbutton", { name: "天辉击杀（可选）" });
    const direKills = screen.getByRole("spinbutton", { name: "夜魇击杀（可选）" });
    const submit = screen.getByRole("button", { name: "追加人工修正" });

    fireEvent.change(radiantNetworth, { target: { value: "20000" } });
    fireEvent.change(direNetworth, { target: { value: "19000" } });
    fireEvent.change(radiantKills, { target: { value: "0" } });
    fireEvent.change(direKills, { target: { value: "4" } });
    expect(submit).toBeEnabled();
    expect(radiantKills).toHaveValue(0);

    for (const input of [radiantNetworth, direNetworth, radiantKills, direKills]) {
      fireEvent.change(input, { target: { value: "" } });
      expect(input).toHaveValue(null);
    }
    expect(submit).toBeDisabled();
  });

  it("keeps the chart on the active Vision map when a future market is open", async () => {
    const value = detail(0, "ready");
    value.current_map_number = 1;
    value.winner = {
      observed_at: "2026-07-16T12:00:00+00:00",
      period: "map_2",
      complete: true,
      prices: { team_one: 1.9, team_two: 1.9 },
      probabilities: { team_one: 0.5, team_two: 0.5 },
    };
    value.winner_timeline = ["map_1", "map_2"].map((period) => ({
      observed_at: "2026-07-16T12:00:00+00:00",
      period,
      prices: { team_one: 1.9, team_two: 1.9 },
      probabilities: { team_one: 0.5, team_two: 0.5 },
      status: { team_one: "1", team_two: "1" },
    }));

    render(
      <MatchWorkspace
        detail={value}
        error={null}
        loading={false}
        match={match}
        replay={false}
      />,
    );

    expect(await screen.findByRole("button", { name: "probability-chart" }))
      .toHaveAttribute("data-selected-period", "map_1");
  });

  it("binds the hero and transition explanation to the chronological latest decision", () => {
    const analysis = matchAnalysis();
    const previous = strategyDecision({
      decision_key: "4".repeat(32),
      input_ref: "5".repeat(24),
      decided_at: "2026-07-16T12:01:00+00:00",
    });
    const current = strategyDecision({
      decision_key: "6".repeat(32),
      input_ref: "7".repeat(24),
      decided_at: "2026-07-16T12:02:00+00:00",
      eligible: 0,
      reason: "edge_below_threshold",
    });
    analysis.strategy.data!.decisions = [current, previous];
    analysis.strategy.data!.displayed_count = 2;
    analysis.strategy.data!.scanned_count = 2;

    render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getByRole("heading", { name: "当前策略拒绝" })).toBeInTheDocument();
    const delta = screen.getByLabelText("与上次判断相比");
    expect(within(delta).getByText(
      "模型 Edge 为 +4.2%，已低于最终阈值，因此最终策略由合格变为拒绝。",
    ))
      .toBeInTheDocument();
    expect(within(delta).getByText("策略合格")).toBeInTheDocument();
    expect(within(delta).getByText("策略拒绝")).toBeInTheDocument();
    expect(within(delta).getByText("全部策略门槛已通过")).toBeInTheDocument();
    expect(within(delta).getByText("Edge 未达到最终阈值")).toBeInTheDocument();
  });

  it("does not present an older transition as current when the latest evidence needs review", () => {
    const analysis = matchAnalysis();
    const previous = strategyDecision({
      decision_key: "4".repeat(32),
      input_ref: "5".repeat(24),
      decided_at: "2026-07-16T12:01:00+00:00",
    });
    const current = strategyDecision({
      decision_key: "6".repeat(32),
      input_ref: "7".repeat(24),
      decided_at: "2026-07-16T12:02:00+00:00",
      eligible: 0,
      reason: "edge_below_threshold",
    });
    const review = strategyDecision({
      decision_key: "8".repeat(32),
      input_ref: "9".repeat(24),
      decided_at: "2026-07-16T12:03:00+00:00",
      eligible: 0,
      reason: "evidence_invalid",
    });
    analysis.strategy.data!.decisions = [current, review, previous];
    analysis.strategy.data!.displayed_count = 3;
    analysis.strategy.data!.scanned_count = 3;

    render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:03:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getByRole("heading", { name: "策略证据无效" })).toBeInTheDocument();
    expect(screen.queryByLabelText("与上次判断相比")).not.toBeInTheDocument();
    expect(screen.getByRole("status", { name: "判断变化暂不可用" }))
      .toHaveTextContent("最新判断证据需复核，暂不生成变化比较");
  });

  it("shows the prematch snapshot while the provider is still pre-match", () => {
    const prematch: MatchDetail = {
      ...detail(0, "missing"),
      lifecycle: "degraded",
      provider_status: "1",
      winner: null,
      prematch_winner: {
        observed_at: "2026-07-16T12:04:01+00:00",
        period: "map_1",
        complete: true,
        prices: { team_one: 1.65, team_two: 2.19 },
        probabilities: { team_one: 0.5703125, team_two: 0.4296875 },
      },
      readiness: {
        ...match.readiness,
        odds: { status: "missing" },
        mapping: { status: "missing" },
        vision: { status: "missing" },
      },
    };

    const view = render(
      <MatchWorkspace
        detail={prematch}
        error={null}
        loading={false}
        match={prematch}
        now={Date.parse("2026-07-16T12:10:00+00:00")}
        replay={false}
      />,
    );

    expect(screen.getAllByText("赛前快照").length).toBeGreaterThan(0);
    expect(screen.getByText(
      `赛前快照 ${formatDateTime(prematch.prematch_winner?.observed_at)}`,
    )).toBeInTheDocument();
    expect(screen.getByText("1.65")).toBeInTheDocument();
    expect(screen.getByText("2.19")).toBeInTheDocument();
    expect(screen.getByText(/不进入实时策略/)).toBeInTheDocument();
    expect(screen.getByText(/赛前快照已保存 · 映射/)).toBeInTheDocument();
    expect(screen.queryByText("实时胜负盘")).not.toBeInTheDocument();
    expect(view.container.querySelector(".source-age.stale")).not.toBeInTheDocument();
  });

  it("requires a confirmed and fresh-enough vision observation", () => {
    const view = render(
      <MatchWorkspace
        detail={detail(0, "ready")}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getByText("暂无可信比赛时钟")).toBeInTheDocument();
    expect(screen.getByText("局数待确认")).toBeInTheDocument();
    expect(screen.queryByText("第 1 局")).not.toBeInTheDocument();

    view.rerender(
      <MatchWorkspace
        detail={detail(1, "stale")}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        replay={false}
      />,
    );
    expect(screen.getByText("暂无可信比赛时钟")).toBeInTheDocument();
    expect(screen.getByText("局数待确认")).toBeInTheDocument();

    view.rerender(
      <MatchWorkspace
        detail={detail(1, "delayed")}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        replay={false}
      />,
    );
    expect(screen.getByText("可信时钟 2:00")).toBeInTheDocument();
  });

  it("renders the latest captured frame from the match-scoped API URL", () => {
    const framed = detailWithFrame();
    framed.watch_link = {
      kind: "public_stream",
      availability: "available",
      url: "https://qplay.ehome.gg/live/42.m3u8",
      reason: "verified_unsigned_stream",
    };

    render(
      <MatchWorkspace
        detail={framed}
        error={null}
        loading={false}
        match={framed}
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        replay={false}
      />,
    );

    const image = screen.getByRole("img", { name: "最近有效视觉观测画面" });
    expect(image).toHaveAttribute("src", framed.latest_vision!.frame_url);
    expect(image).not.toHaveAttribute("src", framed.watch_link.url);
    expect(screen.getByText("已确认")).toBeInTheDocument();
    expect(screen.getByText("game")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "放大最近有效视觉观测画面" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "放大的最近有效视觉观测画面" })).toHaveAttribute(
      "src",
      framed.latest_vision!.frame_url,
    );
    fireEvent.click(screen.getByRole("button", { name: "关闭画面预览" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows an unrecognized capture without treating it as strategy evidence", () => {
    const captured = detail(1, "unconfirmed");
    const digest = "b".repeat(64);
    captured.latest_capture = {
      captured_at: "2026-07-16T12:00:04+00:00",
      map_number: null,
      game_clock_seconds: null,
      screen_state: "unknown",
      confirmed: 0,
      clock_confidence: 0,
      draft_confidence: 0,
      source_frame_ref: `vision-frame:sha256:${digest}`,
      frame_digest: digest,
      frame_url: `/api/monitor/matches/match-1/captures/${digest}.jpg`,
      strategy_authority: false,
    };

    render(
      <MatchWorkspace
        detail={captured}
        error={null}
        loading={false}
        match={captured}
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        replay={false}
      />,
    );

    const image = screen.getByRole("img", { name: "最近捕获但未确认的画面" });
    expect(image).toHaveAttribute("src", captured.latest_capture.frame_url);
    expect(screen.getByText("已捕获，HUD 未识别")).toBeInTheDocument();
    expect(screen.getAllByText("未确认，不能进入策略")).toHaveLength(2);
  });

  it("distinguishes a recognized HUD clock from an unconfirmed draft", () => {
    const captured = detail(1, "unconfirmed");
    const digest = "c".repeat(64);
    captured.latest_capture = {
      captured_at: "2026-07-16T12:00:04+00:00",
      map_number: 2,
      game_clock_seconds: 2886,
      screen_state: "game",
      confirmed: 0,
      clock_confidence: 0.99,
      draft_confidence: 0,
      source_frame_ref: `vision-frame:sha256:${digest}`,
      frame_digest: digest,
      frame_url: `/api/monitor/matches/match-1/captures/${digest}.jpg`,
      strategy_authority: false,
    };

    render(
      <MatchWorkspace
        detail={captured}
        error={null}
        loading={false}
        match={captured}
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getByText("HUD 时钟已识别，完整阵容未确认")).toBeInTheDocument();
    expect(screen.queryByText("已捕获，HUD 未识别")).not.toBeInTheDocument();
    expect(screen.getAllByText("未确认，不能进入策略")).toHaveLength(2);
  });

  it("shows an explicit empty state when a frame is missing or fails to load", () => {
    const withoutFrame = detail(1, "ready");
    withoutFrame.watch_link = {
      kind: "public_stream",
      availability: "available",
      url: "https://qplay.ehome.gg/live/42.m3u8",
      reason: "verified_unsigned_stream",
    };
    const view = render(
      <MatchWorkspace
        detail={withoutFrame}
        error={null}
        loading={false}
        match={withoutFrame}
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getByText("暂无可用的已捕获画面")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(view.container.querySelector(".vision-frame-stage")).not.toBeInTheDocument();

    const framed = detailWithFrame();
    view.rerender(
      <MatchWorkspace
        detail={framed}
        error={null}
        loading={false}
        match={framed}
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        replay={false}
      />,
    );
    fireEvent.error(screen.getByRole("img", { name: "最近有效视觉观测画面" }));
    expect(screen.getByText("已捕获画面加载失败")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("labels available entry links by their proven kind", () => {
    const pageMatch: MonitorMatch = {
      ...match,
      watch_link: {
        kind: "match_page",
        availability: "available",
        url: "https://www.ray086.com/sports/esports",
        reason: "captured_raybet_match_page",
      },
    };
    const view = render(
      <MatchWorkspace
        detail={null}
        error={null}
        loading={false}
        match={pageMatch}
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getByRole("link", { name: "打开比赛页" })).toHaveAttribute(
      "href",
      "https://www.ray086.com/sports/esports",
    );
    expect(screen.queryByRole("link", { name: "打开直播" })).not.toBeInTheDocument();

    view.rerender(
      <MatchWorkspace
        detail={null}
        error={null}
        loading={false}
        match={{
          ...match,
          watch_link: {
            kind: "public_stream",
            availability: "available",
            url: "https://qplay.ehome.gg/live/42.m3u8",
            reason: "verified_unsigned_stream",
          },
        }}
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getByRole("link", { name: "打开直播" })).toHaveAttribute(
      "href",
      "https://qplay.ehome.gg/live/42.m3u8",
    );
    expect(screen.queryByRole("link", { name: "打开比赛页" })).not.toBeInTheDocument();
  });

  it("allows the current RayBet CDN as an unsigned public stream", () => {
    render(
      <MatchWorkspace
        detail={null}
        error={null}
        loading={false}
        match={{
          ...match,
          watch_link: {
            kind: "public_stream",
            availability: "available",
            url: "https://play.xmshlb.com/live/42.m3u8",
            reason: "verified_unsigned_stream",
          },
        }}
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getByRole("link", { name: "打开直播" })).toHaveAttribute(
      "href",
      "https://play.xmshlb.com/live/42.m3u8",
    );
  });

  it("does not fall back to a legacy or unavailable live_url", () => {
    const { rerender } = render(
      <MatchWorkspace
        detail={null}
        error={null}
        loading={false}
        match={{
          ...match,
          live_url: "https://qplay.ehome.gg/live/42.m3u8",
        }}
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.queryByRole("link", { name: /打开/ })).not.toBeInTheDocument();

    rerender(
      <MatchWorkspace
        detail={null}
        error={null}
        loading={false}
        match={{
          ...match,
          watch_link: {
            kind: "none",
            availability: "unavailable",
            url: null,
            reason: "no_safe_entry",
          },
        }}
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.queryByRole("link", { name: /打开/ })).not.toBeInTheDocument();

    for (const watch_link of [
      {
        kind: "match_page",
        availability: "available",
        url: "javascript:alert(1)",
        reason: "malicious_scheme",
      },
      {
        kind: "match_page",
        availability: "available",
        url: "https://foreign.example/sports/esports",
        reason: "foreign_host",
      },
      {
        kind: "public_stream",
        availability: "available",
        url: "https://qplay.ehome.gg/live/42.m3u8?token=stripped",
        reason: "signed_stream",
      },
    ] as const) {
      rerender(
        <MatchWorkspace
          detail={null}
          error={null}
          loading={false}
          match={{ ...match, watch_link }}
          now={Date.parse("2026-07-16T12:00:05+00:00")}
          replay={false}
        />,
      );
      expect(screen.queryByRole("link", { name: /打开/ })).not.toBeInTheDocument();
    }
  });

  it("renders persisted strategy contributions, versions and input evidence without treating logit as percent", () => {
    render(
      <MatchWorkspace
        detail={detailWithAnalysis()}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getByText("Δlogit +0.120")).toBeInTheDocument();
    expect(screen.getByText("保守 +0.080")).toBeInTheDocument();
    expect(screen.getByText("comeback-shadow-v2")).toBeInTheDocument();
    expect(screen.getByText(INPUT_REF)).toBeInTheDocument();
    expect(screen.getByText(DECISION_KEY)).toBeInTheDocument();
    expect(screen.getByText("质量 80.0%")).toBeInTheDocument();
    expect(screen.getByText("draft-model-v3")).toBeInTheDocument();
    expect(screen.queryByText("策略就绪")).not.toBeInTheDocument();
    expect(screen.getByText("进程运行")).toBeInTheDocument();
  });

  it("shows decision-time Rosh and live situation evidence without using the latest score card", () => {
    const analysis = matchAnalysis();
    const decision = roshStrategyDecision(true);
    analysis.strategy.data!.decisions = [decision];
    analysis.strategy.data!.displayed_count = 1;
    analysis.lineup.data!.scores = analysisSection("available", "rosh_lineup_score_available", {
      pure_lineup_score: 9.1,
      player_adjusted_lineup_score: 9.9,
      effective_lineup_score: 9.9,
      mode: "player_adjusted",
      player_coverage: 1,
      player_coverage_count: 10,
      stake_multiplier: 1,
      formula_version: "dematus-rosh-v1",
      source_as_of: "2026-07-16T12:01:59+00:00",
      score_key: "4".repeat(64),
      player_identity_hash: "5".repeat(64),
      evidence_hash: "6".repeat(64),
      stake_cap: 1,
    });
    const view = render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );
    const row = view.container.querySelector(".decision-row");
    if (!(row instanceof HTMLElement)) throw new Error("decision row not found");

    const rosh = within(row).getByLabelText("决策时 Rosh 证据");
    expect(within(rosh).getByText("当前分钟分 -2.8 pp · 30 分钟桶")).toBeInTheDocument();
    expect(within(rosh).getByText("决策时阵容局势 Dire 阵容占优 -2.8 pp")).toBeInTheDocument();
    expect(within(rosh).getByText("评分模式 选手修正")).toBeInTheDocument();
    expect(within(rosh).getByText("选手覆盖 100.0% (10/10)")).toBeInTheDocument();
    expect(within(rosh).getByText("dematus-rosh-v1")).toBeInTheDocument();
    expect(within(row).queryByText("+9.9 pp")).not.toBeInTheDocument();

    const situation = within(row).getByLabelText("决策时实时局势证据");
    expect(within(situation).getByText("实时局势 可控劣势")).toBeInTheDocument();
    expect(within(situation).getByText("弱势方击杀落后 4")).toBeInTheDocument();
    expect(within(situation).getByText("弱势方经济落后 5,000-5,999")).toBeInTheDocument();
    expect(within(situation).getByText(/HUD vision_hud · 置信 96\.0%/)).toBeInTheDocument();
    expect(within(situation).getByText(FRAME_REF)).toBeInTheDocument();
    expect(within(situation).getByText(/入场时间窗 命中 · 20:00-45:00/)).toBeInTheDocument();
    expect(within(situation).getByText("eligible")).toBeInTheDocument();
    expect(within(situation).getByText(/可控区间：击杀落后 2-10 · 经济落后 1,000-10,000 · 20:00-45:00 · HUD 置信至少 90\.0%/)).toBeInTheDocument();

    const overview = view.getByLabelText("当前策略结论与入场链路");
    expect(within(overview).getByText("最终策略合格")).toBeInTheDocument();
    expect(within(overview).getByText("证据有效")).toBeInTheDocument();
    expect(within(overview).getByText(/HUD 已验证 · 置信 96\.0%/)).toBeInTheDocument();
    expect(within(overview).getByText("入场门槛通过 · 击杀落后 4 · 经济落后 5,000-5,999")).toBeInTheDocument();
    expect(within(overview).getByText("52.8% 支持弱势方")).toBeInTheDocument();
    expect(within(overview).getByText("最终合格")).toBeInTheDocument();
    expect(within(overview).getByText("v4 仅为纸面影子信号，不代表策略表现已验证。")).toBeInTheDocument();
  });

  it("fails closed when a v4 decision restores forbidden exact economy totals", () => {
    const decision = roshStrategyDecision(true);
    const state = decision.inputs!.comeback_state as Record<string, unknown>;
    Object.assign(state, {
      underdog_net_worth: 38_000,
      opponent_net_worth: 43_000,
      net_worth_deficit: 5_000,
      net_worth_advantage_side: null,
      net_worth_deficit_min: null,
      net_worth_deficit_max: null,
    });

    const view = render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysisWithSingleDecision(decision))}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    expect(view.container.querySelector(".decision-row")).not.toBeInTheDocument();
    const overview = view.getByLabelText("当前策略结论与入场链路");
    expect(within(overview).getByText("策略证据无效")).toBeInTheDocument();
    expect(within(overview).getByText("证据无效")).toBeInTheDocument();
    expect(within(overview).queryByText(/入场判定 允许/)).not.toBeInTheDocument();
  });

  it("rejects a v4 economy bucket whose signed range contradicts the HUD leader", () => {
    const decision = roshStrategyDecision(true);
    const state = decision.inputs!.comeback_state as Record<string, unknown>;
    state.net_worth_advantage_side = "dire";

    const view = render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysisWithSingleDecision(decision))}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    expect(view.container.querySelector(".decision-row")).not.toBeInTheDocument();
    expect(within(view.getByLabelText("当前策略结论与入场链路")).getByText("证据无效")).toBeInTheDocument();
  });

  it.each([
    ["unavailable state with a permissive entry", (decision: StrategyDecision) => {
      const state = decision.inputs!.comeback_state as Record<string, unknown>;
      Object.assign(state, {
        controllable: false,
        reason: "live_state_not_provided",
        source_status: "unavailable",
        source: null,
        confidence: 0,
        underdog_kills: null,
        opponent_kills: null,
        kill_deficit: null,
        net_worth_advantage_side: null,
        net_worth_deficit_min: null,
        net_worth_deficit_max: null,
        unavailable_reason: "live_state_not_provided",
      });
    }],
    ["eligible entry with a non-eligible reason", (decision: StrategyDecision) => {
      const entry = decision.inputs!.comeback_entry as Record<string, unknown>;
      entry.reason = "rosh_direction_unavailable";
    }],
    ["tampered frozen entry policy", (decision: StrategyDecision) => {
      const entry = decision.inputs!.comeback_entry as Record<string, unknown>;
      const policy = entry.policy as Record<string, unknown>;
      policy.minimum_kill_deficit = 3;
    }],
    ["entry window flag inconsistent with its game clock", (decision: StrategyDecision) => {
      const window = decision.inputs!.entry_window as Record<string, unknown>;
      window.inside = false;
    }],
  ] as const)("fails closed on %s", (_, mutate) => {
    const decision = roshStrategyDecision(true);
    mutate(decision);
    const view = render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysisWithSingleDecision(decision))}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    expect(view.container.querySelector(".decision-row")).not.toBeInTheDocument();
    const overview = view.getByLabelText("当前策略结论与入场链路");
    expect(within(overview).getByText("策略证据无效")).toBeInTheDocument();
    expect(within(overview).getByText("证据无效")).toBeInTheDocument();
  });

  it("keeps a canonical negative deficit range as valid underdog-ahead evidence", () => {
    const decision = roshStrategyDecision(true);
    decision.eligible = 0;
    decision.reason = "underdog_deficit_not_material";
    const state = decision.inputs!.comeback_state as Record<string, unknown>;
    Object.assign(state, {
      controllable: false,
      reason: "underdog_deficit_not_material",
      net_worth_advantage_side: "dire",
      net_worth_deficit_min: -5_999,
      net_worth_deficit_max: -5_000,
    });
    const entry = decision.inputs!.comeback_entry as Record<string, unknown>;
    entry.eligible = false;
    entry.reason = "underdog_deficit_not_material";

    const view = render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysisWithSingleDecision(decision))}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    const row = view.container.querySelector(".decision-row");
    if (!(row instanceof HTMLElement)) throw new Error("decision row not found");
    expect(within(row).getByText("弱势方经济领先 5,000-5,999")).toBeInTheDocument();
    const overview = view.getByLabelText("当前策略结论与入场链路");
    expect(within(overview).getByText("当前策略拒绝")).toBeInTheDocument();
    expect(within(overview).getByText("证据有效")).toBeInTheDocument();
    expect(within(overview).getByText("最终拒绝")).toBeInTheDocument();
  });

  it("distinguishes an entry candidate from a final edge rejection", () => {
    const decision = roshStrategyDecision(true);
    decision.eligible = 0;
    decision.reason = "edge_below_threshold";
    const view = render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysisWithSingleDecision(decision))}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    const overview = view.getByLabelText("当前策略结论与入场链路");
    expect(within(overview).getByText("候选通过，最终策略拒绝")).toBeInTheDocument();
    expect(within(overview).getByText("入场候选通过")).toBeInTheDocument();
    expect(within(overview).getByText(/入场门槛通过/)).toBeInTheDocument();
    expect(within(overview).getByText("最终拒绝")).toBeInTheDocument();
    expect(within(overview).queryByText("证据无效")).not.toBeInTheDocument();
  });

  it("keeps a valid 25-minute Rosh bucket visible", () => {
    const analysis = matchAnalysis();
    const decision = roshStrategyDecision(true, 25);
    analysis.strategy.data!.decisions = [decision];
    analysis.strategy.data!.displayed_count = 1;
    const view = render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );
    const row = view.container.querySelector(".decision-row");
    if (!(row instanceof HTMLElement)) throw new Error("decision row not found");

    const rosh = within(row).getByLabelText("决策时 Rosh 证据");
    expect(within(rosh).getByText("当前分钟分 -2.8 pp · 25 分钟桶")).toBeInTheDocument();
  });

  it("rejects a Rosh bucket later than the decision game clock", () => {
    const analysis = matchAnalysis();
    analysis.strategy.data!.decisions = [roshStrategyDecision(true, 25, 24 * 60)];
    analysis.strategy.data!.displayed_count = 1;
    const view = render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getAllByText("strategy_evidence_invalid").length).toBeGreaterThan(0);
    expect(view.container.querySelector(".decision-row")).not.toBeInTheDocument();
  });

  it("shows the persisted Rosh blocker when a decision has no current-minute score", () => {
    const analysis = matchAnalysis();
    const decision = roshStrategyDecision(false);
    analysis.strategy.data!.decisions = [decision];
    analysis.strategy.data!.displayed_count = 1;
    const view = render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );
    const row = view.container.querySelector(".decision-row");
    if (!(row instanceof HTMLElement)) throw new Error("decision row not found");
    const rosh = within(row).getByLabelText("决策时 Rosh 证据");

    expect(within(rosh).getByText("当前分钟分 不可用")).toBeInTheDocument();
    expect(within(rosh).getByText("决策时阵容局势 不可判定")).toBeInTheDocument();
    expect(within(rosh).getByText(/Rosh 评分阵容与决策时可信阵容不一致/)).toBeInTheDocument();
    expect(within(rosh).getByText(/rosh_lineup_draft_mismatch/)).toBeInTheDocument();
  });

  it("renders the backend mixed golden without dropping blocked or eligible decisions", () => {
    const goldenAnalysis = structuredClone(goldenAvailableAnalysis) as unknown as MatchAnalysis;
    const goldenDecision = fixtureStrategyDecision(goldenAnalysis, 1);
    const blockedDecision = scoredBlockedDecision(goldenAnalysis);
    goldenAnalysis.strategy.data!.decisions = [blockedDecision, goldenDecision];
    goldenAnalysis.strategy.data!.displayed_count = 2;
    goldenAnalysis.strategy.data!.scanned_count = 2;
    goldenAnalysis.strategy.data!.excluded_decision_count = 0;
    goldenAnalysis.strategy.data!.excluded = {
      vision_invalidated: 0,
      mapping_impacted: 0,
      draft_conflicted: 0,
      invalid_payload: 0,
    };
    const decisionKey = goldenDecision.decision_key!;
    const inputRef = goldenDecision.input_ref!;
    const view = render(
      <MatchWorkspace
        detail={detailWithAnalysis(goldenAnalysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-14T14:00:00+00:00")}
        replay={false}
      />,
    );
    const strategy = view.container.querySelector(".decision-section");
    if (!(strategy instanceof HTMLElement)) throw new Error("strategy section not found");

    expect(within(strategy).getByText("有证据")).toBeInTheDocument();
    expect(within(strategy).getByText("显示最近 2 条")).toBeInTheDocument();
    expect(decisionKey).toMatch(/^[0-9a-f]{32}$/);
    expect(inputRef).toMatch(/^[0-9a-f]{24}$/);
    const eligibleRow = within(strategy).getByText(decisionKey).closest(".decision-row");
    if (!(eligibleRow instanceof HTMLElement)) throw new Error("eligible decision row not found");
    const eligibleReason = eligibleRow.querySelector(".decision-reason-code");
    if (!(eligibleReason instanceof HTMLElement)) throw new Error("eligible reason not found");
    expect(within(eligibleReason).getByText("eligible")).toBeInTheDocument();
    expect(within(eligibleRow).getByText(inputRef)).toBeInTheDocument();
    expect(within(eligibleRow).getByText("队伍风格")).toBeInTheDocument();
    expect(within(eligibleRow).getByText("Δlogit +0.502")).toBeInTheDocument();
    expect(within(eligibleRow).getByText("保守 +0.402")).toBeInTheDocument();
    expect(within(eligibleRow).getByText("模型 56.0%")).toBeInTheDocument();
    expect(within(eligibleRow).getByText("市场 40.0%")).toBeInTheDocument();
    expect(within(eligibleRow).getByText("Edge +16.0%")).toBeInTheDocument();
    expect(within(eligibleRow).getByText("质量 83.0%")).toBeInTheDocument();
    expect(within(eligibleRow).getByText("test-draft-model-v1")).toBeInTheDocument();
    expect(within(strategy).getByText("扫描范围：最近 2 条候选记录")).toBeInTheDocument();
    const evidence = within(eligibleRow).getByLabelText("持久化决策证据");
    expect(within(evidence).getByText(/^视觉 (?!未提供).+/)).toBeInTheDocument();
    expect(within(evidence).getByText("时钟 20:00")).toBeInTheDocument();
    expect(within(evidence).getByText(
      "vision-frame:sha256:3fa7663391a9ab14c04a63dc43b72516d9c92db8e13ac0d7f5e8acb767bca5d9",
    )).toBeInTheDocument();
    const blockedRow = within(strategy).getByText(blockedDecision.input_ref!).closest(".decision-row");
    if (!(blockedRow instanceof HTMLElement)) throw new Error("blocked decision row not found");
    expect(Object.keys(blockedDecision.contributions!).sort()).toEqual([
      "draft_curve",
      "late_game_style",
      "lineup_rosh",
      "market_movement",
      "player_form",
      "team_style",
    ]);
    expect(Object.keys(blockedDecision.draft_authority!)).toContain("curve_key");
    expect(Object.keys(blockedDecision.vision_authority!)).toContain("source_frame_ref");
    expect(within(blockedRow).getByText("未满足策略门槛")).toBeInTheDocument();
    expect(within(blockedRow).getByText(blockedDecision.reason)).toBeInTheDocument();
    expect(within(blockedRow).getByText("队伍风格")).toBeInTheDocument();
    expect(within(blockedRow).getByText("Δlogit +0.502")).toBeInTheDocument();
    expect(within(blockedRow).getByText("保守 +0.402")).toBeInTheDocument();
    expect(within(blockedRow).getByText("test-draft-model-v1")).toBeInTheDocument();
    expect(within(blockedRow).queryByText("没有持久化贡献项")).not.toBeInTheDocument();
    expect(within(strategy).queryByText("strategy_evidence_invalid")).not.toBeInTheDocument();
  });

  it("renders the backend no-signal blocked decision as an available explanation", () => {
    const analysis = structuredClone(goldenNoSignalAnalysis) as unknown as MatchAnalysis;
    const blocked = fixtureStrategyDecision(analysis, 0);
    const view = render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-14T14:00:00+00:00")}
        replay={false}
      />,
    );
    const section = view.container.querySelector(".decision-section");
    if (!(section instanceof HTMLElement)) throw new Error("strategy section not found");

    expect(within(section).getByText("有证据")).toBeInTheDocument();
    expect(within(section).getByText("未满足策略门槛")).toBeInTheDocument();
    expect(within(section).getByText(blocked.reason)).toBeInTheDocument();
    expect(within(section).getByText("模型 40.0%")).toBeInTheDocument();
    expect(within(section).getByText("市场 40.0%")).toBeInTheDocument();
    expect(within(section).getByText("Edge 0.0%")).toBeInTheDocument();
    expect(within(section).getByText("质量 0.0%")).toBeInTheDocument();
    expect(within(section).getByText("没有持久化贡献项")).toBeInTheDocument();
    expect(section.querySelectorAll(".decision-row")).toHaveLength(1);
    expect(within(section).queryByText("strategy_evidence_invalid")).not.toBeInTheDocument();
  });

  it("renders a fully scored blocked decision without weakening eligible validation", () => {
    const analysis = structuredClone(goldenAvailableAnalysis) as unknown as MatchAnalysis;
    const scoredBlocked = scoredBlockedDecision(analysis);
    analysis.strategy.data!.decisions = [scoredBlocked];
    analysis.strategy.data!.displayed_count = 1;
    analysis.strategy.data!.scanned_count = 1;
    analysis.strategy.data!.excluded_decision_count = 0;
    analysis.strategy.data!.excluded = {
      vision_invalidated: 0,
      mapping_impacted: 0,
      draft_conflicted: 0,
      invalid_payload: 0,
    };
    const view = render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-14T14:00:00+00:00")}
        replay={false}
      />,
    );
    const section = view.container.querySelector(".decision-section");
    if (!(section instanceof HTMLElement)) throw new Error("strategy section not found");

    expect(within(section).getByText("未满足策略门槛")).toBeInTheDocument();
    expect(within(section).getByText("edge_below_threshold")).toBeInTheDocument();
    expect(within(section).getByText("Δlogit +0.502")).toBeInTheDocument();
    expect(within(section).queryByText("strategy_evidence_invalid")).not.toBeInTheDocument();
  });

  it.each([
    ["nonzero no-signal result", (decision: StrategyDecision) => {
      decision.model_probability += 0.01;
      decision.edge = decision.model_probability - decision.market_probability;
    }],
    ["partial scored payload", (decision: StrategyDecision) => {
      decision.contributions = { team_style: 0.01 };
    }],
    ["unexpected draft authority", (decision: StrategyDecision) => {
      decision.draft_authority = { curve_key: CURVE_KEY };
    }],
    ["eligible reason", (decision: StrategyDecision) => {
      decision.reason = "eligible";
    }],
  ] as const)("rejects a blocked no-signal decision with %s", (_name, tamper) => {
    const analysis = structuredClone(goldenNoSignalAnalysis) as unknown as MatchAnalysis;
    const blocked = fixtureStrategyDecision(analysis, 0);
    tamper(blocked);
    const view = render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-14T14:00:00+00:00")}
        replay={false}
      />,
    );

    expect(screen.getAllByText("strategy_evidence_invalid").length).toBeGreaterThan(0);
    expect(view.container.querySelector(".decision-row")).not.toBeInTheDocument();
  });

  it("prefers input vision evidence and falls back to golden vision authority", () => {
    const analysis = structuredClone(goldenAvailableAnalysis) as unknown as MatchAnalysis;
    const decision = strategyDecision({
      decided_at: "2026-07-14T14:00:00+00:00",
      vision_authority: {
        aligned_game_clock_seconds: 600,
        captured_at: "2026-07-14T13:59:50+00:00",
        source_frame_ref: FRAME_REF,
      },
    });
    const inputFrameRef = `vision-frame:sha256:${"9".repeat(64)}`;
    decision.inputs = {
      ...decision.inputs,
      vision: {
        captured_at: "2026-07-14T13:59:54+00:00",
        game_clock_seconds: 660,
        source_frame_ref: inputFrameRef,
      },
    };
    analysis.strategy.data!.decisions = [decision];
    analysis.strategy.data!.displayed_count = 1;
    analysis.strategy.data!.scanned_count = 1;
    analysis.strategy.data!.excluded_decision_count = 0;
    analysis.strategy.data!.excluded = {
      vision_invalidated: 0,
      mapping_impacted: 0,
      draft_conflicted: 0,
      invalid_payload: 0,
    };
    const view = render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-14T14:00:00+00:00")}
        replay={false}
      />,
    );
    const eligibleEvidence = () => {
      const row = screen.getByText(decision.decision_key!).closest(".decision-row");
      if (!(row instanceof HTMLElement)) throw new Error("eligible decision row not found");
      return within(row).getByLabelText("持久化决策证据");
    };

    let evidence = eligibleEvidence();
    expect(within(evidence).getByText("时钟 11:00")).toBeInTheDocument();
    expect(within(evidence).getByText(inputFrameRef)).toBeInTheDocument();

    delete decision.inputs.vision;
    view.rerender(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-14T14:00:00+00:00")}
        replay={false}
      />,
    );

    evidence = eligibleEvidence();
    expect(within(evidence).getByText(/^视觉 (?!未提供).+/)).toBeInTheDocument();
    expect(within(evidence).getByText("时钟 10:00")).toBeInTheDocument();
    expect(within(evidence).getByText(FRAME_REF)).toBeInTheDocument();
  });

  it("rejects a partial eligible contribution set derived from the backend golden", () => {
    const partialAnalysis = structuredClone(goldenAvailableAnalysis) as unknown as MatchAnalysis;
    delete fixtureStrategyDecision(partialAnalysis, 1).contributions!.draft_curve;
    const view = render(
      <MatchWorkspace
        detail={detailWithAnalysis(partialAnalysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-14T14:00:00+00:00")}
        replay={false}
      />,
    );

    expect(screen.getAllByText("strategy_evidence_invalid").length).toBeGreaterThan(0);
    expect(view.container.querySelector(".decision-row")).not.toBeInTheDocument();
  });

  it.each([
    ["model probability", (decision: StrategyDecision) => {
      decision.model_probability += 0.01;
      decision.edge = decision.model_probability - decision.market_probability;
    }],
    ["one raw contribution", (decision: StrategyDecision) => {
      decision.contributions!.team_style += 0.01;
    }],
    ["one conservative contribution", (decision: StrategyDecision) => {
      decision.conservative_contributions!.team_style += 0.01;
    }],
    ["conservative probability", (decision: StrategyDecision) => {
      decision.inputs!.conservative_probability = 0.55;
    }],
    ["independent-positive flag", (decision: StrategyDecision) => {
      decision.inputs!.independent_positive = false;
    }],
    ["eligible reason", (decision: StrategyDecision) => {
      decision.reason = "eligible_tampered";
    }],
  ] as const)("rejects a golden decision with tampered %s", (_field, tamper) => {
    const analysis = structuredClone(goldenAvailableAnalysis) as unknown as MatchAnalysis;
    tamper(fixtureStrategyDecision(analysis, 1));
    const view = render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-14T14:00:00+00:00")}
        replay={false}
      />,
    );

    expect(screen.getAllByText("strategy_evidence_invalid").length).toBeGreaterThan(0);
    expect(view.container.querySelector(".decision-row")).not.toBeInTheDocument();
  });

  it("rejects team and late-game contributions that cancel without another independent positive", () => {
    const analysis = matchAnalysis();
    const decision = analysis.strategy.data!.decisions[0];
    const cancellingContributions = {
      team_style: 0.2,
      player_form: -0.03,
      draft_curve: 0,
      late_game_style: -0.2,
      market_movement: 0.1,
    };
    decision.contributions = { ...cancellingContributions };
    decision.conservative_contributions = { ...cancellingContributions };
    decision.model_probability = 0.36608734869700643;
    decision.edge = 0.016087348697006454;
    decision.inputs = {
      ...decision.inputs,
      conservative_contributions: { ...cancellingContributions },
      conservative_probability: 0.36608734869700643,
      independent_positive: false,
    };
    const view = render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getAllByText("strategy_evidence_invalid").length).toBeGreaterThan(0);
    expect(view.container.querySelector(".decision-row")).not.toBeInTheDocument();
  });

  it("rejects an amplified positive conservative contribution even with synchronized math", () => {
    const analysis = structuredClone(goldenAvailableAnalysis) as unknown as MatchAnalysis;
    const decision = fixtureStrategyDecision(analysis, 1);
    const conservative = {
      ...decision.conservative_contributions!,
      team_style: decision.contributions!.team_style + 0.01,
    };
    const score = Math.log(decision.market_probability / (1 - decision.market_probability))
      + Object.values(conservative).reduce((sum, value) => sum + value, 0);
    decision.conservative_contributions = conservative;
    decision.inputs!.conservative_contributions = { ...conservative };
    decision.inputs!.conservative_probability = 1 / (1 + Math.exp(-score));
    const view = render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-14T14:00:00+00:00")}
        replay={false}
      />,
    );

    expect(screen.getAllByText("strategy_evidence_invalid").length).toBeGreaterThan(0);
    expect(view.container.querySelector(".decision-row")).not.toBeInTheDocument();
  });

  it.each([
    ["uppercase decision key", (decision: StrategyDecision) => {
      decision.decision_key = decision.decision_key!.toUpperCase();
    }],
    ["wrong-length decision key", (decision: StrategyDecision) => {
      decision.decision_key = decision.decision_key!.slice(1);
    }],
    ["uppercase input ref", (decision: StrategyDecision) => {
      decision.input_ref = decision.input_ref!.toUpperCase();
    }],
    ["wrong-length input ref", (decision: StrategyDecision) => {
      decision.input_ref = decision.input_ref!.slice(1);
    }],
  ] as const)("rejects a golden decision with an %s", (_field, tamper) => {
    const analysis = structuredClone(goldenAvailableAnalysis) as unknown as MatchAnalysis;
    tamper(fixtureStrategyDecision(analysis, 1));
    const view = render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-14T14:00:00+00:00")}
        replay={false}
      />,
    );

    expect(screen.getAllByText("strategy_evidence_invalid").length).toBeGreaterThan(0);
    expect(view.container.querySelector(".decision-row")).not.toBeInTheDocument();
  });

  it("rejects a pseudo market contribution outside the runtime five-key set", () => {
    const analysis = matchAnalysis();
    const decision = analysis.strategy.data!.decisions[0];
    decision.contributions = { ...decision.contributions, market: 0 };
    decision.conservative_contributions = {
      ...decision.conservative_contributions,
      market: 0,
    };
    const view = render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getAllByText("strategy_evidence_invalid").length).toBeGreaterThan(0);
    expect(view.container.querySelector(".decision-row")).not.toBeInTheDocument();
  });

  it.each([
    ["waiting", "upcoming", "waiting_for_match_start", "等待开赛"],
    ["unavailable", "ended", "formal_event_not_approved", "策略分析不可用"],
    ["review", "live", "strategy_evidence_invalid", "策略分析需要人工复核"],
  ] as const)(
    "renders the %s strategy state with its real reason",
    (status, lifecycle, reason, expected) => {
      const analysis = matchAnalysis({
        strategy: analysisSection<StrategyAnalysisData>(status, reason, null),
      });
      render(
        <MatchWorkspace
          detail={detailWithAnalysis(analysis)}
          error={null}
          loading={false}
          match={{ ...match, lifecycle }}
          now={Date.parse("2026-07-16T12:02:05+00:00")}
          replay={false}
        />,
      );

      expect(screen.getAllByText(expected).length).toBeGreaterThan(0);
      expect(screen.getAllByText(reason).length).toBeGreaterThan(0);
      expect(screen.queryByText("策略就绪")).not.toBeInTheDocument();
    },
  );

  it("treats an upcoming match as waiting for start instead of evidence unavailable", () => {
    const analysis = matchAnalysis({
      strategy: analysisSection<StrategyAnalysisData>("waiting", "waiting_for_match_start", null),
    });
    const view = render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={{ ...match, lifecycle: "upcoming" }}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    const overview = view.getByLabelText("当前策略结论与入场链路");
    expect(within(overview).getByText("等待开赛")).toBeInTheDocument();
    expect(within(overview).getByText("尚未开赛")).toBeInTheDocument();
    expect(within(overview).getByText("等待比赛开始并采集可信 HUD。")).toBeInTheDocument();
    expect(within(overview).getByText(/下一步 等待比赛开始并采集可信 HUD/)).toBeInTheDocument();
    expect(within(overview).getByText(/最近数据/)).toBeInTheDocument();
    expect(within(overview).queryByText("证据不可用")).not.toBeInTheDocument();
  });

  it("shows exact mapping, vision, lineup and strategy blockers in the source summary", () => {
    const analysis = matchAnalysis({
      vision: analysisSection<VisionAnalysisData>("waiting", "waiting_for_confirmed_vision", null),
      lineup: analysisSection<LineupAnalysisData>("waiting", "waiting_for_complete_draft", null),
      strategy: analysisSection<StrategyAnalysisData>("waiting", "waiting_for_strategy_inputs", null),
    });
    const blocked = detailWithAnalysis(analysis);
    blocked.readiness = {
      ...blocked.readiness,
      mapping: {
        status: "missing",
        count: 0,
        total_count: 0,
        reasons: ["strict_mapping_missing"],
      },
    };

    render(
      <MatchWorkspace
        detail={blocked}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    for (const reason of [
      "strict_mapping_missing",
      "waiting_for_confirmed_vision",
      "waiting_for_complete_draft",
      "waiting_for_strategy_inputs",
    ]) {
      expect(screen.getAllByText(reason).length).toBeGreaterThan(0);
    }
    expect(screen.getByText("策略进程运行中")).toBeInTheDocument();
    expect(screen.getByText("系统诊断")).toBeInTheDocument();
    expect(screen.queryByText("策略就绪")).not.toBeInTheDocument();
  });

  it("keeps a legacy decision visible but fails closed on invalid contribution JSON", () => {
    const legacy = detail(1, "ready");
    legacy.decisions = [strategyDecision({
      contributions: undefined,
      conservative_contributions: undefined,
      inputs: undefined,
      contributions_json: "{",
    })];

    render(
      <MatchWorkspace
        detail={legacy}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getByText(/仅显示旧契约记录/)).toBeInTheDocument();
    expect(screen.getByText("贡献证据无效")).toBeInTheDocument();
    expect(screen.getByText("invalid_contributions_json")).toBeInTheDocument();
    expect(screen.getAllByText("analysis_contract_unavailable").length).toBeGreaterThan(0);
    expect(screen.queryByText("策略就绪")).not.toBeInTheDocument();
  });

  it("rejects legacy contribution JSON larger than 64 KiB before parsing", () => {
    const legacy = detail(1, "ready");
    legacy.decisions = [strategyDecision({
      contributions: undefined,
      conservative_contributions: undefined,
      inputs: undefined,
      contributions_json: JSON.stringify({ padding: "x".repeat(64 * 1024) }),
    })];

    render(
      <MatchWorkspace
        detail={legacy}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getByText("contributions_json_too_large")).toBeInTheDocument();
    expect(screen.queryByLabelText("策略贡献")).not.toBeInTheDocument();
  });

  it("renders trusted five-versus-five heroes and labels every curve value as conditional", () => {
    render(
      <MatchWorkspace
        detail={detailWithAnalysis()}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    const radiant = screen.getByLabelText("Radiant 阵容");
    const dire = screen.getByLabelText("Dire 阵容");
    expect(within(radiant).getByText("Anti-Mage")).toBeInTheDocument();
    expect(within(radiant).getByText("ID 1")).toBeInTheDocument();
    expect(within(dire).getByText("英雄 10")).toBeInTheDocument();
    expect(screen.getByText("达到 10 分钟")).toBeInTheDocument();
    expect(screen.getByText("当前可用检查点")).toBeInTheDocument();
    expect(screen.queryByText(/策略可用检查点/)).not.toBeInTheDocument();
    expect(screen.getByText("达到 20 分钟")).toBeInTheDocument();
    expect(screen.getByText("未来条件点")).toBeInTheDocument();
    expect(screen.getAllByText(/条件胜率/).length).toBeGreaterThan(1);
    expect(screen.queryByText("当前胜率", { exact: true })).not.toBeInTheDocument();
    expect(screen.getByText(/实时选手身份不可用/)).toBeInTheDocument();
    expect(screen.getByText("live_player_identity_unavailable")).toBeInTheDocument();
  });

  it("shows pure and player-adjusted Rosh scores in the lineup and evidence summary", () => {
    const scored = lineupData({
      scores: analysisSection("available", "rosh_lineup_score_available", {
        pure_lineup_score: 3.2,
        player_adjusted_lineup_score: 4.1,
        effective_lineup_score: 4.1,
        mode: "player_adjusted",
        player_coverage: 1,
        player_coverage_count: 10,
        stake_multiplier: 1,
        formula_version: "dematus-rosh-v1",
        source_as_of: "2026-07-16T11:58:00+00:00",
        score_key: ROSH_SCORE_KEY,
        player_identity_hash: ROSH_PLAYER_IDENTITY_HASH,
        evidence_hash: ROSH_EVIDENCE_HASH,
        stake_cap: 1,
      }),
      players: analysisSection("available", "rosh_player_identity_available", {
        players: Array.from({ length: 10 }, (_, slot) => ({
          steam_account_id: 1000 + slot,
          side: slot < 5 ? "radiant" as const : "dire" as const,
          position: (slot % 5) + 1,
          hero_id: slot + 1,
          status: "resolved" as const,
        })),
      }),
    });
    render(
      <MatchWorkspace
        detail={detailWithAnalysis(matchAnalysis({
          lineup: analysisSection("available", "lineup_available", scored),
        }))}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getByText("纯阵容评分")).toBeInTheDocument();
    expect(screen.getByText("选手修正后实际阵容评分")).toBeInTheDocument();
    expect(screen.getByText("+3.2 pp")).toBeInTheDocument();
    expect(screen.getAllByText("+4.1 pp").length).toBeGreaterThan(1);
    expect(screen.getAllByText(/选手覆盖 100\.0%/).length).toBeGreaterThan(1);
    expect(screen.getByText("选手修正评分")).toBeInTheDocument();
    expect(screen.getByText(ROSH_SCORE_KEY)).toBeInTheDocument();
    expect(screen.getByText(ROSH_EVIDENCE_HASH)).toBeInTheDocument();
    expect(screen.getByText("Steam 1000")).toBeInTheDocument();
    expect(screen.getAllByText("已用于选手修正")).toHaveLength(10);
  });

  it("marks a pure-score fallback as unavailable player correction and half stake", () => {
    const fallback = lineupData({
      scores: analysisSection("available", "rosh_lineup_score_available", {
        pure_lineup_score: -2.4,
        player_adjusted_lineup_score: null,
        effective_lineup_score: -2.4,
        mode: "pure",
        player_coverage: 0.6,
        player_coverage_count: 6,
        stake_multiplier: 0.5,
        formula_version: "dematus-rosh-v1",
        source_as_of: "2026-07-16T11:58:00+00:00",
        score_key: ROSH_SCORE_KEY,
        player_identity_hash: ROSH_PLAYER_IDENTITY_HASH,
        evidence_hash: ROSH_EVIDENCE_HASH,
        stake_cap: 0.5,
      }),
      players: analysisSection("available", "rosh_player_identity_partial", {
        players: Array.from({ length: 10 }, (_, slot) => ({
          steam_account_id: slot < 8 ? 2000 + slot : null,
          side: slot < 5 ? "radiant" as const : "dire" as const,
          position: (slot % 5) + 1,
          hero_id: slot + 1,
          status: slot < 6
            ? "resolved" as const
            : slot < 8
              ? "selected_unresolved" as const
              : "unavailable" as const,
        })),
      }),
    });
    render(
      <MatchWorkspace
        detail={detailWithAnalysis(matchAnalysis({
          lineup: analysisSection("available", "lineup_available", fallback),
        }))}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getAllByText("不可用").length).toBeGreaterThan(0);
    expect(screen.getByText(/回退采用纯阵容评分 · 仓位上限 50\.0%/)).toBeInTheDocument();
    expect(screen.getByText("纯阵容回退")).toBeInTheDocument();
    expect(screen.getByText(/纯阵容半仓回退/)).toBeInTheDocument();
    expect(screen.getByText("Steam 2006")).toBeInTheDocument();
    expect(screen.getAllByText("身份可信，修正数据不可用")).toHaveLength(2);
  });

  it("shows waiting instead of fabricating a missing Rosh score", () => {
    const waiting = lineupData({
      scores: analysisSection<RoshLineupScoresData>("waiting", "rosh_lineup_score_pending", null),
    });
    render(
      <MatchWorkspace
        detail={detailWithAnalysis(matchAnalysis({
          lineup: analysisSection("available", "lineup_available", waiting),
        }))}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getAllByText("rosh_lineup_score_pending").length).toBeGreaterThan(0);
    expect(screen.queryByText("纯阵容评分")).not.toBeInTheDocument();
    expect(screen.queryByText("选手修正后实际阵容评分")).not.toBeInTheDocument();
  });

  it("fails closed for duplicate heroes and invalid active curve points", () => {
    const invalidLineup = lineupData({
      dire: { hero_ids: [5, 6, 7, 8, 9] },
    });
    const view = render(
      <MatchWorkspace
        detail={detailWithAnalysis(matchAnalysis({
          lineup: analysisSection("available", "lineup_available", invalidLineup),
        }))}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getByText("阵容分析需要人工复核")).toBeInTheDocument();
    expect(screen.getAllByText("lineup_payload_invalid").length).toBeGreaterThan(0);
    expect(screen.queryByLabelText("Radiant 阵容")).not.toBeInTheDocument();

    const invalidCurve = lineupData();
    const curveData = invalidCurve.active_curve.data!;
    invalidCurve.active_curve = analysisSection("available", "active_curve_available", {
      ...curveData,
      points: curveData.points.map((point, index) => (
        index === 0 ? { ...point, conditional: true, active: true } : point
      )),
    });
    view.rerender(
      <MatchWorkspace
        detail={detailWithAnalysis(matchAnalysis({
          lineup: analysisSection("available", "lineup_available", invalidCurve),
        }))}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getByText("阵容曲线需要人工复核")).toBeInTheDocument();
    expect(screen.getAllByText("lineup_curve_payload_invalid").length).toBeGreaterThan(0);
    expect(screen.queryByText("当前可用检查点")).not.toBeInTheDocument();
  });

  it("does not retain lineup evidence when the selected match changes", () => {
    const firstDetail = detailWithAnalysis();
    const secondLineup = lineupData({
      radiant: { hero_ids: [11, 12, 13, 14, 15] },
      dire: { hero_ids: [16, 17, 18, 19, 20] },
    });
    const secondDetail = {
      ...detailWithAnalysis(matchAnalysis({
        lineup: analysisSection("available", "lineup_available", secondLineup),
      })),
      raybet_match_id: "match-2",
      team_one: "Second One",
      team_two: "Second Two",
    };
    const view = render(
      <MatchWorkspace
        detail={firstDetail}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );
    expect(screen.getByText("Anti-Mage")).toBeInTheDocument();

    view.rerender(
      <MatchWorkspace
        detail={secondDetail}
        error={null}
        loading={false}
        match={{
          ...match,
          raybet_match_id: "match-2",
          team_one: "Second One",
          team_two: "Second Two",
        }}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.queryByText("Anti-Mage")).not.toBeInTheDocument();
    expect(screen.getByText("英雄 11")).toBeInTheDocument();
    expect(screen.getByLabelText("Second One 阵容")).toBeInTheDocument();
  });

  it("keeps a valid lineup visible and fails the curve closed when vision is missing", () => {
    const analysis = matchAnalysis();
    delete (analysis as Partial<MatchAnalysis>).vision;

    expect(() => render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    )).not.toThrow();

    expect(screen.getByLabelText("Radiant 阵容")).toBeInTheDocument();
    expect(screen.getByText("阵容曲线需要人工复核")).toBeInTheDocument();
    expect(screen.getAllByText("lineup_curve_clock_unavailable").length).toBeGreaterThan(0);
  });

  it("fails closed when optional authoritative hero metadata is not a valid array", () => {
    const malformed = lineupData({
      radiant: {
        hero_ids: [1, 2, 3, 4, 5],
        heroes: "not-an-array" as unknown as LineupAnalysisData["radiant"]["heroes"],
      },
    });
    render(
      <MatchWorkspace
        detail={detailWithAnalysis(matchAnalysis({
          lineup: analysisSection("available", "lineup_available", malformed),
        }))}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getByText("阵容分析需要人工复核")).toBeInTheDocument();
    expect(screen.getAllByText("lineup_payload_invalid").length).toBeGreaterThan(0);
  });

  it("normalizes malformed available odds and vision data before reading it", () => {
    const unsafeRef = "https://evil.example/private-frame";
    const analysis = matchAnalysis({
      odds: analysisSection("available", "odds_available", {
        point_count: Number.NaN,
        periods: ["map_1"],
        latest_observed_at: "2099-01-01T00:00:00+00:00",
      }),
      vision: analysisSection("available", "vision_available", {
        map_number: 1,
        captured_at: "2099-01-01T00:00:00+00:00",
        game_clock_seconds: 720,
        clock_confidence: 0.98,
        draft_confidence: 0.96,
        source_frame_ref: unsafeRef,
      }),
    });
    render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getByText("odds_payload_invalid")).toBeInTheDocument();
    expect(screen.getByText("vision_payload_invalid")).toBeInTheDocument();
    expect(within(sourceRow("赔率")).queryByText("odds_payload_invalid")).not.toBeInTheDocument();
    expect(within(sourceRow("视觉时钟")).queryByText("vision_payload_invalid")).not.toBeInTheDocument();
    expect(screen.queryByText(unsafeRef)).not.toBeInTheDocument();
    expect(screen.queryByText(/2099/)).not.toBeInTheDocument();
  });

  it("ignores stale data carried by non-available analysis sections", () => {
    const analysis = matchAnalysis({
      odds: analysisSection("waiting", "odds_waiting", {
        point_count: 999,
        periods: ["map_9"],
        latest_observed_at: "2099-01-01T00:00:00+00:00",
      }),
      vision: analysisSection("review", "vision_review", {
        map_number: 9,
        captured_at: "2099-01-01T00:00:00+00:00",
        game_clock_seconds: 9999,
        clock_confidence: 1,
        draft_confidence: 1,
        source_frame_ref: `vision-frame:sha256:${"9".repeat(64)}`,
      }),
    });
    render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    expect(within(sourceRow("赔率")).queryByText(/999/)).not.toBeInTheDocument();
    expect(within(sourceRow("视觉时钟")).queryByText(/2099|9999/)).not.toBeInTheDocument();
    expect(screen.getByText("odds_waiting")).toBeInTheDocument();
    expect(screen.getByText("vision_review")).toBeInTheDocument();
  });

  it.each([
    ["waiting", "upcoming", "waiting_for_complete_draft", "等待开赛"],
    ["review", "live", "draft_conflict", "曲线需复核"],
  ] as const)(
    "inherits lineup %s state into the curve source row",
    (status, lifecycle, reason, expected) => {
      render(
        <MatchWorkspace
          detail={detailWithAnalysis(matchAnalysis({
            lineup: analysisSection<LineupAnalysisData>(status, reason, null),
          }))}
          error={null}
          loading={false}
          match={{ ...match, lifecycle }}
          now={Date.parse("2026-07-16T12:02:05+00:00")}
          replay={false}
        />,
      );

      const row = sourceRow("阵容曲线");
      expect(within(row).getByText(expected)).toBeInTheDocument();
      expect(screen.getAllByText(reason).length).toBeGreaterThan(0);
    },
  );

  it.each([
    "future_not_conditional",
    "active_is_future",
    "past_marked_conditional",
  ] as const)("rejects a curve whose clock relationship is %s", (kind) => {
    const lineup = lineupData();
    const curve = lineup.active_curve.data!;
    if (kind === "future_not_conditional") {
      curve.points[1] = { ...curve.points[1], conditional: false };
    } else if (kind === "active_is_future") {
      curve.active_horizon_minutes = 20;
      curve.points[0] = { ...curve.points[0], active: false };
      curve.points[1] = { ...curve.points[1], active: true, conditional: false };
    } else {
      curve.points[0] = { ...curve.points[0], conditional: true };
    }
    render(
      <MatchWorkspace
        detail={detailWithAnalysis(matchAnalysis({
          lineup: analysisSection("available", "lineup_available", lineup),
        }))}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getByText("阵容曲线需要人工复核")).toBeInTheDocument();
    expect(screen.getAllByText("lineup_curve_payload_invalid").length).toBeGreaterThan(0);
  });

  it.each([
    ["out of range", 999],
    ["inconsistent with probabilities", 0.2],
  ] as const)("rejects a strategy edge that is %s", (_kind, edge) => {
    const analysis = matchAnalysis({
      strategy: analysisSection("available", "strategy_available", {
        decisions: [strategyDecision({ edge })],
        displayed_count: 1,
        scanned_count: 1,
        count_scope: "recent_scanned_window",
        has_more: false,
        truncated: false,
        excluded_decision_count: 0,
        excluded: {
          vision_invalidated: 0,
          mapping_impacted: 0,
          draft_conflicted: 0,
          invalid_payload: 0,
        },
      }),
    });
    const view = render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getAllByText("strategy_evidence_invalid").length).toBeGreaterThan(0);
    expect(view.container.querySelector(".decision-row")).not.toBeInTheDocument();
  });

  it.each([
    ["ready", "进程运行", "策略进程运行中"],
    ["delayed", "延迟", "策略进程心跳延迟"],
    ["stale", "陈旧", "策略进程心跳陈旧"],
    ["missing", "缺失", "策略进程心跳缺失"],
    ["invalid", "数据无效", "策略进程数据无效"],
    ["unconfirmed", "等待确认", "策略进程等待确认"],
    ["degraded", "降级", "策略进程降级运行"],
    ["unhealthy", "故障", "策略进程运行异常"],
    ["stopped", "未运行", "策略进程未运行"],
    ["future_status", "状态未知", "策略进程状态未知"],
  ] as const)("shows shadow worker freshness %s precisely", (status, label, description) => {
    const value = detailWithAnalysis();
    value.readiness = {
      ...value.readiness,
      strategy: {
        status: status as MonitorMatch["readiness"]["strategy"]["status"],
      },
    };
    render(
      <MatchWorkspace
        detail={value}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    const row = sourceRow("策略进程");
    expect(within(row).getByText(label)).toBeInTheDocument();
    expect(within(row).getByText(description)).toBeInTheDocument();
  });

  it("labels the displayed strategy window without claiming a persisted total", () => {
    const analysis = matchAnalysis();
    analysis.strategy.data!.scanned_count = 1000;
    analysis.strategy.data!.count_scope = "recent_scanned_window";
    analysis.strategy.data!.has_more = true;
    analysis.strategy.data!.truncated = true;
    render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getByText("显示最近 1 条")).toBeInTheDocument();
    expect(screen.getByText("扫描范围：最近 1000 条候选记录")).toBeInTheDocument();
    expect(screen.getByText("还有更早的策略记录未显示。")).toBeInTheDocument();
    expect(screen.getByText("后端结果已截断。")).toBeInTheDocument();
    expect(screen.queryByText(/持久化总数|条持久化记录/)).not.toBeInTheDocument();
  });

  it.each([
    ["displayed count", { displayed_count: 2 }],
    ["scanned count", { scanned_count: 0 }],
    ["count scope", { count_scope: "scanned" }],
    ["unique excluded count", { excluded_decision_count: -1 }],
    ["reason count above the unique excluded count", {
      scanned_count: 2,
      excluded_decision_count: 1,
      excluded: {
        vision_invalidated: 2,
        mapping_impacted: 0,
        draft_conflicted: 0,
        invalid_payload: 0,
      },
    }],
    ["reason counts that do not cover the unique excluded count", {
      scanned_count: 3,
      excluded_decision_count: 2,
      excluded: {
        vision_invalidated: 1,
        mapping_impacted: 0,
        draft_conflicted: 0,
        invalid_payload: 0,
      },
    }],
  ] as const)("rejects malformed strategy %s metadata", (_name, invalid) => {
    const analysis = matchAnalysis();
    Object.assign(analysis.strategy.data!, invalid);
    const view = render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getAllByText("strategy_evidence_invalid").length).toBeGreaterThan(0);
    expect(view.container.querySelector(".decision-row")).not.toBeInTheDocument();
  });

  it("keeps the backend invalid-only scan window available without double-counting exclusions", () => {
    const analysis = matchAnalysis();
    const strategy = analysis.strategy.data!;
    strategy.scanned_count = 201;
    strategy.excluded_decision_count = 200;
    strategy.excluded = {
      vision_invalidated: 0,
      mapping_impacted: 0,
      draft_conflicted: 0,
      invalid_payload: 200,
    };
    const view = render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );
    const section = view.container.querySelector(".decision-section");
    if (!(section instanceof HTMLElement)) throw new Error("strategy section not found");

    expect(within(section).getByText("有证据")).toBeInTheDocument();
    expect(within(section).getByText("扫描范围：最近 201 条候选记录")).toBeInTheDocument();
    expect(section.querySelectorAll(".decision-row")).toHaveLength(1);
    expect(within(section).queryByText("strategy_evidence_invalid")).not.toBeInTheDocument();
    expect(within(sourceRow("策略输出")).getByText(/200 条唯一排除/)).toBeInTheDocument();
    expect(within(sourceRow("策略输出")).getByText(/无效载荷 200/)).toBeInTheDocument();
  });

  it("shows a unique excluded count and labels overlapping reason details", () => {
    const analysis = matchAnalysis();
    const strategy = analysis.strategy.data!;
    strategy.scanned_count = 3;
    strategy.excluded_decision_count = 2;
    strategy.excluded = {
      vision_invalidated: 2,
      mapping_impacted: 2,
      draft_conflicted: 1,
      invalid_payload: 0,
    };
    render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    const output = sourceRow("策略输出");
    expect(within(output).getByText(/2 条唯一排除/)).toBeInTheDocument();
    expect(within(output).getByText(/原因明细（可重叠）：视觉失效 2、映射影响 2、阵容冲突 1、无效载荷 0/)).toBeInTheDocument();
    expect(within(output).queryByText(/5 条排除/)).not.toBeInTheDocument();
    expect(within(sourceRow("模型输入")).getByText(
      "当前显示决策中 1 条有持久化输入",
    )).toBeInTheDocument();
  });

  it.each([
    "https://evil.example/decision",
    "analysis-decision?token=secret",
    "analysis-decision#fragment",
    "analysis-decision\u0000control",
  ])("rejects unsafe opaque strategy reference %s", (unsafeRef) => {
    const analysis = matchAnalysis();
    analysis.strategy.data!.decisions = [strategyDecision({ decision_key: unsafeRef })];
    render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getAllByText("strategy_evidence_invalid").length).toBeGreaterThan(0);
    expect(screen.queryByText(unsafeRef)).not.toBeInTheDocument();
  });

  it("rejects arbitrary URL-shaped frame and evidence references", () => {
    const unsafeRef = "https://evil.example/secret";
    const badDecision = strategyDecision({
      input_ref: unsafeRef,
      vision_authority: { source_frame_ref: unsafeRef },
      inputs: {
        vision: {
          captured_at: "2026-07-16T12:01:58+00:00",
          game_clock_seconds: 720,
          source_frame_ref: unsafeRef,
        },
      },
    });
    const badLineup = lineupData();
    badLineup.evidence.anchor_source_frame_ref = unsafeRef;
    const analysis = matchAnalysis({
      vision: analysisSection("available", "vision_available", {
        ...matchAnalysis().vision.data!,
        source_frame_ref: unsafeRef,
      }),
      strategy: analysisSection("available", "strategy_available", {
        decisions: [badDecision],
        displayed_count: 1,
        scanned_count: 1,
        count_scope: "recent_scanned_window",
        has_more: false,
        truncated: false,
        excluded_decision_count: 0,
        excluded: {
          vision_invalidated: 0,
          mapping_impacted: 0,
          draft_conflicted: 0,
          invalid_payload: 0,
        },
      }),
      lineup: analysisSection("available", "lineup_available", badLineup),
    });
    render(
      <MatchWorkspace
        detail={detailWithAnalysis(analysis)}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:02:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.queryByText(unsafeRef)).not.toBeInTheDocument();
    expect(screen.getAllByText("vision_payload_invalid").length).toBeGreaterThan(0);
    expect(screen.getAllByText("strategy_evidence_invalid").length).toBeGreaterThan(0);
    expect(screen.getAllByText("lineup_payload_invalid").length).toBeGreaterThan(0);
  });

  it("shares the replay map selection with the exact postmatch panel", () => {
    const replayDetail = detail(1, "ready");
    replayDetail.lifecycle = "ended";
    replayDetail.history_eligible = true;
    replayDetail.winner_timeline = [
      {
        observed_at: "2026-07-16T12:00:00+00:00",
        period: "map_1",
        prices: { team_one: 1.8, team_two: 2.1 },
        probabilities: { team_one: 0.54, team_two: 0.46 },
        status: { team_one: "open", team_two: "open" },
      },
      {
        observed_at: "2026-07-16T13:00:00+00:00",
        period: "map_2",
        prices: { team_one: 1.7, team_two: 2.2 },
        probabilities: { team_one: 0.57, team_two: 0.43 },
        status: { team_one: "open", team_two: "open" },
      },
    ];

    const view = render(
      <MatchWorkspace
        detail={replayDetail}
        error={null}
        loading={false}
        match={replayDetail}
        now={Date.parse("2026-07-16T14:00:00+00:00")}
        replay
      />,
    );

    expect(screen.getByText("postmatch-map-2")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "probability-chart" }));
    expect(screen.getByText("postmatch-map-1")).toBeInTheDocument();

    const nextMatch = { ...replayDetail, raybet_match_id: "match-2" };
    view.rerender(
      <MatchWorkspace
        detail={nextMatch}
        error={null}
        loading={false}
        match={nextMatch}
        now={Date.parse("2026-07-16T14:00:00+00:00")}
        replay
      />,
    );
    expect(screen.getByText("postmatch-map-2")).toBeInTheDocument();
  });
});
