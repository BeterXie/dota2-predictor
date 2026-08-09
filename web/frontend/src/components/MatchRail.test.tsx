import { FluentProvider, webDarkTheme } from "@fluentui/react-components";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { MonitorMatch } from "../types";
import { MatchRail } from "./MatchRail";


const liveMatch: MonitorMatch = {
  raybet_match_id: "live-1",
  official_match_id: "8123456789",
  display_name: "官方 Match ID 8123456789 · Alpha vs Beta · Elite League",
  tournament: "Elite League",
  team_one: "Alpha",
  team_two: "Beta",
  scheduled_at: "2026-08-09T10:00:00+00:00",
  best_of: 3,
  provider_status: "2",
  updated_at: "2026-08-09T10:12:00+00:00",
  lifecycle: "live",
  history_eligible: false,
  winner: {
    observed_at: "2026-08-09T10:11:00+00:00",
    period: "map_1",
    complete: true,
    prices: { team_one: 1.72, team_two: 2.08 },
    probabilities: { team_one: 0.547, team_two: 0.453 },
  },
  latest_vision: {
    captured_at: "2026-08-09T10:11:00+00:00",
    map_number: 1,
    game_clock_seconds: 420,
    screen_state: "game",
    confirmed: 1,
    clock_confidence: 0.98,
    draft_confidence: 0.99,
  },
};

const prematchMatch: MonitorMatch = {
  ...liveMatch,
  raybet_match_id: "prematch-1",
  official_match_id: null,
  display_name: "RayBet prematch-1 · Gamma vs Delta · EPL 大师赛",
  tournament: "EPL 大师赛",
  team_one: "Gamma",
  team_two: "Delta",
  provider_status: "1",
  lifecycle: "degraded",
  scheduled_at: "2026-08-09T17:00:00+00:00",
  winner: null,
  latest_vision: null,
};


function renderRail(onSelect = vi.fn()) {
  return {
    onSelect,
    ...render(
      <FluentProvider theme={webDarkTheme}>
        <MatchRail
          matches={[prematchMatch, liveMatch]}
          mode="live"
          onSelect={onSelect}
          selectedId={null}
          variant="page"
        />
      </FluentProvider>,
    ),
  };
}


describe("MatchRail", () => {
  it("presents live and pre-match fixtures as full-page grouped rows", () => {
    const { onSelect } = renderRail();

    expect(screen.getByLabelText("实时与赛前赛事列表")).toHaveClass("match-list-page");
    expect(screen.getByText("正在进行")).toBeInTheDocument();
    expect(screen.getByText("赛前赛事")).toBeInTheDocument();
    expect(screen.getByText("官方 Match ID 8123456789")).toBeInTheDocument();
    expect(screen.getByText("RayBet prematch-1")).toBeInTheDocument();
    expect(screen.getByText("赛前")).toBeInTheDocument();
    expect(screen.queryByText("数据降级")).not.toBeInTheDocument();
    expect(screen.getByText("1.72")).toBeInTheDocument();
    expect(screen.getByText("2.08")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Gamma.*Delta/s }));
    expect(onSelect).toHaveBeenCalledWith("prematch-1");
  });

  it("filters the page without losing the grouped presentation", () => {
    renderRail();

    fireEvent.change(screen.getByRole("textbox", { name: "搜索赛事" }), {
      target: { value: "Gamma" },
    });

    const list = screen.getByLabelText("实时与赛前赛事列表");
    expect(within(list).getByText("Gamma")).toBeInTheDocument();
    expect(within(list).queryByText("Alpha")).not.toBeInTheDocument();
    expect(within(list).queryByText("正在进行")).not.toBeInTheDocument();
    expect(within(list).getByText("赛前赛事")).toBeInTheDocument();
  });
});
