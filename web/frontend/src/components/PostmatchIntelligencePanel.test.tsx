import { cleanup, render, screen, waitFor } from "@testing-library/react";
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

  it("rejects a response for another map instead of showing stale facts", async () => {
    fetchMock.mockResolvedValue({ ...available(), map_number: 3 });
    renderPanel();

    expect(await screen.findByRole("alert")).toHaveTextContent("赛后归因响应与当前比赛局号不一致");
    expect(screen.queryByText("Kunkka")).not.toBeInTheDocument();
  });
});

function renderPanel() {
  return render(
    <PostmatchIntelligencePanel
      mapNumber={2}
      raybetMatchId="match-1"
      teamOne="Aurora"
      teamTwo="Beacon"
    />,
  );
}

function available(): ExactPostmatchAttribution {
  return {
    raybet_match_id: "match-1",
    map_number: 2,
    status: "available",
    reason: "confirmed",
    mapping: { mapping_id: 12 },
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
