import { FluentProvider, webDarkTheme } from "@fluentui/react-components";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import {
  fetchIntelligenceMatchDetail,
  fetchIntelligenceMatches,
  fetchIntelligenceOverview,
  fetchIntelligencePlayers,
  fetchIntelligenceTeams,
} from "../api";
import type {
  IntelligenceDraftQualitySlice,
  IntelligenceOverview,
} from "../types";
import { IntelligenceDashboard } from "./IntelligenceDashboard";

vi.mock("../api", () => ({
  fetchIntelligenceMatchDetail: vi.fn(),
  fetchIntelligenceMatches: vi.fn(),
  fetchIntelligenceOverview: vi.fn(),
  fetchIntelligencePlayers: vi.fn(),
  fetchIntelligenceTeams: vi.fn(),
}));

const overviewMock = vi.mocked(fetchIntelligenceOverview);
const matchesMock = vi.mocked(fetchIntelligenceMatches);
const detailMock = vi.mocked(fetchIntelligenceMatchDetail);
const playersMock = vi.mocked(fetchIntelligencePlayers);
const teamsMock = vi.mocked(fetchIntelligenceTeams);

const pagination = { page: 1, page_size: 12, total: 0, total_pages: 1 };

beforeAll(() => {
  Object.defineProperty(globalThis, "NodeFilter", { value: window.NodeFilter });
});

afterEach(() => cleanup());

beforeEach(() => {
  vi.clearAllMocks();
  overviewMock.mockResolvedValue(overview());
  matchesMock.mockResolvedValue({ data: [], pagination });
  playersMock.mockResolvedValue({ data: [], pagination });
  teamsMock.mockResolvedValue({ data: [] });
});

describe("IntelligenceDashboard", () => {
  it("keeps every calibration status distinct and shows the complete gate", async () => {
    overviewMock.mockResolvedValue({
      ...overview(),
      draft_quality: [
        quality({
          horizon_minutes: 0,
          status: "passed",
        }),
        quality({
          horizon_minutes: 10,
          status: "failed",
          brier_score: 0.257,
          gate_failures: ["brier_not_below_0.25"],
        }),
        quality({
          horizon_minutes: 20,
          status: "unsupported",
          support: 47,
          gate_failures: ["support_below_100"],
        }),
        quality({
          horizon_minutes: 30,
          status: "provisional",
          ece_90_upper: null,
          gate_failures: ["ece_upper_bound_missing"],
        }),
        quality({
          horizon_minutes: 40,
          availability_mode: "prospective",
          availability_status: "missing",
          status: "missing",
          support: 0,
          gate_failures: ["prospective_data_missing"],
        }),
      ],
    });
    renderDashboard();
    fireEvent.click(screen.getByRole("tab", { name: /阵容校准/ }));

    expect((await screen.findAllByText("player-score-v3+observed-role=role-v1")).length).toBe(2);
    expect(screen.getByText("前瞻验证 尚未建立")).toBeInTheDocument();
    expect(screen.getByText(/ECE 90% bootstrap 上界不高于 0.15/)).toBeInTheDocument();
    expect(screen.getByText("ECE 90% 上界")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("1 个切片明确未通过门槛");
    expect(screen.getByText(/校准状态暂定：1 个切片/)).toBeInTheDocument();
    expect(screen.getByText(/校准暂不支持：1 个切片/)).toBeInTheDocument();
    expect(screen.getByText("通过")).toBeInTheDocument();
    expect(screen.getByText("未通过")).toHaveAttribute(
      "title",
      expect.stringContaining("Brier 未低于 0.25"),
    );
    expect(screen.getByText("样本不足")).toHaveAttribute(
      "title",
      expect.stringContaining("样本少于 100"),
    );
    expect(screen.getByText("暂定")).toHaveAttribute(
      "title",
      expect.stringContaining("ECE 90% 上界缺失"),
    );
    expect(screen.getByText("无数据")).toHaveAttribute(
      "title",
      expect.stringContaining("前瞻数据尚未建立"),
    );
  });

  it("loads match states, player scores, and draft prediction modes", async () => {
    matchesMock.mockResolvedValue({
      data: [{
        match_id: 9001,
        radiant_team_id: 101,
        dire_team_id: 202,
        radiant_team_name: "Aurora",
        dire_team_name: "Beacon",
        radiant_win: true,
        duration: 2400,
        start_time: 1_752_500_000,
        leagueid: 77,
        league_name: "Strict Invitational",
        radiant_score: 31,
        dire_score: 18,
        radiant_state: state("radiant", "comeback", 101),
        dire_state: state("dire", "throw", 202),
      }],
      pagination: { ...pagination, total: 1 },
    });
    detailMock.mockResolvedValue({
      match: {
        match_id: 9001,
        radiant_team_id: 101,
        dire_team_id: 202,
        radiant_team_name: "Aurora",
        dire_team_name: "Beacon",
        radiant_win: true,
        duration: 2400,
        start_time: 1_752_500_000,
        leagueid: 77,
        league_name: "Strict Invitational",
        radiant_score: 31,
        dire_score: 18,
      },
      radiant_state: state("radiant", "comeback", 101),
      dire_state: state("dire", "throw", 202),
      player_scores: [{
        player_slot: 0,
        account_id: 11,
        player_name: "River",
        team_id: 101,
        side: "radiant",
        hero_id: 2,
        hero_name: "Axe",
        performance: {
          kills: 8,
          deaths: 2,
          assists: 11,
          gold_per_min: 650,
          xp_per_min: 720,
          net_worth: 21000,
          last_hits: 260,
          denies: 12,
          hero_damage: 24000,
          hero_healing: 0,
          tower_damage: 9000,
          level: 25,
          lane_efficiency: 0.57,
          kda: 9.5,
        },
        position: 3,
        execution_score: 78.2,
        result_adjusted_score: 81.4,
        coverage: 0.96,
        role_confidence: 0.91,
        ranking_eligible: true,
        benchmark_cutoff: "2026-07-01T00:00:00Z",
        score_version: "player-score-v3+observed-role=role-v1",
        component_facts: {},
        component_scores: [],
        weights: [],
        explanation: {},
      }],
      draft_predictions: [{
        model_version: "draft-v1",
        model_kind: "context_adjusted",
        horizon_minutes: 20,
        availability_mode: "reconstructed_walk_forward",
        assignment_version: "role-assignment-v1-reconstructed-walk-forward",
        score_version: "player-score-v3+observed-role=role-assignment-v1-reconstructed-walk-forward",
        training_cutoff: "2026-07-01T00:00:00Z",
        model_status: "complete",
        prediction_cutoff: "2026-07-01T00:00:00Z",
        cutoff_source: "historical",
        probability: 0.64,
        uncertainty: 0.08,
        support: 120,
        eventual_radiant_win: 1,
        status: "settled",
      }],
    });

    renderDashboard();

    expect(await screen.findByText("River")).toBeInTheDocument();
    expect(screen.getAllByText("翻盘局").length).toBeGreaterThan(0);
    expect(screen.getAllByText("被翻盘局").length).toBeGreaterThan(0);
    expect(screen.getByText("78.2")).toBeInTheDocument();
    expect(screen.getByText("K/D/A 8 / 2 / 11")).toBeInTheDocument();
    expect(screen.getByText(/GPM\/XPM 650 \/ 720/)).toBeInTheDocument();
    expect(screen.getByText(/英雄\/建筑伤害 24,000 \/ 9,000/)).toBeInTheDocument();
    expect(screen.getAllByText("历史重建").length).toBeGreaterThan(0);
    expect(screen.getAllByText("击杀比分 31 : 18")).toHaveLength(2);
    expect(screen.getByText("比赛局势分类")).toBeInTheDocument();
    expect(screen.queryByText("比赛局势评分")).not.toBeInTheDocument();
    expect(detailMock).toHaveBeenCalledWith(9001, expect.any(AbortSignal));
  });

  it("prefers a completed intelligence match over a newer pending row", async () => {
    const base = {
      radiant_team_id: 101,
      dire_team_id: 202,
      radiant_team_name: "Aurora",
      dire_team_name: "Beacon",
      radiant_win: true,
      duration: 2400,
      leagueid: 77,
      league_name: "Strict Invitational",
      radiant_score: 31,
      dire_score: 18,
    };
    matchesMock.mockResolvedValue({
      data: [{
        ...base,
        match_id: 9002,
        start_time: 1_752_600_000,
        radiant_state: null,
        dire_state: null,
      }, {
        ...base,
        match_id: 9001,
        start_time: 1_752_500_000,
        radiant_state: state("radiant", "comeback", 101),
        dire_state: state("dire", "throw", 202),
      }],
      pagination: { ...pagination, total: 2 },
    });
    detailMock.mockResolvedValue({
      match: { ...base, match_id: 9001, start_time: 1_752_500_000 },
      radiant_state: state("radiant", "comeback", 101),
      dire_state: state("dire", "throw", 202),
      player_performance: [{
        player_slot: 0,
        account_id: 11,
        player_name: "River",
        team_id: 101,
        side: "radiant",
        hero_id: 2,
        hero_name: "Axe",
        performance: {
          kills: 8,
          deaths: 2,
          assists: 11,
          gold_per_min: 650,
          xp_per_min: 720,
          net_worth: 21000,
          last_hits: 260,
          denies: 12,
          hero_damage: 24000,
          hero_healing: 0,
          tower_damage: 9000,
          level: 25,
          lane_efficiency: 0.57,
          kda: 9.5,
        },
      }],
      player_scores: [],
      draft_predictions: [],
    });

    renderDashboard();

    await waitFor(() => {
      expect(detailMock).toHaveBeenCalledWith(9001, expect.any(AbortSignal));
    });
    expect(screen.getByText("K/D/A 8 / 2 / 11")).toBeInTheDocument();
    expect(screen.getAllByText("评分待处理").length).toBeGreaterThan(0);
    expect(detailMock).not.toHaveBeenCalledWith(9002, expect.anything());
  });

  it("switches between player rankings and team profiles", async () => {
    playersMock.mockResolvedValue({
      data: [{
        rank: 1,
        account_id: 44,
        player_name: "Northwind",
        position: 1,
        map_count: 22,
        average_execution_score: 84.3,
        average_result_adjusted_score: 82.1,
        average_coverage: 0.97,
        average_role_confidence: 0.93,
        score_version: "player-score-v3+observed-role=role-v1",
      }, {
        rank: 2,
        account_id: 44,
        player_name: "Northwind",
        position: 2,
        map_count: 16,
        average_execution_score: 81.7,
        average_result_adjusted_score: 80.4,
        average_coverage: 0.95,
        average_role_confidence: 0.9,
        score_version: "player-score-v3+observed-role=role-v1",
      }],
      pagination: { ...pagination, total: 2 },
    });
    teamsMock.mockResolvedValue({
      data: [{
        team_id: 101,
        team_name: "Aurora",
        team_tag: "AUR",
        logo_url: null,
        profile_cutoff: "2026-07-01T00:00:00Z",
        profile_version: "team-profile-v1",
        opportunity_counts: [],
        posterior_rates: [{
          metric: "comeback_after_5000_deficit",
          opportunities: 8,
          mean: 0.35,
        }],
        duration_quantiles: [],
        weighting: {},
        effective_sample_size: 12.4,
        created_at: "2026-07-02T00:00:00Z",
        state_counts: { comeback: 3, throw: 1 },
      }],
    });
    renderDashboard();

    fireEvent.click(screen.getByRole("tab", { name: /选手评分/ }));
    expect(await screen.findAllByText("Northwind")).toHaveLength(2);
    expect(screen.getByText("84.3")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /球队画像/ }));
    expect(await screen.findByText("Aurora")).toBeInTheDocument();
    expect(screen.getByText("35.0%")).toBeInTheDocument();
    expect(screen.getByText("8 次机会")).toBeInTheDocument();
    expect(screen.getByText("低样本，画像不稳定（有效样本量 < 15）")).toBeInTheDocument();
  });

  it("renders loading, empty, and error states", async () => {
    let rejectOverview: ((reason: Error) => void) | undefined;
    overviewMock.mockReturnValue(new Promise((_, reject) => {
      rejectOverview = reject;
    }));
    const view = renderDashboard();

    expect(overviewMock).not.toHaveBeenCalled();
    expect(await screen.findByText("没有符合条件的历史比赛")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: /阵容校准/ }));
    await waitFor(() => expect(overviewMock).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("progressbar", { name: "正在加载历史情报总览" })).toBeInTheDocument();
    rejectOverview?.(new Error("database unavailable"));
    expect(await screen.findByRole("alert")).toHaveTextContent("database unavailable");

    fireEvent.click(screen.getByRole("tab", { name: /比赛复盘/ }));
    expect(await screen.findByText("没有符合条件的历史比赛")).toBeInTheDocument();

    view.unmount();
    await waitFor(() => expect(matchesMock).toHaveBeenCalled());
  });

  it("renders an honest empty state in every data view", async () => {
    renderDashboard();

    expect(await screen.findByText("没有符合条件的历史比赛")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /选手评分/ }));
    expect(await screen.findByText("没有符合条件的选手评分")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /球队画像/ }));
    expect(await screen.findByText("没有符合条件的球队画像")).toBeInTheDocument();
  });

  it("shows match, player, and team API errors without inventing data", async () => {
    overviewMock.mockResolvedValue({
      ...overview(),
      draft_quality: [],
      availability: {
        reconstructed_walk_forward: true,
        prospective: true,
      },
    });
    matchesMock.mockRejectedValue(new Error("matches unavailable"));
    playersMock.mockRejectedValue(new Error("players unavailable"));
    teamsMock.mockRejectedValue(new Error("teams unavailable"));
    renderDashboard();

    expect(await screen.findByText("matches unavailable")).toBeInTheDocument();
    expect(screen.queryByText("没有符合条件的历史比赛")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /选手评分/ }));
    expect(await screen.findByText("players unavailable")).toBeInTheDocument();
    expect(screen.queryByText("没有符合条件的选手评分")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /球队画像/ }));
    expect(await screen.findByText("teams unavailable")).toBeInTheDocument();
    expect(screen.queryByText("没有符合条件的球队画像")).not.toBeInTheDocument();
  });
});

function renderDashboard() {
  return render(
    <FluentProvider theme={webDarkTheme} applyStylesToPortals={false}>
      <IntelligenceDashboard />
    </FluentProvider>,
  );
}

function overview(): IntelligenceOverview {
  return {
    versions: {
      player_score: "player-score-v3+observed-role=role-v1",
      team_state: "team-state-v1",
      team_profile: "team-profile-v1",
      draft_score: "player-score-v3+observed-role=role-v1",
    },
    coverage: {
      formal_maps: 526,
      scored_matches: 526,
      player_score_rows: 5260,
      team_state_rows: 1052,
    },
    team_state_distribution: { comeback: 61, throw: 61, stomp: 70, stomp_loss: 70 },
    draft_quality: [
      quality({
        availability_mode: "reconstructed_walk_forward",
        availability_status: "available",
        status: "failed",
        support: 370,
        brier_score: 0.257,
        log_loss: 0.747,
        ece_5_bin: 0.12,
        ece_90_upper: 0.14,
        gate_failures: ["brier_not_below_0.25", "log_loss_not_below_ln2", "ece_above_0.10"],
      }),
      quality({
        availability_mode: "prospective",
        availability_status: "missing",
        status: "missing",
        support: 0,
        gate_failures: ["prospective_data_missing"],
      }),
    ],
    availability: {
      reconstructed_walk_forward: true,
      prospective: false,
    },
  };
}

function quality(
  overrides: Partial<IntelligenceDraftQualitySlice>,
): IntelligenceDraftQualitySlice {
  return {
    model_kind: "context_adjusted",
    horizon_minutes: 20,
    availability_mode: "reconstructed_walk_forward",
    assignment_version: "role-assignment-v1-reconstructed-walk-forward",
    score_version: "player-score-v3+observed-role=role-assignment-v1-reconstructed-walk-forward",
    availability_status: "available",
    is_reconstructed: true,
    support: 100,
    eligible_targets: 100,
    predicted: 100,
    insufficient_evidence: 0,
    brier_score: 0.2,
    log_loss: 0.6,
        ece_5_bin: 0.08,
        ece_90_upper: 0.11,
    status: "passed",
    gate_failures: [],
    ...overrides,
  };
}

function state(
  side: "radiant" | "dire",
  label: "comeback" | "throw",
  teamId: number,
) {
  return {
    team_id: teamId,
    side,
    label,
    duration_seconds: 2400,
    max_lead: 12000,
    max_deficit: -7000,
    ahead_fraction: 0.52,
    behind_fraction: 0.31,
    closeout_seconds: 410,
    curve_coverage: 0.98,
    label_version: "team-state-v1",
  };
}
