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

describe("MatchRail", () => {
  it("renders the live collection as a full page list", () => {
    const onSelect = vi.fn();
    const view = render(
      <MatchRail
        matches={[match]}
        mode="live"
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        onSelect={onSelect}
        selectedId={null}
        variant="page"
      />,
    );

    expect(screen.getByLabelText("滚球赛事列表")).toHaveClass("match-list-page");
    expect(screen.queryByRole("button", { name: /切换滚球赛事/ })).not.toBeInTheDocument();
    expect(view.container.querySelector(".match-list-columns")).toBeInTheDocument();

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

    const toggle = screen.getByRole("button", { name: /切换滚球赛事/ });
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
