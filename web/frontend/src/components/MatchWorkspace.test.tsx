import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import goldenAvailableAnalysis from "../../../../tests/fixtures/monitor-analysis-available.json";
import goldenNoSignalAnalysis from "../../../../tests/fixtures/monitor-analysis-no-signal.json";
import type {
  AnalysisSection,
  AnalysisSectionStatus,
  LineupAnalysisData,
  LivePlayerIdentityData,
  MatchAnalysis,
  MatchDetail,
  MonitorMatch,
  StrategyAnalysisData,
  StrategyDecision,
  VisionAnalysisData,
} from "../types";


vi.mock("@fluentui/react-components", () => ({
  Badge: ({ children }: { children: ReactNode }) => <span>{children}</span>,
  Button: ({
    as,
    children,
    href,
  }: {
    as?: string;
    children: ReactNode;
    href?: string;
  }) => as === "a" ? <a href={href}>{children}</a> : <button>{children}</button>,
  Skeleton: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SkeletonItem: () => <div />,
}));
vi.mock("./ProbabilityChart", () => ({
  ProbabilityChart: ({ onPeriodChange }: { onPeriodChange: (value: string) => void }) => (
    <button onClick={() => onPeriodChange("map_1")}>probability-chart</button>
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
    scores: analysisSection("waiting", "rosh_lineup_score_pending", null),
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

function sourceRow(label: string): HTMLElement {
  const row = screen.getAllByText(label)
    .map((element) => element.closest(".source-status-row"))
    .find((element): element is HTMLElement => element instanceof HTMLElement);
  if (!(row instanceof HTMLElement)) throw new Error(`source row not found: ${label}`);
  return row;
}

describe("MatchWorkspace", () => {
  afterEach(cleanup);

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
    expect(within(eligibleRow).getByText("eligible")).toBeInTheDocument();
    expect(within(eligibleRow).getByText(inputRef)).toBeInTheDocument();
    expect(within(eligibleRow).getByText("队伍风格")).toBeInTheDocument();
    expect(within(eligibleRow).getByText("Δlogit +0.650")).toBeInTheDocument();
    expect(within(eligibleRow).getByText("保守 +0.520")).toBeInTheDocument();
    expect(within(eligibleRow).getByText("模型 57.8%")).toBeInTheDocument();
    expect(within(eligibleRow).getByText("市场 40.0%")).toBeInTheDocument();
    expect(within(eligibleRow).getByText("Edge +17.8%")).toBeInTheDocument();
    expect(within(eligibleRow).getByText("质量 90.0%")).toBeInTheDocument();
    expect(within(eligibleRow).getByText("test-draft-model-v1")).toBeInTheDocument();
    expect(within(strategy).getByText("扫描范围：最近 2 条候选记录")).toBeInTheDocument();
    const evidence = within(eligibleRow).getByLabelText("持久化决策证据");
    expect(within(evidence).getByText(/^视觉 (?!未提供).+/)).toBeInTheDocument();
    expect(within(evidence).getByText("时钟 10:00")).toBeInTheDocument();
    expect(within(evidence).getByText(
      "vision-frame:sha256:7f75e66f0d00cc074e26ac35e66da4aa8fcb9473947a2fb5bc334aa2bbec85a7",
    )).toBeInTheDocument();
    const blockedRow = within(strategy).getByText(blockedDecision.input_ref!).closest(".decision-row");
    if (!(blockedRow instanceof HTMLElement)) throw new Error("blocked decision row not found");
    expect(Object.keys(blockedDecision.contributions!).sort()).toEqual([
      "draft_curve",
      "late_game_style",
      "market_movement",
      "player_form",
      "team_style",
    ]);
    expect(Object.keys(blockedDecision.draft_authority!)).toContain("curve_key");
    expect(Object.keys(blockedDecision.vision_authority!)).toContain("source_frame_ref");
    expect(within(blockedRow).getByText("未满足策略门槛")).toBeInTheDocument();
    expect(within(blockedRow).getByText(blockedDecision.reason)).toBeInTheDocument();
    expect(within(blockedRow).getByText("队伍风格")).toBeInTheDocument();
    expect(within(blockedRow).getByText("Δlogit +0.650")).toBeInTheDocument();
    expect(within(blockedRow).getByText("保守 +0.520")).toBeInTheDocument();
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
    expect(within(section).getByText("Δlogit +0.650")).toBeInTheDocument();
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
    const decision = fixtureStrategyDecision(analysis, 1);
    const inputFrameRef = `vision-frame:sha256:${"9".repeat(64)}`;
    decision.inputs = {
      ...decision.inputs,
      vision: {
        captured_at: "2026-07-14T13:59:54+00:00",
        game_clock_seconds: 660,
        source_frame_ref: inputFrameRef,
      },
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
    expect(within(evidence).getByText(
      "vision-frame:sha256:7f75e66f0d00cc074e26ac35e66da4aa8fcb9473947a2fb5bc334aa2bbec85a7",
    )).toBeInTheDocument();
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
    expect(screen.getByText("shadow_worker 运行中")).toBeInTheDocument();
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
      scores: analysisSection("waiting", "rosh_lineup_score_pending", null),
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

    expect(within(sourceRow("赔率")).getByText("odds_payload_invalid")).toBeInTheDocument();
    expect(within(sourceRow("视觉时钟")).getByText("vision_payload_invalid")).toBeInTheDocument();
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
    expect(within(sourceRow("赔率")).getByText("odds_waiting")).toBeInTheDocument();
    expect(within(sourceRow("视觉时钟")).getByText("vision_review")).toBeInTheDocument();
  });

  it.each([
    ["waiting", "upcoming", "waiting_for_complete_draft", "等待开赛"],
    ["review", "live", "draft_conflict", "需复核"],
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
      expect(within(row).getByText(reason)).toBeInTheDocument();
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
    ["ready", "进程运行", "shadow_worker 运行中"],
    ["delayed", "延迟", "shadow_worker 心跳延迟"],
    ["stale", "陈旧", "shadow_worker 心跳陈旧"],
    ["missing", "缺失", "shadow_worker 心跳缺失"],
    ["invalid", "数据无效", "shadow_worker 数据无效"],
    ["unconfirmed", "等待确认", "shadow_worker 等待确认"],
    ["degraded", "降级", "shadow_worker 降级"],
    ["unhealthy", "故障", "shadow_worker 故障"],
    ["stopped", "未运行", "shadow_worker 未运行"],
    ["future_status", "状态未知", "shadow_worker 状态未知"],
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
