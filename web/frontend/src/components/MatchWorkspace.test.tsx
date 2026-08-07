import { FluentProvider, webDarkTheme } from "@fluentui/react-components";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { MatchDetail, MonitorMatch } from "../types";

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

function renderWorkspace(replay = false, currentDetail = detail) {
  return render(
    <FluentProvider theme={webDarkTheme}>
      <MatchWorkspace
        csrfToken="csrf"
        detail={currentDetail}
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
    api.fetchLiveDraftPrediction.mockResolvedValue({ status: "not_found", prediction: null });
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
      created_at: "2026-08-07T10:09:30+00:00",
      slots: savedSlots,
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

  it("lets the operator select canonical teams and apply the HUD lineup", async () => {
    const editableDetail: MatchDetail = {
      ...detail,
      draft_mapping: null,
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
    fireEvent.click(screen.getByRole("button", { name: "应用 HUD 识别阵容" }));

    await waitFor(() => expect(screen.getByText("Hero 1")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("checkbox", { name: "锁定后允许生成实时阵容预测" }));
    fireEvent.click(screen.getByRole("button", { name: "提交阵容" }));

    await waitFor(() => expect(api.saveLiveDraftMapping).toHaveBeenCalledTimes(1));
    const savedSlots = api.saveLiveDraftMapping.mock.calls[0][2] as typeof slots;
    expect(savedSlots.filter((slot) => slot.side === "radiant").map((slot) => slot.team_id))
      .toEqual([11, 11, 11, 11, 11]);
    expect(savedSlots.map((slot) => slot.hero_id)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
  });
});
