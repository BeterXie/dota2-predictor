import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { MonitorMatch, MonitorSnapshot } from "./types";

const api = vi.hoisted(() => ({
  acknowledgeAlert: vi.fn(),
  approveAutomaticMapping: vi.fn(),
  controlComponent: vi.fn(),
  createAutomaticMapping: vi.fn(),
  createControlSession: vi.fn(),
  fetchBootstrap: vi.fn(),
  fetchMappings: vi.fn(),
  fetchMatchDetail: vi.fn(),
  fetchMonitorHistory: vi.fn(),
  invalidateMapping: vi.fn(),
  snapshotStream: vi.fn(),
}));

vi.mock("./api", () => api);
vi.mock("./components/MatchRail", () => ({
  MatchRail: ({ matches }: { matches: MonitorMatch[] }) => (
    <nav>{matches.map((match) => <span key={match.raybet_match_id}>{match.raybet_match_id}</span>)}</nav>
  ),
}));
vi.mock("./components/MatchWorkspace", () => ({
  MatchWorkspace: ({ match }: { match: MonitorMatch | null }) => (
    <main>{match ? `workspace-${match.raybet_match_id}` : "empty-workspace"}</main>
  ),
}));
vi.mock("./components/OperationsPanel", () => ({
  OperationsPanel: () => <main>operations</main>,
}));

import App from "./App";

const match: MonitorMatch = {
  raybet_match_id: "live-1",
  tournament: "Event",
  team_one: "Radiant",
  team_two: "Dire",
  scheduled_at: "2026-08-07T10:00:00+00:00",
  best_of: 3,
  provider_status: "2",
  updated_at: "2026-08-07T10:01:00+00:00",
  lifecycle: "live",
  history_eligible: false,
  winner: null,
  latest_vision: null,
};

const snapshot: MonitorSnapshot = {
  generated_at: "2026-08-07T10:01:00+00:00",
  cursor: "cursor-1",
  mapping_revision: "mapping-1",
  health: [],
  matches: [match],
  alerts: [],
  summary: {
    total: 1,
    live: 1,
    upcoming: 0,
    degraded: 0,
    ended: 0,
    live_view: { total: 1, live: 1, upcoming: 0, degraded: 0, ended: 0 },
    history_view: { total: 0, live: 0, upcoming: 0, degraded: 0, ended: 0 },
    unhealthy_components: 0,
    active_alerts: 0,
  },
};

const historyMatch: MonitorMatch = {
  ...match,
  raybet_match_id: "history-1",
  lifecycle: "ended",
  history_eligible: true,
};

describe("App", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/monitor");
    api.fetchBootstrap.mockResolvedValue(snapshot);
    api.fetchMatchDetail.mockResolvedValue({
      ...match,
      winner_timeline: [],
      vision: [],
      markets: [],
    });
    api.fetchMonitorHistory.mockResolvedValue({
      items: [historyMatch],
      next_cursor: null,
      has_more: false,
    });
    api.createControlSession.mockResolvedValue({
      csrf_token: "csrf",
      expires_in: 3600,
      client_host: "127.0.0.1",
      components: [],
    });
    api.snapshotStream.mockReturnValue({
      addEventListener: vi.fn(),
      close: vi.fn(),
      onerror: null,
    });
  });

  it("opens the realtime product with the selected RayBet match", async () => {
    render(<App />);

    expect(screen.getByText("Dota 2 实时阵容预测")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("live-1")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("workspace-live-1")).toBeInTheDocument());
  });

  it("restores the preferred live match after visiting history", async () => {
    render(<App />);

    await waitFor(() => expect(screen.getByText("workspace-live-1")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("tab", { name: "历史结果" }));
    await waitFor(() => expect(screen.getByText("workspace-history-1")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("tab", { name: "实时赛事" }));
    await waitFor(() => expect(screen.getByText("workspace-live-1")).toBeInTheDocument());
    expect(screen.queryByText("empty-workspace")).not.toBeInTheDocument();
  });
});
