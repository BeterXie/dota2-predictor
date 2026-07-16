import { cleanup, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { MatchDetail, MonitorMatch } from "../types";


vi.mock("@fluentui/react-components", () => ({
  Badge: ({ children }: { children: ReactNode }) => <span>{children}</span>,
  Button: ({ children }: { children: ReactNode }) => <button>{children}</button>,
  Skeleton: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SkeletonItem: () => <div />,
}));
vi.mock("./ProbabilityChart", () => ({
  ProbabilityChart: () => <div>probability-chart</div>,
}));

import { MatchWorkspace } from "./MatchWorkspace";


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

function detail(
  confirmed: number,
  visionStatus: MonitorMatch["readiness"]["vision"]["status"],
): MatchDetail {
  return {
    ...match,
    latest_vision: {
      captured_at: "2026-07-16T12:00:00+00:00",
      observed_at: "2026-07-16T12:00:00+00:00",
      map_number: 1,
      game_clock_seconds: 120,
      screen_state: "game",
      confirmed,
      clock_confidence: 0.99,
      draft_confidence: 0.99,
    },
    readiness: {
      ...match.readiness,
      vision: { status: visionStatus },
    },
    winner_timeline: [],
    decisions: [],
    vision: [],
    markets: [],
  };
}

describe("MatchWorkspace trusted vision clock", () => {
  afterEach(cleanup);

  it("requires a confirmed and fresh-enough vision observation", () => {
    const view = render(
      <MatchWorkspace
        detail={detail(0, "ready")}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getByText("暂无可信比赛时钟")).toBeInTheDocument();
    expect(screen.getByText("局数待确认")).toBeInTheDocument();
    expect(screen.queryByText("第 1 局")).not.toBeInTheDocument();

    view.rerender(
      <MatchWorkspace
        detail={detail(1, "stale")}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        replay={false}
      />,
    );
    expect(screen.getByText("暂无可信比赛时钟")).toBeInTheDocument();
    expect(screen.getByText("局数待确认")).toBeInTheDocument();

    view.rerender(
      <MatchWorkspace
        detail={detail(1, "delayed")}
        error={null}
        loading={false}
        match={match}
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        replay={false}
      />,
    );
    expect(screen.getByText("可信时钟 2:00")).toBeInTheDocument();
  });
});
