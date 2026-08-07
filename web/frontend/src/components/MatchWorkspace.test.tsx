import { FluentProvider, webDarkTheme } from "@fluentui/react-components";
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { MatchDetail, MonitorMatch } from "../types";

const api = vi.hoisted(() => ({
  correctLiveGameSnapshot: vi.fn(),
  createLiveDraftPrediction: vi.fn(),
  fetchHeroGrid: vi.fn(),
  fetchLiveDraftPrediction: vi.fn(),
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
  winner_timeline: [],
  vision: [match.latest_vision!],
  markets: [],
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
  draft_context: {
    status: "ready",
    reason: "strict_mapping_available",
    source: "strict_mapping",
    teams: [
      { match_side: "team_one", team_id: 1, team_name: "Radiant Team" },
      { match_side: "team_two", team_id: 2, team_name: "Dire Team" },
    ],
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
};

function renderWorkspace(replay = false) {
  return render(
    <FluentProvider theme={webDarkTheme}>
      <MatchWorkspace
        csrfToken="csrf"
        detail={detail}
        error={null}
        loading={false}
        match={match}
        replay={replay}
      />
    </FluentProvider>,
  );
}

describe("MatchWorkspace", () => {
  beforeEach(() => {
    api.fetchHeroGrid.mockResolvedValue({ str: [], agi: [], int: [], all: [] });
    api.fetchLiveDraftPrediction.mockResolvedValue({ status: "not_found", prediction: null });
  });

  it("keeps HUD evidence and the locked live prediction action", async () => {
    renderWorkspace();

    expect(screen.getByText("HUD 与 Vision 证据")).toBeInTheDocument();
    expect(screen.getByText("7:00")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "生成实时阵容预测" })).toBeInTheDocument();
    });
    expect(screen.getByText(/不使用击杀、经济、经验/)).toBeInTheDocument();
  });

  it("keeps historical replay read-only", async () => {
    renderWorkspace(true);

    await waitFor(() => expect(api.fetchLiveDraftPrediction).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "生成实时阵容预测" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "追加人工修正" })).not.toBeInTheDocument();
  });
});
