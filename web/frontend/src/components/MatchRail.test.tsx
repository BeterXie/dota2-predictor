import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { MonitorMatch } from "../types";
import { MatchRail } from "./MatchRail";

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

const decisionMatch: MonitorMatch = {
  ...match,
  latest_vision: {
    captured_at: "2026-07-16T12:00:04+00:00",
    map_number: 2,
    game_clock_seconds: 1_234,
    screen_state: "gameplay",
    confirmed: 1,
    clock_confidence: 0.99,
    draft_confidence: 0.98,
  },
  latest_decision: {
    decided_at: "2026-07-16T12:00:04+00:00",
    map_number: 2,
    underdog_side: "team_one",
    market_probability: 0.44,
    model_probability: 0.56,
    edge: 0.12,
    eligible: 1,
    reason: "eligible",
    strategy_version: "test-v1",
  },
};

describe("MatchRail", () => {
  it("renders the live collection as a full page list", () => {
    const onSelect = vi.fn();
    const view = render(
      <MatchRail
        matches={[decisionMatch]}
        mode="live"
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        onSelect={onSelect}
        selectedId={null}
        variant="page"
      />,
    );

    expect(screen.getByLabelText("实时赛事列表")).toHaveClass("match-list-page");
    expect(screen.queryByRole("button", { name: /切换实时赛事/ })).not.toBeInTheDocument();
    expect(view.container.querySelector(".match-list-columns")).not.toBeInTheDocument();
    expect(screen.getByText("第 2 局 · 20:34")).toBeInTheDocument();
    expect(screen.getByText("策略合格")).toBeInTheDocument();
    expect(screen.getByText("关注 Radiant")).toBeInTheDocument();

    const row = view.container.querySelector(".match-row");
    if (!(row instanceof HTMLButtonElement)) throw new Error("match row not found");
    fireEvent.click(row);
    expect(onSelect).toHaveBeenCalledWith(match.raybet_match_id);
  });

  it("labels a full history collection as a history list", () => {
    render(
      <MatchRail
        matches={[{ ...match, lifecycle: "ended", history_eligible: true }]}
        mode="history"
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        onSelect={vi.fn()}
        selectedId={null}
        variant="page"
      />,
    );

    expect(screen.getByLabelText("历史赛事列表")).toBeInTheDocument();
  });

  it("keeps the desktop rail body rendered and only toggles the mobile presentation", () => {
    const onSelect = vi.fn();
    const view = render(
      <MatchRail
        matches={[match]}
        mode="live"
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        onSelect={onSelect}
        selectedId={match.raybet_match_id}
      />,
    );

    const rail = screen.getByLabelText("赛事列表");
    const body = view.container.querySelector(".rail-body");
    expect(body).toBeInTheDocument();
    expect(body?.closest("details")).toBeNull();

    const toggle = screen.getByRole("button", { name: /切换实时赛事/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    expect(rail).toHaveClass("expanded");
    expect(toggle).toHaveAttribute("aria-expanded", "true");

    const row = view.container.querySelector(".match-row");
    if (!(row instanceof HTMLButtonElement)) throw new Error("match row not found");
    fireEvent.click(row);
    expect(onSelect).toHaveBeenCalledWith(match.raybet_match_id);
    expect(rail).not.toHaveClass("expanded");
  });
});
