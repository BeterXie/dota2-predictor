import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { MonitorMatch } from "../types";
import { MatchRail } from "./MatchRail";

afterEach(cleanup);

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
  readiness: {
    ...match.readiness,
    model: { status: "ready" },
  },
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
    expect(view.container.querySelector(".match-row-strategy strong"))
      .toHaveTextContent("策略合格");
    expect(screen.getByText("关注 Radiant")).toBeInTheDocument();

    const row = view.container.querySelector(".match-row");
    if (!(row instanceof HTMLButtonElement)) throw new Error("match row not found");
    fireEvent.click(row);
    expect(onSelect).toHaveBeenCalledWith(match.raybet_match_id);
  });

  it("only shows a confirmed clock when vision readiness is usable", () => {
    const staleMatch: MonitorMatch = {
      ...decisionMatch,
      readiness: {
        ...decisionMatch.readiness,
        vision: { status: "stale" },
      },
    };
    const view = render(
      <MatchRail
        matches={[staleMatch]}
        mode="live"
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        onSelect={vi.fn()}
        selectedId={null}
        variant="page"
      />,
    );

    expect(screen.getByText("等待可信比赛时钟")).toBeInTheDocument();
    expect(screen.queryByText("第 2 局 · 20:34")).not.toBeInTheDocument();

    view.rerender(
      <MatchRail
        matches={[{
          ...staleMatch,
          readiness: {
            ...staleMatch.readiness,
            vision: { status: "delayed" },
          },
        }]}
        mode="live"
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        onSelect={vi.fn()}
        selectedId={null}
        variant="page"
      />,
    );
    expect(screen.getByText("第 2 局 · 20:34")).toBeInTheDocument();
  });

  it("does not describe rejected or invalid decisions as current attention", () => {
    const rejectedMatch: MonitorMatch = {
      ...decisionMatch,
      latest_decision: {
        ...decisionMatch.latest_decision!,
        eligible: 0,
        reason: "edge_below_threshold",
      },
    };
    const view = render(
      <MatchRail
        matches={[rejectedMatch]}
        mode="live"
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        onSelect={vi.fn()}
        selectedId={null}
        variant="page"
      />,
    );

    expect(screen.getByText("策略拒绝")).toBeInTheDocument();
    expect(screen.getByText("已拒绝 Radiant")).toBeInTheDocument();
    expect(screen.queryByText("当前关注 Radiant")).not.toBeInTheDocument();

    view.rerender(
      <MatchRail
        matches={[{
          ...rejectedMatch,
          latest_decision: {
            ...rejectedMatch.latest_decision!,
            reason: "rosh_profile_mismatch",
          },
        }]}
        mode="live"
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        onSelect={vi.fn()}
        selectedId={null}
        variant="page"
      />,
    );
    expect(screen.getByText("证据需复核")).toBeInTheDocument();
    expect(screen.getByText("涉及 Radiant")).toBeInTheDocument();
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
    expect(screen.queryByLabelText("关注状态筛选")).not.toBeInTheDocument();
  });

  it("filters the live attention queue with visible counts", () => {
    const reviewMatch: MonitorMatch = {
      ...decisionMatch,
      raybet_match_id: "match-review",
      team_one: "Review team",
      latest_decision: {
        ...decisionMatch.latest_decision!,
        reason: "rosh_profile_mismatch",
      },
    };
    const degradedMatch: MonitorMatch = {
      ...match,
      raybet_match_id: "match-degraded",
      team_one: "Delayed team",
      readiness: {
        ...match.readiness,
        odds: { status: "stale" },
      },
    };
    const waitingMatch: MonitorMatch = {
      ...match,
      raybet_match_id: "match-waiting",
      team_one: "Waiting team",
      readiness: decisionMatch.readiness,
    };
    const upcomingMatch: MonitorMatch = {
      ...waitingMatch,
      raybet_match_id: "match-upcoming",
      team_one: "Upcoming team",
      lifecycle: "upcoming",
    };
    const view = render(
      <MatchRail
        matches={[waitingMatch, upcomingMatch, degradedMatch, decisionMatch, reviewMatch]}
        mode="live"
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        onSelect={vi.fn()}
        selectedId={null}
        variant="page"
      />,
    );

    expect(screen.getByRole("button", { name: /需处理 3/ })).toBeInTheDocument();
    expect(Array.from(view.container.querySelectorAll(".match-row")).map(
      (row) => row.textContent,
    )).toEqual(expect.arrayContaining([
      expect.stringContaining("Review team"),
      expect.stringContaining("Radiant"),
      expect.stringContaining("Delayed team"),
    ]));

    fireEvent.click(screen.getByRole("button", { name: /需处理 3/ }));

    expect(view.container.querySelectorAll(".match-row")).toHaveLength(3);
    expect(screen.queryByText("Waiting team")).not.toBeInTheDocument();
    expect(screen.queryByText("Upcoming team")).not.toBeInTheDocument();
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
