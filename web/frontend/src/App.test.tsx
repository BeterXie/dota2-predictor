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
  MatchRail: ({
    hasMore,
    matches,
    onLoadMore,
    onSelect,
  }: {
    hasMore?: boolean;
    matches: MonitorMatch[];
    onLoadMore?: () => void;
    onSelect: (matchId: string) => void;
  }) => (
    <nav>
      {matches.map((match) => (
        <button key={match.raybet_match_id} onClick={() => onSelect(match.raybet_match_id)}>
          {match.raybet_match_id}
        </button>
      ))}
      {hasMore && <button onClick={onLoadMore}>加载更多历史赛事</button>}
    </nav>
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
vi.mock("./components/FanMatchRecap", () => ({
  FanMatchRecap: ({ match }: { match: MonitorMatch | null }) => (
    <main>{match ? `recap-${match.raybet_match_id}` : "empty-recap"}</main>
  ),
}));
vi.mock("./components/VisionCalibrationPage", () => ({
  VisionCalibrationPage: ({ csrfToken }: { csrfToken: string | null }) => (
    <main>vision-calibration-{csrfToken || "readonly"}</main>
  ),
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
  display_name: "RayBet Series history-1 · Radiant vs Dire · Event",
  lifecycle: "ended",
  history_eligible: true,
};
const historyMatch2: MonitorMatch = {
  ...historyMatch,
  raybet_match_id: "history-2",
};

describe("App", () => {
  beforeEach(() => {
    vi.clearAllMocks();
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
    fireEvent.click(await screen.findByRole("button", { name: "live-1" }));
    await waitFor(() => expect(screen.getByText("workspace-live-1")).toBeInTheDocument());
  });

  it("does not cancel a slow match detail request when an SSE snapshot arrives", async () => {
    let snapshotListener: (event: MessageEvent<string>) => void = (_event) => {
      throw new Error("snapshot listener was not registered");
    };
    let resolveDetail: (value: unknown) => void = (_value) => {
      throw new Error("detail resolver was not registered");
    };
    api.snapshotStream.mockReturnValue({
      addEventListener: vi.fn((name: string, listener: (event: MessageEvent<string>) => void) => {
        if (name === "snapshot") snapshotListener = listener;
      }),
      close: vi.fn(),
      onerror: null,
    });
    api.fetchMatchDetail.mockReturnValue(new Promise((resolve) => {
      resolveDetail = resolve;
    }));
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "live-1" }));
    await waitFor(() => expect(api.fetchMatchDetail).toHaveBeenCalledTimes(1));
    snapshotListener({
      data: JSON.stringify({ ...snapshot, cursor: "next-cursor" }),
    } as MessageEvent<string>);
    expect(api.fetchMatchDetail).toHaveBeenCalledTimes(1);

    resolveDetail({ ...match, winner_timeline: [], vision: [], markets: [] });
    await waitFor(() => expect(screen.getByText("workspace-live-1")).toBeInTheDocument());
  });

  it("keeps ended and history-eligible matches out of the realtime list", async () => {
    api.fetchBootstrap.mockResolvedValue({
      ...snapshot,
      matches: [
        match,
        { ...historyMatch, raybet_match_id: "ended-in-snapshot" },
        {
          ...match,
          raybet_match_id: "stale-prematch",
          provider_status: "1",
          lifecycle: "degraded",
          history_eligible: true,
        },
      ],
    });

    render(<App />);

    expect(await screen.findByRole("button", { name: "live-1" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "ended-in-snapshot" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "stale-prematch" })).not.toBeInTheDocument();
  });

  it("returns to each section list before opening another match", async () => {
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "live-1" }));
    await waitFor(() => expect(screen.getByText("workspace-live-1")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("tab", { name: "历史结果" }));
    const historyButton = await screen.findByRole("button", { name: "history-1" });
    expect(screen.queryByText("workspace-history-1")).not.toBeInTheDocument();
    fireEvent.click(historyButton);
    await waitFor(() => expect(screen.getByText("workspace-history-1")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("tab", { name: "实时赛事" }));
    const liveButton = await screen.findByRole("button", { name: "live-1" });
    expect(screen.queryByText("workspace-live-1")).not.toBeInTheDocument();
    fireEvent.click(liveButton);
    await waitFor(() => expect(screen.getByText("workspace-live-1")).toBeInTheDocument());
    expect(screen.queryByText("empty-workspace")).not.toBeInTheDocument();
  });

  it("loads subsequent history pages without dropping the first page", async () => {
    api.fetchMonitorHistory.mockImplementation((cursor?: string | null) => Promise.resolve(
      cursor
        ? { items: [historyMatch2], next_cursor: null, has_more: false }
        : { items: [historyMatch], next_cursor: "cursor-2", has_more: true },
    ));
    render(<App />);

    fireEvent.click(screen.getByRole("tab", { name: "历史结果" }));
    await waitFor(() => expect(screen.getByText("history-1")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "加载更多历史赛事" }));

    await waitFor(() => expect(screen.getByText("history-2")).toBeInTheDocument());
    expect(screen.getByText("history-1")).toBeInTheDocument();
    expect(api.fetchMonitorHistory).toHaveBeenLastCalledWith("cursor-2");
  });

  it("opens the player recap as a separate history presentation", async () => {
    window.history.replaceState(null, "", "/monitor?view=recap");
    render(<App />);

    expect(await screen.findByText("Dota 2 比赛复盘")).toBeInTheDocument();
    expect(screen.getByRole("banner")).toHaveClass("recap-header-mode");
    fireEvent.click(await screen.findByRole("button", { name: "history-1" }));

    await waitFor(() => expect(screen.getByText("recap-history-1")).toBeInTheDocument());
    expect(screen.getByText(/^Event · /)).toBeInTheDocument();
    expect(screen.queryByText(historyMatch.display_name!)).not.toBeInTheDocument();
    expect(screen.queryByText("workspace-history-1")).not.toBeInTheDocument();
  });

  it("opens Vision calibration as a standalone page without the match rail", async () => {
    render(<App />);

    fireEvent.click(screen.getByRole("tab", { name: "Vision 校正" }));
    expect(await screen.findByText("vision-calibration-csrf")).toBeInTheDocument();
    expect(screen.queryByText("live-1")).not.toBeInTheDocument();
  });

  it("supports the direct Vision calibration route", async () => {
    window.history.replaceState(null, "", "/monitor?view=vision");
    render(<App />);

    expect(await screen.findByText("vision-calibration-csrf")).toBeInTheDocument();
    expect(api.fetchBootstrap).not.toHaveBeenCalled();
    expect(api.snapshotStream).not.toHaveBeenCalled();
  });
});
