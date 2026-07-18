import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { MatchDetail, MonitorMatch } from "../types";


vi.mock("@fluentui/react-components", () => ({
  Badge: ({ children }: { children: ReactNode }) => <span>{children}</span>,
  Button: ({
    as,
    children,
    href,
  }: {
    as?: string;
    children: ReactNode;
    href?: string;
  }) => as === "a" ? <a href={href}>{children}</a> : <button>{children}</button>,
  Skeleton: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SkeletonItem: () => <div />,
}));
vi.mock("./ProbabilityChart", () => ({
  ProbabilityChart: ({ onPeriodChange }: { onPeriodChange: (value: string) => void }) => (
    <button onClick={() => onPeriodChange("map_1")}>probability-chart</button>
  ),
}));
vi.mock("./PostmatchIntelligencePanel", () => ({
  PostmatchIntelligencePanel: ({ mapNumber }: { mapNumber: number | null }) => (
    <div>postmatch-map-{mapNumber ?? "none"}</div>
  ),
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

  it("labels available entry links by their proven kind", () => {
    const pageMatch: MonitorMatch = {
      ...match,
      watch_link: {
        kind: "match_page",
        availability: "available",
        url: "https://www.ray086.com/sports/esports",
        reason: "captured_raybet_match_page",
      },
    };
    const view = render(
      <MatchWorkspace
        detail={null}
        error={null}
        loading={false}
        match={pageMatch}
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getByRole("link", { name: "打开比赛页" })).toHaveAttribute(
      "href",
      "https://www.ray086.com/sports/esports",
    );
    expect(screen.queryByRole("link", { name: "打开直播" })).not.toBeInTheDocument();

    view.rerender(
      <MatchWorkspace
        detail={null}
        error={null}
        loading={false}
        match={{
          ...match,
          watch_link: {
            kind: "public_stream",
            availability: "available",
            url: "https://qplay.ehome.gg/live/42.m3u8",
            reason: "verified_unsigned_stream",
          },
        }}
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.getByRole("link", { name: "打开直播" })).toHaveAttribute(
      "href",
      "https://qplay.ehome.gg/live/42.m3u8",
    );
    expect(screen.queryByRole("link", { name: "打开比赛页" })).not.toBeInTheDocument();
  });

  it("does not fall back to a legacy or unavailable live_url", () => {
    const { rerender } = render(
      <MatchWorkspace
        detail={null}
        error={null}
        loading={false}
        match={{
          ...match,
          live_url: "https://qplay.ehome.gg/live/42.m3u8",
        }}
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.queryByRole("link", { name: /打开/ })).not.toBeInTheDocument();

    rerender(
      <MatchWorkspace
        detail={null}
        error={null}
        loading={false}
        match={{
          ...match,
          watch_link: {
            kind: "none",
            availability: "unavailable",
            url: null,
            reason: "no_safe_entry",
          },
        }}
        now={Date.parse("2026-07-16T12:00:05+00:00")}
        replay={false}
      />,
    );

    expect(screen.queryByRole("link", { name: /打开/ })).not.toBeInTheDocument();

    for (const watch_link of [
      {
        kind: "match_page",
        availability: "available",
        url: "javascript:alert(1)",
        reason: "malicious_scheme",
      },
      {
        kind: "match_page",
        availability: "available",
        url: "https://foreign.example/sports/esports",
        reason: "foreign_host",
      },
      {
        kind: "public_stream",
        availability: "available",
        url: "https://qplay.ehome.gg/live/42.m3u8?token=stripped",
        reason: "signed_stream",
      },
    ] as const) {
      rerender(
        <MatchWorkspace
          detail={null}
          error={null}
          loading={false}
          match={{ ...match, watch_link }}
          now={Date.parse("2026-07-16T12:00:05+00:00")}
          replay={false}
        />,
      );
      expect(screen.queryByRole("link", { name: /打开/ })).not.toBeInTheDocument();
    }
  });

  it("shares the replay map selection with the exact postmatch panel", () => {
    const replayDetail = detail(1, "ready");
    replayDetail.lifecycle = "ended";
    replayDetail.history_eligible = true;
    replayDetail.winner_timeline = [
      {
        observed_at: "2026-07-16T12:00:00+00:00",
        period: "map_1",
        prices: { team_one: 1.8, team_two: 2.1 },
        probabilities: { team_one: 0.54, team_two: 0.46 },
        status: { team_one: "open", team_two: "open" },
      },
      {
        observed_at: "2026-07-16T13:00:00+00:00",
        period: "map_2",
        prices: { team_one: 1.7, team_two: 2.2 },
        probabilities: { team_one: 0.57, team_two: 0.43 },
        status: { team_one: "open", team_two: "open" },
      },
    ];

    const view = render(
      <MatchWorkspace
        detail={replayDetail}
        error={null}
        loading={false}
        match={replayDetail}
        now={Date.parse("2026-07-16T14:00:00+00:00")}
        replay
      />,
    );

    expect(screen.getByText("postmatch-map-2")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "probability-chart" }));
    expect(screen.getByText("postmatch-map-1")).toBeInTheDocument();

    const nextMatch = { ...replayDetail, raybet_match_id: "match-2" };
    view.rerender(
      <MatchWorkspace
        detail={nextMatch}
        error={null}
        loading={false}
        match={nextMatch}
        now={Date.parse("2026-07-16T14:00:00+00:00")}
        replay
      />,
    );
    expect(screen.getByText("postmatch-map-2")).toBeInTheDocument();
  });
});
