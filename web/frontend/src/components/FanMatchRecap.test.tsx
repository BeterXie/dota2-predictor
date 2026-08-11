import { FluentProvider, webDarkTheme } from "@fluentui/react-components";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { MatchDetail, MonitorMatch, PostmatchGame, PostmatchHistoricalAverage } from "../types";
import { FanMatchRecap } from "./FanMatchRecap";


const match: MonitorMatch = {
  raybet_match_id: "38422439",
  tournament: "EPL 大师赛",
  team_one: "Zero Tenacity",
  team_two: "Na`Vi",
  scheduled_at: "2026-08-10T02:00:00+08:00",
  best_of: 3,
  provider_status: "3",
  updated_at: "2026-08-10T04:30:00+08:00",
  lifecycle: "ended",
  history_eligible: true,
  winner: null,
  latest_vision: null,
};


function historicalAverage(kills: number): PostmatchHistoricalAverage {
  return {
    sample_size: 6,
    source: "opendota_collected_history",
    cutoff: "before_match_start",
    sample_start_date: "2026-07-01",
    sample_end_date: "2026-08-09",
    kills,
    deaths: 3.5,
    assists: 9.2,
    gold_per_min: 610,
    xp_per_min: 680,
    net_worth: 18500,
    last_hits: 260,
    hero_damage: 20500,
    tower_damage: 3400,
  };
}


function game(mapNumber: number, radiantWin: boolean, radiantScore: number, direScore: number): PostmatchGame {
  const radiantIsNavi = mapNumber === 2;
  return {
    map_number: mapNumber,
    official_match_id: `893772278${mapNumber}`,
    identity_reason: "confirmed_map_result",
    identity_evidence: {
      method: "confirmed_settlement_reconciliation",
      official_source: "confirmed_map_result",
    },
    status: "available",
    source: "opendota",
    enrichment: {
      provider: "stratz",
      status: "not_available",
      reason: "optional_enrichment_not_ingested",
      observed_at: null,
    },
    fetched_at: "2026-08-10T05:00:00+08:00",
    result: {
      radiant_team_id: radiantIsNavi ? 36 : 9600141,
      dire_team_id: radiantIsNavi ? 9600141 : 36,
      radiant_team_name: radiantIsNavi ? "Natus Vincere" : "Zero Tenacity",
      dire_team_name: radiantIsNavi ? "Zero Tenacity" : "Natus Vincere",
      radiant_win: radiantWin,
      duration_seconds: mapNumber === 1 ? 1802 : 2100,
      start_time: 1_786_340_000,
      league_id: 19944,
      league_name: "EPL Masters 2026",
      radiant_score: radiantScore,
      dire_score: direScore,
    },
    players: [
      {
        player_slot: 0,
        account_id: 1,
        player_name: "Munkushi~",
        player_name_source: "opendota_name",
        side: "radiant",
        team_id: radiantIsNavi ? 36 : 9600141,
        hero_id: 1,
        hero_name: "Anti-Mage",
        hero_key: "antimage",
        kills: 8,
        deaths: 2,
        assists: 10,
        gold_per_min: 700,
        xp_per_min: 800,
        net_worth: 22000,
        last_hits: 320,
        denies: 8,
        hero_damage: 24000,
        hero_healing: 0,
        tower_damage: 6000,
        level: 25,
        position: 1,
        position_source: "stratz",
        historical_average: historicalAverage(6.3),
        items: [],
      },
      {
        player_slot: 128,
        account_id: 2,
        player_name: "Malik",
        player_name_source: "opendota_name",
        side: "dire",
        team_id: radiantIsNavi ? 9600141 : 36,
        hero_id: 2,
        hero_name: "Axe",
        hero_key: "axe",
        kills: 4,
        deaths: 5,
        assists: 8,
        gold_per_min: 480,
        xp_per_min: 560,
        net_worth: 15000,
        last_hits: 180,
        denies: 5,
        hero_damage: 17000,
        hero_healing: 0,
        tower_damage: 1200,
        level: 21,
        position: 3,
        position_source: "stratz",
        historical_average: historicalAverage(4.8),
        items: [],
      },
    ],
    draft: [
      { order: 1, is_pick: true, side: "radiant", hero_id: 1, hero_name: "Anti-Mage", hero_key: "antimage" },
      { order: 2, is_pick: true, side: "dire", hero_id: 2, hero_name: "Axe", hero_key: "axe" },
    ],
    advantages: {
      gold: [{ minute: 0, value: 0 }, { minute: 30, value: radiantWin ? 12000 : -12000 }],
      xp: [{ minute: 0, value: 0 }, { minute: 30, value: radiantWin ? 8000 : -8000 }],
    },
    objectives: [
      { time_seconds: 600, type: "CHAT_MESSAGE_TOWER_KILL", unit: "npc_dota_goodguys_tower1_top", key: "", player_slot: null },
      { time_seconds: 1200, type: "CHAT_MESSAGE_ROSHAN_KILL", unit: "npc_dota_roshan", key: "", player_slot: null },
      { time_seconds: 1400, type: "UNSUPPORTED_RAW_EVENT", unit: "", key: "RAW_VALUE", player_slot: null },
    ],
    teamfights: [{
      start_time: 900,
      end_time: 920,
      last_death: 918,
      deaths: 3,
      kills: 3,
      damage: 9000,
      healing: 400,
      gold_delta: 1200,
      xp_delta: 900,
    }],
    availability: {
      result: "available",
      players: "available",
      player_names: "available",
      historical_averages: "available",
      positions: "available",
      draft: "available",
      gold_advantage: "available",
      xp_advantage: "available",
      objectives: "available",
      teamfights: "available",
    },
  };
}


const detail: MatchDetail = {
  ...match,
      draft_context: null,
      games: [],
      market_evidence: [],
  postmatch: {
    status: "available",
    reason: "exact_opendota_series_available",
    sources: {
      canonical: { provider: "opendota", role: "canonical_postmatch", status: "available", reason: "available" },
      enhancement: { provider: "stratz", role: "optional_enrichment", status: "not_available", reason: "not_available" },
    },
    games: [game(1, false, 8, 32), game(2, true, 20, 5)],
    unresolved_maps: [],
  },
};


describe("FanMatchRecap", () => {
  it("leads with the series result and hides operator terminology", () => {
    render(
      <FluentProvider theme={webDarkTheme}>
        <FanMatchRecap detail={detail} error={null} loading={false} match={match} />
      </FluentProvider>,
    );

    expect(screen.getByText("Natus Vincere 赢下系列赛")).toBeInTheDocument();
    expect(screen.getByLabelText("系列赛比分 Zero Tenacity 0 比 2 Natus Vincere")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Natus Vincere 32 : 8 取胜" })).toBeInTheDocument();
    expect(screen.getByText("上路一塔被摧毁")).toBeInTheDocument();
    expect(screen.getByText("击杀肉山")).toBeInTheDocument();
    expect(screen.getByText("Munkushi~")).toBeInTheDocument();
    expect(screen.getAllByText(/前 6 局/).length).toBeGreaterThan(0);
    expect(screen.getAllByText("OpenDota · 2026-07-01 至 2026-08-09 · 6 局")).toHaveLength(2);
    expect(screen.getByText(/Map 身份与赛果以精确关联的 Valve\/Dota 比赛记录为准/)).toBeInTheDocument();
    expect(screen.queryByText(/OpenDota 官方比赛记录/)).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "数据完整性" })).toBeInTheDocument();
    expect(screen.getByText("10 / 10 项完整，缺失项不会被推测补齐")).toBeInTheDocument();
    expect(screen.queryByText("UNSUPPORTED_RAW_EVENT")).not.toBeInTheDocument();
    expect(screen.queryByText(/P0|P1|Vision/)).not.toBeInTheDocument();
  });

  it("switches maps without leaving the recap", () => {
    render(
      <FluentProvider theme={webDarkTheme}>
        <FanMatchRecap detail={detail} error={null} loading={false} match={match} />
      </FluentProvider>,
    );

    fireEvent.click(screen.getByRole("tab", { name: /第 2 局/ }));

    expect(screen.getByRole("heading", { name: "Natus Vincere 20 : 5 取胜" })).toBeInTheDocument();
    expect(screen.getByText("35:00")).toBeInTheDocument();
  });

  it("explains a blocked STRATZ position source without hiding OpenDota data", () => {
    const blocked: MatchDetail = {
      ...detail,
      postmatch: {
        ...detail.postmatch,
        sources: {
          ...detail.postmatch.sources,
          enhancement: {
            ...detail.postmatch.sources.enhancement,
            status: "blocked",
            reason: "stratz_http_403",
          },
        },
        games: detail.postmatch.games.map((item) => ({
          ...item,
          enrichment: {
            ...item.enrichment,
            status: "blocked",
            reason: "stratz_http_403",
          },
        })),
      },
    };

    render(
      <FluentProvider theme={webDarkTheme}>
        <FanMatchRecap detail={blocked} error={null} loading={false} match={match} />
      </FluentProvider>,
    );

    expect(screen.getByText(/STRATZ 位置补充暂不可用（认证被拒绝）/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Natus Vincere 32 : 8 取胜" })).toBeInTheDocument();
  });
});
