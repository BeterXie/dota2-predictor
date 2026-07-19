import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fetchExactPostmatchAttribution } from "../api";
import type { ExactPostmatchAttribution, IntelligenceTeamState } from "../types";
import { PostmatchIntelligencePanel } from "./PostmatchIntelligencePanel";

vi.mock("../api", () => ({
  fetchExactPostmatchAttribution: vi.fn(),
}));

const fetchMock = vi.mocked(fetchExactPostmatchAttribution);

afterEach(cleanup);
beforeEach(() => vi.clearAllMocks());

describe("PostmatchIntelligencePanel", () => {
  it("renders exact linked states, aligned events, and player performance", async () => {
    fetchMock.mockResolvedValue(available());
    renderPanel();

    expect(await screen.findByText("OpenDota #9001")).toBeInTheDocument();
    expect(screen.getByText("翻盘局")).toBeInTheDocument();
    expect(screen.getByText("被翻盘局")).toBeInTheDocument();
    expect(screen.getByText("肉山击杀")).toBeInTheDocument();
    expect(screen.getByText("Kunkka")).toBeInTheDocument();
    expect(screen.getByText("8 / 2 / 11")).toBeInTheDocument();
    expect(screen.getByText("57.0%")).toBeInTheDocument();
    expect(screen.getByText("团战事件 缺失")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("match-1", 2, expect.any(AbortSignal));
  });

  it("keeps historical mapping names on their exact probability sides", async () => {
    const result = available();
    result.postmatch!.match.radiant_team_id = 202;
    result.postmatch!.match.dire_team_id = 101;
    result.postmatch!.states.radiant = state("radiant", "comeback", 202);
    result.postmatch!.states.dire = state("dire", "throw", 101);
    fetchMock.mockResolvedValue(result);
    const { container } = renderPanel("Current Beacon", "Current Aurora");

    expect(await screen.findByText("Historical Aurora")).toBeInTheDocument();
    expect(screen.getByText("Historical Beacon")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", {
      name: "Historical Aurora（team_one）概率",
    })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", {
      name: "Historical Beacon（team_two）概率",
    })).toBeInTheDocument();
    const eventRow = screen.getByText("肉山击杀").closest("tr");
    expect(eventRow).not.toBeNull();
    const cells = within(eventRow!).getAllByRole("cell");
    expect(cells[5]).toHaveTextContent("57.0%");
    expect(cells[6]).toHaveTextContent("43.0%");
    const stateCards = container.querySelectorAll(".postmatch-state");
    expect(stateCards).toHaveLength(2);
    expect(within(stateCards[0] as HTMLElement).getByText("Historical Beacon")).toBeInTheDocument();
    expect(within(stateCards[1] as HTMLElement).getByText("Historical Aurora")).toBeInTheDocument();
    expect(screen.queryByText("Current Beacon")).not.toBeInTheDocument();
    expect(screen.queryByText("Current Aurora")).not.toBeInTheDocument();
  });

  it("fails closed when a legacy available response has no mapping names", async () => {
    fetchMock.mockResolvedValue({ ...available(), mapping: null });
    renderPanel("Current Team One", "Current Team Two");

    expect(await screen.findByRole("columnheader", {
      name: "映射名称缺失（team_one）概率",
    })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", {
      name: "映射名称缺失（team_two）概率",
    })).toBeInTheDocument();
    expect(screen.getAllByText("映射名称缺失")).toHaveLength(2);
    expect(screen.queryByText("Current Team One")).not.toBeInTheDocument();
    expect(screen.queryByText("Current Team Two")).not.toBeInTheDocument();
  });

  it("shows current mapping warnings without hiding immutable postmatch facts", async () => {
    fetchMock.mockResolvedValue({
      ...available(),
      warnings: ["mapping_invalidated"],
    });
    renderPanel();

    expect(await screen.findByText("当前 mapping 状态待复核")).toBeInTheDocument();
    expect(screen.getByText("该局 strict mapping 已失效。")).toBeInTheDocument();
    expect(screen.getByText("mapping_invalidated")).toBeInTheDocument();
    expect(screen.getByText("OpenDota #9001")).toBeInTheDocument();
    expect(screen.getByText("Kunkka")).toBeInTheDocument();
  });

  it.each([
    ["review", "opendota_match_link_conflict", "赛后归因待复核"],
    ["unavailable", "strict_mapping_missing", "赛后归因不可用"],
  ] as const)("fails closed for %s link state", async (status, reason, title) => {
    fetchMock.mockResolvedValue({
      ...available(),
      status,
      reason,
      postmatch: null,
    });
    renderPanel();

    expect(await screen.findByText(title)).toBeInTheDocument();
    expect(screen.getByText(reason)).toBeInTheDocument();
    expect(screen.queryByText("Kunkka")).not.toBeInTheDocument();
  });

  it.each([
    ["review", "reconciliation_causal_order_invalid", "结算核对时间早于 mapping、时间顺序异常，或缺少可验证时区。"],
    ["unavailable", "reconciliation_schema_unavailable", "当前数据库缺少赛后结算核对协议结构。"],
    ["review", "opendota_match_identity_invalid", "已确认结算中的 OpenDota 比赛 ID 无效。"],
    ["unavailable", "opendota_scope_schema_unavailable", "当前数据库缺少验证 OpenDota 正式赛事范围所需的协议结构。"],
    ["unavailable", "opendota_ingest_schema_unavailable", "当前数据库缺少验证 OpenDota 入库身份所需的协议结构。"],
    ["review", "opendota_result_identity_conflict", "OpenDota 比赛结果缺失或类型无效，无法确认胜方身份。"],
    ["review", "reconciliation_winner_conflict", "RayBet 与 OpenDota 的已确认胜方不一致。"],
    ["unavailable", "settlement_evidence_schema_unavailable", "当前数据库缺少验证结算证据所需的协议结构。"],
    ["unavailable", "raybet_match_schema_unavailable", "当前数据库缺少 RayBet 比赛身份协议结构。"],
    ["review", "reconciliation_mapping_authority_missing", "历史结算缺少不可变 mapping authority，需人工复核。"],
    ["unavailable", "map_result_schema_unavailable", "当前数据库缺少赛果 mapping authority 协议结构。"],
    ["unavailable", "map_result_missing", "已确认结算缺少对应的不可变赛果记录。"],
    ["review", "map_result_mapping_lineage_unverified", "赛果记录与结算时的不可变 mapping 不一致。"],
    ["review", "map_result_causal_order_invalid", "赛果记录的时间顺序无法通过因果校验。"],
  ] as const)("explains exact-postmatch reason %s", async (status, reason, detail) => {
    fetchMock.mockResolvedValue({
      ...available(),
      status,
      reason,
      postmatch: null,
    });
    renderPanel();

    expect(await screen.findByText(detail)).toBeInTheDocument();
    expect(screen.getByText(reason)).toBeInTheDocument();
  });

  it("rejects a response for another map instead of showing stale facts", async () => {
    fetchMock.mockResolvedValue({ ...available(), map_number: 3 });
    renderPanel();

    expect(await screen.findByRole("alert")).toHaveTextContent("赛后归因响应与当前比赛局号不一致");
    expect(screen.queryByText("Kunkka")).not.toBeInTheDocument();
  });
});

function renderPanel(teamOne = "Aurora", teamTwo = "Beacon") {
  return render(
    <PostmatchIntelligencePanel
      mapNumber={2}
      raybetMatchId="match-1"
      teamOne={teamOne}
      teamTwo={teamTwo}
    />,
  );
}

function available(): ExactPostmatchAttribution {
  return {
    raybet_match_id: "match-1",
    map_number: 2,
    status: "available",
    reason: "confirmed",
    mapping: {
      mapping_id: 12,
      event_id: "strict-event-1",
      acceptance_mode: "manual_exact",
      mapping_version: "strict-live-map-v1",
      canonical_teams: [
        {
          side: "team_one",
          team_id: 101,
          team_name: "Historical Aurora",
        },
        {
          side: "team_two",
          team_id: 202,
          team_name: "Historical Beacon",
        },
      ],
      accepted_at: "2026-07-17T00:00:00Z",
      recorded_at: "2026-07-17T00:00:01Z",
    },
    reconciliation: { reconciliation_id: 18, status: "confirmed" },
    odds_timeline: [{
      observed_at: "2026-07-17T00:20:00Z",
      period: "map_2",
      prices: { team_one: 1.7, team_two: 2.2 },
      probabilities: { team_one: 0.57, team_two: 0.43 },
      status: { team_one: "open", team_two: "open" },
      game_clock_seconds: 1200,
    }],
    postmatch: {
      match: {
        match_id: 9001,
        radiant_team_id: 101,
        dire_team_id: 202,
        radiant_team_name: "Aurora",
        dire_team_name: "Beacon",
        radiant_win: true,
        duration: 2400,
        start_time: 1_752_600_000,
        leagueid: 77,
        league_name: "Strict Invitational",
        radiant_score: 31,
        dire_score: 18,
      },
      states: {
        radiant: state("radiant", "comeback", 101),
        dire: state("dire", "throw", 202),
      },
      player_performance: [{
        player_slot: 0,
        account_id: 11,
        player_name: "River",
        team_id: 101,
        side: "radiant",
        hero_id: 23,
        hero_name: "Kunkka",
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
      events: [{
        game_time_seconds: 1200,
        event_type: "objective",
        side: "radiant",
        label: "肉山击杀",
        radiant_gold_adv: 3500,
        radiant_xp_adv: 2100,
        team_one_probability: 0.57,
        team_two_probability: 0.43,
        details: {},
      }],
      event_availability: {
        gold_advantage: true,
        xp_advantage: true,
        objectives: true,
        teamfights: false,
        buybacks: true,
        odds_game_clock_alignment: true,
        missing_reasons: ["teamfights_missing"],
      },
    },
  };
}

function state(
  side: "radiant" | "dire",
  label: IntelligenceTeamState["label"],
  teamId: number,
): IntelligenceTeamState {
  return {
    side,
    team_id: teamId,
    label,
    duration_seconds: 2400,
    max_lead: 8500,
    max_deficit: -6200,
    ahead_fraction: 0.58,
    behind_fraction: 0.32,
    curve_coverage: 0.98,
    label_version: "team-state-v1",
  };
}
