import { FluentProvider, webDarkTheme } from "@fluentui/react-components";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  LiveDraftProspectivePrediction,
  MapDecisionCheckpoint,
  MatchDetail,
  MonitorMatch,
} from "../types";

const api = vi.hoisted(() => ({
  correctLiveGameSnapshot: vi.fn(),
  createLiveDraftPrediction: vi.fn(),
  fetchHeroGrid: vi.fn(),
  fetchLiveDraftPrediction: vi.fn(),
  fetchTeamGrid: vi.fn(),
  saveLiveDraftMapping: vi.fn(),
}));

vi.mock("../api", () => api);

import { MatchWorkspace } from "./MatchWorkspace";

const match: MonitorMatch = {
  raybet_match_id: "raybet-1",
  tournament: "Test Event",
  team_one: "Radiant Team",
  team_two: "Dire Team",
  scheduled_at: "2026-08-07T10:00:00+00:00",
  best_of: 3,
  provider_status: "2",
  updated_at: "2026-08-07T10:10:00+00:00",
  lifecycle: "live",
  history_eligible: false,
  watch_link: {
    kind: "stream_resolver",
    availability: "available",
    url: "/api/monitor/matches/raybet-1/live-stream",
    reason: "fresh_stream_resolution_available",
  },
  winner: null,
  latest_vision: {
    captured_at: "2026-08-07T10:09:00+00:00",
    map_number: 1,
    game_clock_seconds: 420,
    is_paused: 0,
    screen_state: "game",
    confirmed: 1,
    clock_confidence: 0.98,
    draft_confidence: 0.99,
    radiant_hero_ids: [1, 2, 3, 4, 5],
    dire_hero_ids: [6, 7, 8, 9, 10],
  },
};

const slots = (["radiant", "dire"] as const).flatMap((side, sideIndex) =>
  Array.from({ length: 5 }, (_, index) => ({
    team_id: sideIndex + 1,
    side,
    position: index + 1,
    hero_id: sideIndex * 5 + index + 1,
    player_id: null,
  })),
);

const detail: MatchDetail = {
  ...match,
  postmatch: {
    status: "waiting",
    reason: "exact_opendota_map_not_available",
    sources: {
      canonical: {
        provider: "opendota",
        role: "canonical_postmatch",
        status: "waiting_for_exact_link",
        reason: "exact_map_link_not_confirmed",
      },
      enhancement: {
        provider: "stratz",
        role: "optional_enrichment",
        status: "not_available",
        reason: "optional_enrichment_not_ingested",
      },
    },
    games: [],
    unresolved_maps: [],
  },
  draft_context: {
    status: "ready",
    reason: "strict_mapping_available",
    source: "strict_mapping",
    teams: [
      { match_side: "team_one", team_id: 1, team_name: "Radiant Team" },
      { match_side: "team_two", team_id: 2, team_name: "Dire Team" },
    ],
  },
  market_evidence: [],
  games: [{
    game_id: "raybet-1:map_1",
    map_number: 1,
    period: "map_1",
    official_match_id: null,
    link_status: "unlinked",
    link_reason: "exact_official_match_id_not_available",
    play_evidence: ["verified_game_frame"],
    state: "live",
    winner: null,
    prematch_winner: null,
    winner_timeline: [],
    odds_coverage: {
      source: "raybet_direct",
      gap_threshold_seconds: 150,
      prematch: {
        status: "missing",
        complete_snapshot_count: 0,
        observation_count: 0,
        first_observed_at: null,
        last_observed_at: null,
        gap_count: 0,
        longest_gap_seconds: null,
        periods: [],
      },
      live: {
        status: "available",
        complete_snapshot_count: 42,
        observation_count: 18,
        first_observed_at: "2026-08-07T10:00:00+00:00",
        last_observed_at: "2026-08-07T10:10:00+00:00",
        gap_count: 1,
        longest_gap_seconds: 190,
        periods: [],
      },
      closing: {
        status: "pending",
        observed_at: null,
        prices: null,
        probabilities: null,
      },
    },
    vision: [match.latest_vision!],
    latest_vision: match.latest_vision,
    latest_capture: null,
    draft_mapping: {
      raybet_match_id: "raybet-1",
      map_number: 1,
      version: 1,
      source: "manual",
      is_locked: true,
      created_by: "operator",
      created_at: "2026-08-07T10:08:00+00:00",
      slots,
    },
    game_snapshots: [{
      snapshot_id: 1,
      raybet_match_id: "raybet-1",
      map_number: 1,
      game_time_seconds: 420,
      radiant_networth: 16000,
      dire_networth: 15000,
      networth_lead: 1000,
      radiant_kills: 8,
      dire_kills: 6,
      vision_confidence: 0.98,
      screenshot_path: null,
      source: "vision",
      captured_at: "2026-08-07T10:09:00+00:00",
      created_by: null,
      created_at: "2026-08-07T10:09:00+00:00",
    }],
    latest_game_snapshot: {
      snapshot_id: 1,
      raybet_match_id: "raybet-1",
      map_number: 1,
      game_time_seconds: 420,
      radiant_networth: 16000,
      dire_networth: 15000,
      networth_lead: 1000,
      radiant_kills: 8,
      dire_kills: 6,
      vision_confidence: 0.98,
      screenshot_path: null,
      source: "vision",
      captured_at: "2026-08-07T10:09:00+00:00",
      created_by: null,
      created_at: "2026-08-07T10:09:00+00:00",
    },
    latest_hud_observation: null,
    vision_runtime: null,
    markets: [],
    decision_checkpoints: [],
    postmatch: {
      status: "waiting",
      reason: "map_1_not_observed",
      sources: {
        canonical: {
          provider: "opendota",
          role: "canonical_postmatch",
          status: "waiting_for_exact_link",
          reason: "exact_map_link_not_confirmed",
        },
        enhancement: {
          provider: "stratz",
          role: "optional_enrichment",
          status: "not_available",
          reason: "optional_enrichment_not_ingested",
        },
      },
      games: [],
      unresolved_maps: [],
    },
  }],
};

const pairedPrediction: LiveDraftProspectivePrediction = {
  prediction_hash: "a".repeat(64),
  version: "live-draft-prospective-bridge-v1",
  identity: {
    raybet_match_id: "raybet-1",
    map_number: 1,
    mapping_version: 1,
    mapping_hash: "b".repeat(64),
  },
  operator_locked_at: "2026-08-07T10:08:00+00:00",
  confirmed_at: "2026-08-07T10:08:01+00:00",
  record_status: "paired",
  p0_probability: 0.6,
  p1_probability: 0.65,
  pure_rosh_score: 1,
  standardized_rosh_score: 0.5,
  rosh_logit_contribution: 0.2,
  missing_reason: null,
  candidate_hash: "c".repeat(64),
  causal_evidence: {
    game_clock_seconds: null,
    vision_frame_timestamp: null,
    draft_state_marker: "draft_complete",
    live_state_input_used: false,
    causal_status: "eligible",
    causal_reason: null,
  },
  created_at: "2026-08-07T10:08:02+00:00",
};

const pregameCheckpoint: MapDecisionCheckpoint = {
  checkpoint_id: 7,
  raybet_match_id: "raybet-1",
  map_number: 1,
  mapping_version: 1,
  phase: "pregame",
  checkpoint_minute: 0,
  strategy_version: "map-decision-shadow-v1",
  decision: "bet_team_a",
  assumed_stake_units: 1,
  observed_price: 1.82,
  model_probability_team_one: 0.65,
  model_probability_team_two: 0.35,
  market_probability_team_one: 0.55,
  market_probability_team_two: 0.45,
  selected_edge: 0.1,
  odds_observation_key: "odds-observation-7",
  odds_group_id: "winner-map-1",
  odds_observed_at: "2026-08-07T10:09:59+00:00",
  odds_age_seconds: 1,
  odds_max_age_seconds: 150,
  vision_snapshot_id: null,
  vision_source_frame_ref: null,
  vision_captured_at: null,
  vision_game_time_seconds: null,
  vision_networth_lead: null,
  vision_radiant_kills: null,
  vision_dire_kills: null,
  vision_age_seconds: null,
  vision_max_age_seconds: null,
  odds_vision_gap_seconds: null,
  odds_vision_gap_max_seconds: null,
  vision_trusted: false,
  vision_replay: false,
  input_versions: { strategy_version: "map-decision-shadow-v1", mapping_version: 1 },
  feature_availability: { pregame_probability: { available: true } },
  reason: "minimum_edge_met",
  decided_at: "2026-08-07T10:10:00+00:00",
  created_at: "2026-08-07T10:10:00+00:00",
  evaluation_eligible: true,
  evaluation_exclusion_reason: null,
  settlement: null,
};

const liveCheckpoint: MapDecisionCheckpoint = {
  ...pregameCheckpoint,
  checkpoint_id: 8,
  phase: "live",
  checkpoint_minute: 5,
  model_probability_team_one: 0.788,
  model_probability_team_two: 0.212,
  selected_edge: 0.238,
  odds_max_age_seconds: 15,
  vision_snapshot_id: 19,
  vision_source_frame_ref: "vision-frame:series-1:map-1:300",
  vision_captured_at: "2026-08-07T10:09:59+00:00",
  vision_game_time_seconds: 300,
  vision_networth_lead: 1200,
  vision_radiant_kills: 8,
  vision_dire_kills: 5,
  vision_age_seconds: 1,
  vision_max_age_seconds: 5,
  odds_vision_gap_seconds: 0,
  odds_vision_gap_max_seconds: 15,
  vision_trusted: true,
  input_versions: {
    strategy_version: "map-decision-shadow-v1",
    mapping_version: 1,
    live_probability_model_version: "vision-gold-lead-logit-v1",
  },
  feature_availability: {
    pregame_probability: { available: true },
    live_probability_model: {
      available: true,
      reason: null,
      model_version: "vision-gold-lead-logit-v1",
      prior_radiant_probability: 0.65,
      coefficient_per_1000_gold: 0.576768492,
      validation: {
        holdout_samples: 796,
        holdout_brier: 0.234769,
        baseline_brier: 0.249759,
        holdout_log_loss: 0.661511,
        baseline_log_loss: 0.692665,
      },
    },
    levels: { available: false, reason: "not_collected" },
    objectives: { available: false, reason: "not_collected" },
  },
};

function renderWorkspace(
  replay = false,
  currentDetail = detail,
  currentMatch = match,
) {
  return render(
    <FluentProvider theme={webDarkTheme}>
      <MatchWorkspace
        csrfToken="csrf"
        detail={currentDetail}
        error={null}
        loading={false}
        match={currentMatch}
        replay={replay}
      />
    </FluentProvider>,
  );
}

describe("MatchWorkspace", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchHeroGrid.mockResolvedValue({
      str: Array.from({ length: 10 }, (_, index) => ({
        hero_id: index + 1,
        localized_name: `Hero ${index + 1}`,
        hero_key: `hero_${index + 1}`,
        image_url: "",
      })),
      agi: [],
      int: [],
      all: [],
    });
    api.fetchTeamGrid.mockResolvedValue([
      { team_id: 11, team_name: "Alpha", tag: "A" },
      { team_id: 22, team_name: "Beta", tag: "B" },
    ]);
    api.fetchLiveDraftPrediction.mockResolvedValue({
      status: "not_found",
      prediction: null,
      decision_checkpoints: [],
    });
    api.saveLiveDraftMapping.mockImplementation((
      matchId: string,
      mapNumber: number,
      savedSlots: typeof slots,
      isLocked: boolean,
    ) => Promise.resolve({
      raybet_match_id: matchId,
      map_number: mapNumber,
      version: 1,
      source: "manual",
      is_locked: isLocked,
      created_by: "operator",
      actor: "operator",
      evidence_source_url: "https://example.test/evidence/raybet-1/map-1",
      authority_version: "sourced-manual-draft-v1",
      created_at: "2026-08-07T10:09:30+00:00",
      slots: savedSlots,
      prediction_automation: isLocked ? {
        status: "created",
        prediction: pairedPrediction,
      } : undefined,
      decision_checkpoint: isLocked ? pregameCheckpoint : undefined,
    }));
  });

  it("shows a loading skeleton before the first match arrives", () => {
    render(
      <FluentProvider theme={webDarkTheme}>
        <MatchWorkspace
          detail={null}
          error={null}
          loading
          match={null}
          replay
        />
      </FluentProvider>,
    );

    expect(screen.getByLabelText("正在加载赛事详情")).toBeInTheDocument();
    expect(screen.queryByText("没有可显示的赛事")).not.toBeInTheDocument();
  });

  it("keeps HUD evidence and the locked live prediction action", async () => {
    renderWorkspace();

    const watchLink = screen.getByRole("link", { name: "观看直播" });
    expect(watchLink).toHaveAttribute(
      "href",
      "/api/monitor/matches/raybet-1/live-stream",
    );
    expect(watchLink).toHaveAttribute("target", "_blank");
    expect(watchLink).toHaveAttribute("rel", "noreferrer");
    expect(screen.getByText("HUD 与 Vision 证据")).toBeInTheDocument();
    expect(screen.getByLabelText("赔率采集覆盖")).toBeInTheDocument();
    expect(screen.getByText(/42 个完整盘口 · 18 个采集时点/)).toBeInTheDocument();
    expect(screen.getByText(/1 次断档，最长 3 分 10 秒/)).toBeInTheDocument();
    expect(screen.getByText("7:00")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "生成实时阵容预测" })).toBeInTheDocument();
    });
    expect(screen.getByText(/不使用击杀、经济、经验/)).toBeInTheDocument();
  });

  it("keeps each BO3 map in an independent game workspace", () => {
    const firstGame = { ...detail.games[0], state: "ended" as const };
    const secondGame = {
      ...detail.games[0],
      game_id: "raybet-1:map_2",
      map_number: 2,
      period: "map_2",
      state: "live" as const,
      winner: null,
      prematch_winner: null,
      winner_timeline: [],
      odds_coverage: {
        ...detail.games[0].odds_coverage,
        live: {
          ...detail.games[0].odds_coverage.live,
          complete_snapshot_count: 7,
          observation_count: 4,
          gap_count: 0,
          longest_gap_seconds: null,
        },
      },
      vision: [],
      latest_vision: null,
      draft_mapping: null,
      game_snapshots: [],
      latest_game_snapshot: null,
      postmatch: {
        ...detail.games[0].postmatch,
        reason: "map_2_not_observed",
      },
    };
    renderWorkspace(false, {
      ...detail,
      current_map_number: 2,
      games: [firstGame, secondGame],
    });

    expect(screen.getAllByText(/raybet-1:map_2/).length).toBeGreaterThan(0);
    expect(screen.getByText(/7 个完整盘口 · 4 个采集时点/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "第 1 局 · 已结束" }));
    expect(screen.getAllByText(/raybet-1:map_1/).length).toBeGreaterThan(0);
    expect(screen.getByText(/42 个完整盘口 · 18 个采集时点/)).toBeInTheDocument();
  });

  it("shows the match-specific Vision blocker while capture continues", () => {
    renderWorkspace(false, {
      ...detail,
      games: detail.games.map((game) => ({
        ...game,
        latest_game_snapshot: null,
        game_snapshots: [],
        latest_hud_observation: {
          status: "unavailable",
          source: null,
          observation_file: "raybet-1.jsonl",
          captured_at: "2026-08-07T10:09:40+00:00",
          map_number: 1,
          game_clock_seconds: null,
          is_paused: null,
          screen_state: "unknown",
          clock_confidence: 0,
          draft_confidence: 0,
          hud_confidence: 0,
          draft_confirmed: false,
          radiant_hero_count: 0,
          dire_hero_count: 0,
          radiant_hero_ids: [],
          dire_hero_ids: [],
          radiant_kills: null,
          dire_kills: null,
          radiant_net_worth: null,
          dire_net_worth: null,
          net_worth_advantage_side: null,
          net_worth_advantage_min: null,
          net_worth_advantage_max: null,
          unavailable_reason: "screen_state_unknown",
        },
        vision_runtime: {
          worker_status: "degraded",
          freshness: "fresh",
          observed_at: "2026-08-07T10:09:45+00:00",
          map_number: 1,
          capture_state: "capturing_partial",
          reason: "replay_gate_untrusted",
          blocker_code: "replay_gate_untrusted",
          replay_gate_status: "untrusted",
          screen_state: "game",
        },
      })),
    });

    expect(screen.getByText(/采集中，等待可信帧/)).toBeInTheDocument();
    expect(screen.getByText(/直播或回放状态尚未可信确认/)).toBeInTheDocument();
  });

  it("renders the paired P1 versus current-map odds as a shadow value decision", async () => {
    api.fetchLiveDraftPrediction.mockResolvedValueOnce({
      status: "available",
      prediction: pairedPrediction,
      decision_checkpoints: [pregameCheckpoint],
    });
    const winner = {
      observed_at: "2026-08-07T10:10:00+00:00",
      period: "map_1",
      complete: true,
      prices: { team_one: 1.82, team_two: 2.08 },
      probabilities: { team_one: 0.55, team_two: 0.45 },
    };

    renderWorkspace(false, {
      ...detail,
      games: detail.games.map((game) => ({ ...game, winner })),
    }, { ...match, winner });

    expect(await screen.findByLabelText("影子投注决策")).toBeInTheDocument();
    expect(screen.getByText("bet_team_a")).toBeInTheDocument();
    expect(screen.getByText("map-decision-shadow-v1")).toBeInTheDocument();
    expect(screen.getByText(/不会回写 P0\/P1、创建订单或提交真实投注/)).toBeInTheDocument();
  });

  it("renders the live model, evidence, thresholds, and validation trace", () => {
    renderWorkspace(false, {
      ...detail,
      games: detail.games.map((game) => ({
        ...game,
        decision_checkpoints: [liveCheckpoint],
      })),
    });

    expect(screen.getByText(/bet_team_a \(Radiant Team\) · minimum_edge_met/)).toBeInTheDocument();
    expect(screen.getByText(/模型 vision-gold-lead-logit-v1/)).toBeInTheDocument();
    expect(screen.getByText(/模型概率：Radiant Team 78.8% · Dire Team 21.2%/)).toBeInTheDocument();
    expect(screen.getByText(/市场概率：Radiant Team 55.0% · Dire Team 45.0%/)).toBeInTheDocument();
    expect(screen.getByText(/价值差 23.8% · 采用赔率 1.82/)).toBeInTheDocument();
    expect(screen.getByText(/Vision：比赛 5 分 · Radiant 经济 \+1,200 · 击杀 8:5/)).toBeInTheDocument();
    expect(screen.getByText(/时间留出验证：n=796 · Brier 0.235 \/ 基线 0.250/)).toBeInTheDocument();
    expect(screen.getByText(/赔率引用 odds-observation-7/)).toBeInTheDocument();
    expect(screen.getByText(/Vision 引用 vision-frame:series-1:map-1:300 · snapshot #19/)).toBeInTheDocument();
  });

  it("does not render an untrusted stream resolver URL", () => {
    renderWorkspace(false, detail, {
      ...match,
      watch_link: {
        ...match.watch_link!,
        url: "/api/monitor/matches/another-match/live-stream",
      },
    });

    expect(screen.queryByRole("link", { name: "观看直播" })).not.toBeInTheDocument();
  });

  it("keeps historical replay read-only", async () => {
    renderWorkspace(true);

    await waitFor(() => expect(api.fetchLiveDraftPrediction).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "生成实时阵容预测" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "追加人工修正" })).not.toBeInTheDocument();
  });

  it("shows exact OpenDota postmatch details without claiming STRATZ authority", () => {
    const availablePostmatch: MatchDetail["postmatch"] = {
        ...detail.postmatch,
        status: "available",
        reason: "exact_opendota_maps_available",
        sources: {
          ...detail.postmatch.sources,
          canonical: {
            ...detail.postmatch.sources.canonical,
            status: "available",
            reason: "exact_map_details_available",
          },
          enhancement: {
            ...detail.postmatch.sources.enhancement,
            status: "available",
            reason: "player_positions_available",
          },
        },
        games: [{
          map_number: 1,
          official_match_id: "8937335830",
          identity_reason: "confirmed_map_result",
          identity_evidence: {
            method: "confirmed_settlement_reconciliation",
            official_source: "confirmed_map_result",
          },
          status: "available",
          source: "opendota",
          enrichment: {
            provider: "stratz",
            status: "available",
            reason: "player_positions_available",
            observed_at: "2026-08-07T11:00:00+00:00",
          },
          fetched_at: "2026-08-07T11:00:00+00:00",
          result: {
            radiant_team_id: 1,
            dire_team_id: 2,
            radiant_team_name: "Radiant Team",
            dire_team_name: "Dire Team",
            radiant_win: true,
            duration_seconds: 2400,
            start_time: 1786096800,
            league_id: 77,
            league_name: "Test Event",
            radiant_score: 31,
            dire_score: 18,
          },
          players: [{
            player_slot: 0,
            account_id: 123,
            player_name: "Test Player",
            player_name_source: "opendota_name",
            side: "radiant",
            team_id: 1,
            hero_id: 23,
            hero_name: "Kunkka",
            hero_key: "kunkka",
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
            position: 1,
            position_source: "stratz",
            historical_average: null,
            items: [],
          }],
          draft: [],
          advantages: {
            gold: [{ minute: 0, value: 0 }, { minute: 10, value: 1200 }],
            xp: [{ minute: 0, value: 0 }, { minute: 10, value: 800 }],
          },
          objectives: [],
          teamfights: [],
          availability: {
            result: "available",
            players: "partial",
            player_names: "available",
            historical_averages: "missing",
            positions: "partial",
            draft: "missing",
            gold_advantage: "available",
            xp_advantage: "available",
            objectives: "missing",
            teamfights: "missing",
          },
        }],
        unresolved_maps: [],
    };
    renderWorkspace(true, {
      ...detail,
      postmatch: availablePostmatch,
      games: detail.games.map((game) => ({
        ...game,
        state: "ended",
        postmatch: availablePostmatch,
      })),
    });

    expect(screen.getByText("OpenDota #8937335830")).toBeInTheDocument();
    expect(screen.getByText("Kunkka")).toBeInTheDocument();
    expect(screen.getByText("STRATZ 增强 · 可用")).toBeInTheDocument();
    expect(screen.getByText("STRATZ")).toBeInTheDocument();
    expect(screen.getByText("31 : 18")).toBeInTheDocument();
  });

  it("shows the complete prematch snapshot while live odds are pending", async () => {
    const prematchDetail: MatchDetail = {
      ...detail,
      winner: null,
      games: detail.games.map((game) => ({
        ...game,
        state: "scheduled",
        winner: null,
        winner_timeline: [],
        prematch_winner: {
          observed_at: "2026-08-07T10:09:00+00:00",
          period: "map_1",
          complete: true,
          prices: { team_one: 1.65, team_two: 2.19 },
          probabilities: { team_one: 0.5703125, team_two: 0.4296875 },
        },
      })),
    };

    renderWorkspace(false, prematchDetail);

    expect(screen.getByText("赛前快照")).toBeInTheDocument();
    expect(screen.getAllByText(/赛前赔率/).length).toBeGreaterThan(0);
    expect(screen.getByText("1.65")).toBeInTheDocument();
    expect(screen.getByText("2.19")).toBeInTheDocument();
    expect(screen.queryByText("赔率 未收到")).not.toBeInTheDocument();
  });

  it("shows Series prematch odds without creating an actual Map", () => {
    const prematchWinner = {
      observed_at: "2026-08-07T10:09:00+00:00",
      period: "map_1",
      complete: true,
      prices: { team_one: 1.65, team_two: 2.19 },
      probabilities: { team_one: 0.5703125, team_two: 0.4296875 },
    };
    const prematchDetail: MatchDetail = {
      ...detail,
      lifecycle: "upcoming",
      current_map_number: null,
      winner: null,
      prematch_winner: prematchWinner,
      latest_vision: null,
      games: [],
      market_evidence: [{
        market_id: "raybet-1:map_1",
        map_number: 1,
        period: "map_1",
        status: "market_only",
        reason: "no_play_evidence",
        prematch_winner: prematchWinner,
        winner_timeline: [],
        odds_coverage: detail.games[0].odds_coverage,
        markets: [],
      }],
    };

    renderWorkspace(false, prematchDetail, {
      ...match,
      lifecycle: "upcoming",
      current_map_number: null,
      winner: null,
      prematch_winner: prematchWinner,
      latest_vision: null,
      readiness: {
        odds: {
          status: "ready",
          observed_at: prematchWinner.observed_at,
          age_seconds: 30,
        },
        mapping: {
          status: "missing",
          count: 0,
          total_count: 0,
          reasons: [],
        },
        vision: {
          status: "missing",
          observed_at: null,
          age_seconds: null,
          reason: "waiting_for_watch_window",
          watch_starts_at: "2026-08-07T09:30:00+00:00",
        },
      },
    });

    expect(screen.getByText("赛前快照")).toBeInTheDocument();
    expect(screen.getByText("第 1 局盘口 · 尚未开打")).toBeInTheDocument();
    expect(screen.getByText("1.65")).toBeInTheDocument();
    expect(screen.getByText("2.19")).toBeInTheDocument();
    expect(screen.queryByText("赔率 未收到")).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /第 1 局/ })).not.toBeInTheDocument();
    expect(screen.getByText("Vision 将于 08/07 17:30:00 自动开始直播探测")).toBeInTheDocument();
  });

  it("requires the operator to assign every hero to an explicit position", async () => {
    const editableDetail: MatchDetail = {
      ...detail,
      games: detail.games.map((game) => ({ ...game, draft_mapping: null })),
      draft_context: {
        status: "unavailable",
        reason: "canonical_teams_unresolved",
        source: "raybet_exact_name",
        teams: [],
      },
    };
    renderWorkspace(false, editableDetail);

    fireEvent.click(screen.getByRole("button", { name: "录入阵容" }));
    await waitFor(() => expect(screen.getAllByRole("option", { name: "Alpha · A" })).toHaveLength(2));
    fireEvent.change(screen.getByRole("combobox", { name: "选择天辉队伍" }), {
      target: { value: "11" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: "选择夜魇队伍" }), {
      target: { value: "22" },
    });
    expect(screen.queryByRole("button", { name: "应用 HUD 识别阵容" })).not.toBeInTheDocument();
    for (const [label, offset] of [
      ["天辉", 0],
      ["夜魇", 5],
    ] as const) {
      for (let position = 1; position <= 5; position += 1) {
        fireEvent.click(screen.getByRole("button", {
          name: `选择 ${label} ${position} 号位英雄`,
        }));
        fireEvent.click(await screen.findByRole("button", {
          name: `Hero ${offset + position}`,
        }));
      }
    }
    fireEvent.click(screen.getByRole("checkbox", { name: "锁定后允许生成实时阵容预测" }));
    fireEvent.change(screen.getByRole("textbox", { name: "阵容证据 URL" }), {
      target: { value: "https://example.test/evidence/raybet-1/map-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交阵容" }));

    await waitFor(() => expect(api.saveLiveDraftMapping).toHaveBeenCalledTimes(1));
    const savedSlots = api.saveLiveDraftMapping.mock.calls[0][2] as typeof slots;
    expect(savedSlots.filter((slot) => slot.side === "radiant").map((slot) => slot.team_id))
      .toEqual([11, 11, 11, 11, 11]);
    expect(savedSlots.map((slot) => slot.hero_id)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
    expect(api.saveLiveDraftMapping.mock.calls[0][4]).toBe(
      "https://example.test/evidence/raybet-1/map-1",
    );
    expect(screen.getByText("阵容预测已自动保存")).toBeInTheDocument();
  });
});
