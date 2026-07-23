import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  AlertIncident,
  ControlComponent,
  MappingRecord,
  MonitorMatch,
  MonitorSnapshot,
} from "./types";


const api = vi.hoisted(() => ({
  acknowledgeAlert: vi.fn(),
  approveAutomaticMapping: vi.fn(),
  controlComponent: vi.fn(),
  createAutomaticMapping: vi.fn(),
  createControlSession: vi.fn(),
  fetchBootstrap: vi.fn(),
  fetchControlComponents: vi.fn(),
  fetchMappings: vi.fn(),
  fetchMatchDetail: vi.fn(),
  fetchMonitorHistory: vi.fn(),
  invalidateMapping: vi.fn(),
  snapshotStream: vi.fn(),
}));

vi.mock("./api", () => api);
vi.mock("@fluentui/react-components", () => ({
  Switch: ({ "aria-label": ariaLabel, checked }: {
    "aria-label"?: string;
    checked?: boolean;
  }) => <input aria-label={ariaLabel} checked={checked} readOnly type="checkbox" />,
  Tab: ({ children, value }: { children: ReactNode; value: string }) => (
    <button data-tab-value={value}>{children}</button>
  ),
  TabList: ({ children, onTabSelect }: {
    children: ReactNode;
    onTabSelect?: (event: unknown, data: { value: string }) => void;
  }) => (
    <div
      onClick={(event) => {
        const tab = (event.target as HTMLElement).closest<HTMLButtonElement>(
          "button[data-tab-value]",
        );
        if (tab?.dataset.tabValue) onTabSelect?.(event, { value: tab.dataset.tabValue });
      }}
    >
      {children}
    </div>
  ),
}));
vi.mock("./components/MatchRail", () => ({
  MatchRail: ({ matches, onSelect, historyHasMore, historyLoading, onLoadMore }: {
    matches: MonitorMatch[];
    onSelect: (matchId: string) => void;
    historyHasMore?: boolean;
    historyLoading?: boolean;
    onLoadMore?: () => void;
  }) => (
    <nav>
      {matches.map((match) => (
        <button key={match.raybet_match_id} onClick={() => onSelect(match.raybet_match_id)}>
          select-{match.raybet_match_id}
        </button>
      ))}
      {(historyHasMore || historyLoading) && (
        <button disabled={historyLoading} onClick={onLoadMore}>
          load-more-history
        </button>
      )}
    </nav>
  ),
}));
vi.mock("./components/MatchWorkspace", () => ({
  MatchWorkspace: ({ replay }: { replay: boolean }) => (
    <main>{replay ? "odds-replay" : "live-workspace"}</main>
  ),
}));
vi.mock("./components/IntelligenceDashboard", () => ({
  IntelligenceDashboard: () => <section>opendota-postmatch</section>,
}));
vi.mock("./components/OperationsPanel", () => ({
  OperationsPanel: ({ alerts, components, controlMessage, match, mappings, onAcknowledge }: {
    alerts: AlertIncident[];
    components: ControlComponent[];
    controlMessage: string | null;
    match: MonitorMatch | null;
    mappings: MappingRecord[];
    onAcknowledge: (incidentId: number) => void;
  }) => (
    <aside>
      <span data-testid="selected-match">{match?.raybet_match_id || "none"}</span>
      {controlMessage && <span data-testid="control-message">{controlMessage}</span>}
      {alerts.map((alert) => (
        <span key={alert.incident_id}>
          incident-{alert.incident_id}-{alert.acknowledged_at ? "acknowledged" : "unacknowledged"}
        </span>
      ))}
      {alerts[0] && (
        <button onClick={() => onAcknowledge(alerts[0].incident_id)}>ack-first-alert</button>
      )}
      {components.map((component) => (
        <span key={component.component}>
          component-{component.component}-{component.status}
        </span>
      ))}
      {mappings.map((mapping) => (
        <span key={mapping.mapping_id}>mapping-{mapping.mapping_id}</span>
      ))}
    </aside>
  ),
}));

import App from "./App";


const match = (raybet_match_id: string): MonitorMatch => ({
  raybet_match_id,
  tournament: "Test event",
  team_one: `${raybet_match_id}-one`,
  team_two: `${raybet_match_id}-two`,
  scheduled_at: null,
  best_of: 3,
  provider_status: "1",
  live_url: null,
  updated_at: "2026-07-15T00:00:00+00:00",
  lifecycle: "degraded",
  history_eligible: false,
  winner: null,
  latest_vision: null,
  latest_decision: null,
  readiness: {
    odds: { status: "missing" },
    mapping: { status: "missing" },
    vision: { status: "missing" },
    model: { status: "missing" },
    strategy: { status: "missing" },
  },
});

const mapping: MappingRecord = {
  mapping_id: 101,
  map_number: 1,
  event_id: "event-a",
  raybet_team_ids: [1, 2],
  canonical_teams: [{ id: 11, name: "A" }, { id: 22, name: "B" }],
  acceptance_mode: "manual_exact",
  automatic_approval_id: null,
  accepted_by: "operator",
  accepted_at: "2026-07-15T00:00:00+00:00",
  recorded_at: "2026-07-15T00:00:00+00:00",
  evidence: {},
  evidence_hash: "a".repeat(64),
  invalidation: null,
  evidence_approval_id: null,
};

const snapshot: MonitorSnapshot = {
  generated_at: "2026-07-15T00:00:00+00:00",
  cursor: "cursor-1",
  mapping_revision: "mapping-1",
  health: [],
  matches: [match("a"), match("b")],
  alerts: [],
  summary: {
    total: 2,
    upcoming: 0,
    live: 0,
    degraded: 2,
    ended: 0,
    unhealthy_components: 0,
    active_alerts: 0,
    live_view: {
      total: 2,
      upcoming: 0,
      live: 0,
      degraded: 2,
      ended: 0,
    },
    history_view: {
      total: 0,
      upcoming: 0,
      live: 0,
      degraded: 0,
      ended: 0,
    },
  },
};

const stoppedComponent: ControlComponent = {
  component: "raybet_collector",
  label: "RayBet collector",
  status: "stopped",
  pid: null,
  started_at: null,
  detail: null,
  control_allowed: true,
};

const runningComponent: ControlComponent = {
  ...stoppedComponent,
  status: "running",
  pid: 1234,
};

const activeAlert: AlertIncident = {
  incident_id: 77,
  dedupe_key: "operational:test",
  episode: 1,
  category: "operational",
  severity: "warning",
  title: "Test alert",
  body: "still active",
  first_detected_at: "2026-07-15T00:00:00+00:00",
  opened_at: "2026-07-15T00:00:00+00:00",
  last_detected_at: "2026-07-15T00:00:00+00:00",
  acknowledged_at: null,
  acknowledged_by: null,
  source: {},
  occurrence_count: 1,
};

describe("App data recovery and ownership", () => {
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  beforeEach(() => {
    vi.resetAllMocks();
    localStorage.clear();
    window.history.replaceState(null, "", "/");
    api.fetchBootstrap.mockResolvedValue(snapshot);
    api.fetchMatchDetail.mockImplementation((matchId: string) => (
      Promise.resolve({ ...match(matchId), winner_timeline: [], decisions: [], vision: [], markets: [] })
    ));
    api.fetchMonitorHistory.mockResolvedValue({
      items: [],
      next_cursor: null,
      has_more: false,
    });
    api.fetchMappings.mockImplementation((matchId: string) => (
      matchId === "a" ? Promise.resolve([mapping]) : new Promise(() => undefined)
    ));
    api.createControlSession.mockResolvedValue({
      csrf_token: "csrf",
      expires_in: 3600,
      client_host: "127.0.0.1",
      components: [stoppedComponent],
    });
    api.fetchControlComponents.mockResolvedValue([runningComponent]);
    api.snapshotStream.mockReturnValue({
      addEventListener: vi.fn(),
      close: vi.fn(),
      onerror: null,
      onopen: null,
    });
  });

  it("never exposes mappings from the previously selected match", async () => {
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "系统运行" }));
    expect(await screen.findByText("mapping-101")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "滚球列表" }));
    fireEvent.click(screen.getByRole("button", { name: "select-b" }));
    fireEvent.click(screen.getByRole("button", { name: "系统运行" }));

    expect(await screen.findByTestId("selected-match")).toHaveTextContent("b");
    expect(screen.queryByText("mapping-101")).not.toBeInTheDocument();
  });

  it("separates live matches, historical evidence, and operations", async () => {
    render(<App />);

    expect(await screen.findByText("live-workspace")).toBeInTheDocument();
    expect(screen.queryByTestId("selected-match")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "历史比赛" }));
    expect(screen.getByRole("button", { name: "赔率复盘" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "OpenDota 赛后情报" })).toBeInTheDocument();
    expect(screen.getByText("odds-replay")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "OpenDota 赛后情报" }));
    expect(await screen.findByText("opendota-postmatch")).toBeInTheDocument();
    expect(screen.queryByText("odds-replay")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "系统运行" }));
    expect(await screen.findByTestId("selected-match")).toHaveTextContent("a");
    expect(screen.getByText("数据降级赛事")).toBeInTheDocument();
    expect(screen.getByText("活动告警")).toBeInTheDocument();
    expect(screen.queryByText("opendota-postmatch")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "select-a" })).not.toBeInTheDocument();
  });

  it("labels history counters as the loaded page rather than an all-time total", async () => {
    const loaded = Array.from({ length: 7 }, (_, index): MonitorMatch => ({
      ...match(`history-${index}`),
      lifecycle: index < 5 ? "ended" : "degraded",
      history_eligible: true,
    }));
    api.fetchMonitorHistory.mockResolvedValue({
      items: loaded,
      next_cursor: "history-cursor-1",
      has_more: true,
    });
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "历史比赛" }));
    expect(await screen.findByText("已加载历史")).toBeInTheDocument();
    expect(screen.getByText("7")).toBeInTheDocument();
    expect(screen.getByText("历史降级")).toBeInTheDocument();
    expect(screen.getByText("已完赛")).toBeInTheDocument();
    expect(await screen.findByText("还有更多")).toBeInTheDocument();
  });

  it("keeps legacy history deep links working", async () => {
    window.history.replaceState(null, "", "/?view=intelligence");

    render(<App />);

    expect(await screen.findByText("opendota-postmatch")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "OpenDota 赛后情报" })).toBeInTheDocument();
    expect(api.fetchBootstrap).not.toHaveBeenCalled();
    expect(api.snapshotStream).not.toHaveBeenCalled();
  });

  it("closes realtime transport in replay and reconnects when live is reopened", async () => {
    const close = vi.fn();
    api.snapshotStream.mockReturnValue({
      addEventListener: vi.fn(),
      close,
      onerror: null,
      onopen: null,
    });
    render(<App />);

    await waitFor(() => expect(api.snapshotStream).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "历史比赛" }));
    await waitFor(() => expect(close).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "滚球列表" }));
    await waitFor(() => expect(api.snapshotStream).toHaveBeenCalledTimes(2));
  });

  it("advances across an empty history page and merges later pages without duplicates", async () => {
    const first = { ...match("history-a"), lifecycle: "ended", history_eligible: true };
    const second = { ...match("history-b"), lifecycle: "ended", history_eligible: true };
    api.fetchMonitorHistory
      .mockResolvedValueOnce({
        items: [first],
        next_cursor: "history-cursor-1",
        has_more: true,
      })
      .mockResolvedValueOnce({
        items: [],
        next_cursor: "history-cursor-2",
        has_more: true,
      })
      .mockResolvedValueOnce({
        items: [first, second],
        next_cursor: null,
        has_more: false,
      });
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "历史比赛" }));

    expect(await screen.findByRole("button", { name: "select-history-a" })).toBeInTheDocument();
    const firstLoadMore = screen.getByRole("button", { name: "load-more-history" });
    await waitFor(() => expect(firstLoadMore).not.toBeDisabled());
    fireEvent.click(firstLoadMore);
    await waitFor(() => {
      expect(api.fetchMonitorHistory).toHaveBeenCalledWith(
        "history-cursor-1",
        expect.any(AbortSignal),
      );
    });
    const secondLoadMore = screen.getByRole("button", { name: "load-more-history" });
    await waitFor(() => expect(secondLoadMore).not.toBeDisabled());
    fireEvent.click(secondLoadMore);

    expect(await screen.findByRole("button", { name: "select-history-b" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "select-history-a" })).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "load-more-history" })).not.toBeInTheDocument();
  });

  it("keeps history load-more single-flight and aborts it when replay closes", async () => {
    const first = { ...match("history-a"), lifecycle: "ended", history_eligible: true };
    let resolveMore: ((value: {
      items: MonitorMatch[];
      next_cursor: string | null;
      has_more: boolean;
    }) => void) | undefined;
    api.fetchMonitorHistory
      .mockResolvedValueOnce({
        items: [first],
        next_cursor: "history-cursor-1",
        has_more: true,
      })
      .mockReturnValueOnce(new Promise((resolve) => {
        resolveMore = resolve;
      }));
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "历史比赛" }));

    expect(await screen.findByRole("button", { name: "select-history-a" })).toBeInTheDocument();
    const loadMore = screen.getByRole("button", { name: "load-more-history" });
    await waitFor(() => expect(loadMore).not.toBeDisabled());
    act(() => {
      loadMore.click();
      loadMore.click();
    });
    expect(api.fetchMonitorHistory).toHaveBeenCalledTimes(2);
    const signal = api.fetchMonitorHistory.mock.calls[1][1] as AbortSignal;

    fireEvent.click(screen.getByRole("button", { name: "OpenDota 赛后情报" }));
    expect(signal.aborted).toBe(true);
    await act(async () => {
      resolveMore?.({ items: [], next_cursor: null, has_more: false });
      await Promise.resolve();
    });
    expect(api.fetchMonitorHistory).toHaveBeenCalledTimes(2);
  });

  it("replaces stale replay eligibility when reentering history", async () => {
    const old = { ...match("history-old"), lifecycle: "degraded", history_eligible: true };
    const recent = { ...match("history-new"), lifecycle: "ended", history_eligible: true };
    api.fetchMonitorHistory
      .mockResolvedValueOnce({
        items: [old],
        next_cursor: "history-cursor-1",
        has_more: true,
      })
      .mockResolvedValueOnce({
        items: [recent],
        next_cursor: null,
        has_more: false,
      });
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "历史比赛" }));
    expect(await screen.findByRole("button", { name: "select-history-old" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "滚球列表" }));
    fireEvent.click(screen.getByRole("button", { name: "历史比赛" }));

    expect(await screen.findByRole("button", { name: "select-history-new" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "select-history-old" })).not.toBeInTheDocument();
    expect(api.fetchMonitorHistory).toHaveBeenNthCalledWith(
      2,
      null,
      expect.any(AbortSignal),
    );
  });

  it("aborts an in-flight fallback poll and ignores its result after SSE recovers", async () => {
    vi.useFakeTimers();
    let resolvePoll: ((value: MonitorSnapshot) => void) | undefined;
    api.fetchBootstrap
      .mockResolvedValueOnce(snapshot)
      .mockReturnValueOnce(new Promise((resolve) => {
        resolvePoll = resolve;
      }));
    const listeners: Array<((event: MessageEvent<string>) => void) | null> = [];
    const sources = Array.from({ length: 2 }, () => ({
      addEventListener: vi.fn((name: string, listener: (event: MessageEvent<string>) => void) => {
        if (name === "snapshot") listeners.push(listener);
      }),
      close: vi.fn(),
      onerror: null as ((event: Event) => void) | null,
      onopen: null as ((event: Event) => void) | null,
    }));
    api.snapshotStream
      .mockReturnValueOnce(sources[0])
      .mockReturnValueOnce(sources[1]);
    render(<App />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(api.snapshotStream).toHaveBeenCalledTimes(1);

    act(() => sources[0].onerror?.(new Event("error")));
    await act(async () => {
      vi.advanceTimersByTime(5_000);
      await Promise.resolve();
    });
    expect(api.fetchBootstrap).toHaveBeenCalledTimes(2);
    const pollSignal = api.fetchBootstrap.mock.calls[1][0] as AbortSignal;
    expect(pollSignal.aborted).toBe(false);

    await act(async () => {
      vi.advanceTimersByTime(5_000);
      await Promise.resolve();
    });
    expect(api.snapshotStream).toHaveBeenCalledTimes(2);
    act(() => sources[1].onopen?.(new Event("open")));
    expect(pollSignal.aborted).toBe(true);

    const sseSnapshot = {
      ...snapshot,
      cursor: "cursor-sse",
      matches: [match("sse-new")],
    };
    act(() => listeners[1]?.(new MessageEvent("snapshot", {
      data: JSON.stringify(sseSnapshot),
    })));
    expect(screen.getByRole("button", { name: "select-sse-new" })).toBeInTheDocument();

    await act(async () => {
      resolvePoll?.({
        ...snapshot,
        cursor: "cursor-stale-poll",
        matches: [match("stale-poll")],
      });
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByRole("button", { name: "select-sse-new" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "select-stale-poll" })).not.toBeInTheDocument();
    expect(api.fetchBootstrap).toHaveBeenCalledTimes(2);
  });

  it("stops hidden monitor detail, mapping, and control refreshes in OpenDota view", async () => {
    vi.useFakeTimers();
    render(<App />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    fireEvent.click(screen.getByRole("button", { name: "历史比赛" }));
    fireEvent.click(screen.getByRole("button", { name: "OpenDota 赛后情报" }));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    const before = {
      details: api.fetchMatchDetail.mock.calls.length,
      mappings: api.fetchMappings.mock.calls.length,
      controls: api.fetchControlComponents.mock.calls.length,
    };
    await act(async () => {
      vi.advanceTimersByTime(15_000);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(api.fetchMatchDetail).toHaveBeenCalledTimes(before.details);
    expect(api.fetchMappings).toHaveBeenCalledTimes(before.mappings);
    expect(api.fetchControlComponents).toHaveBeenCalledTimes(before.controls);
    expect(api.createControlSession).not.toHaveBeenCalled();
  });

  it("does not refetch an immutable replay detail when live snapshots advance", async () => {
    const liveMatch = match("live");
    const replayMatch: MonitorMatch = {
      ...match("replay"),
      lifecycle: "ended",
      history_eligible: true,
    };
    const replaySnapshot = {
      ...snapshot,
      matches: [liveMatch],
    };
    let snapshotListener: ((event: MessageEvent<string>) => void) | null = null;
    const close = vi.fn();
    api.fetchBootstrap.mockResolvedValue(replaySnapshot);
    api.fetchMonitorHistory.mockResolvedValue({
      items: [replayMatch],
      next_cursor: null,
      has_more: false,
    });
    api.snapshotStream.mockReturnValue({
      addEventListener: vi.fn((name: string, listener: (event: MessageEvent<string>) => void) => {
        if (name === "snapshot") snapshotListener = listener;
      }),
      close,
      onerror: null,
      onopen: null,
    });

    render(<App />);
    expect(await screen.findByText("live-workspace")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "历史比赛" }));
    expect(await screen.findByText("odds-replay")).toBeInTheDocument();
    expect(close).toHaveBeenCalledTimes(1);
    await waitFor(() => {
      expect(api.fetchMatchDetail).toHaveBeenCalledWith("replay", expect.any(AbortSignal));
    });
    const callsBeforeSnapshot = api.fetchMatchDetail.mock.calls.length;

    await act(async () => {
      snapshotListener?.(new MessageEvent("snapshot", {
        data: JSON.stringify({ ...replaySnapshot, cursor: "cursor-2" }),
      }));
      await Promise.resolve();
    });

    expect(api.fetchMatchDetail).toHaveBeenCalledTimes(callsBeforeSnapshot);
  });

  it("refreshes live detail at a bounded cadence without overlapping requests", async () => {
    vi.useFakeTimers();
    let resolveRefresh: ((value: unknown) => void) | undefined;
    api.fetchMatchDetail
      .mockResolvedValueOnce({
        ...match("a"),
        winner_timeline: [],
        decisions: [],
        vision: [],
        markets: [],
      })
      .mockReturnValueOnce(new Promise((resolve) => {
        resolveRefresh = resolve;
      }));

    render(<App />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(api.fetchMatchDetail).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(5_000);
      await Promise.resolve();
    });
    expect(api.fetchMatchDetail).toHaveBeenCalledTimes(2);

    await act(async () => {
      vi.advanceTimersByTime(20_000);
      await Promise.resolve();
    });
    expect(api.fetchMatchDetail).toHaveBeenCalledTimes(2);

    await act(async () => {
      resolveRefresh?.({
        ...match("a"),
        winner_timeline: [],
        decisions: [],
        vision: [],
        markets: [],
      });
      await Promise.resolve();
      await Promise.resolve();
    });
    await act(async () => {
      vi.advanceTimersByTime(4_999);
      await Promise.resolve();
    });
    expect(api.fetchMatchDetail).toHaveBeenCalledTimes(2);

    await act(async () => {
      vi.advanceTimersByTime(1);
      await Promise.resolve();
    });
    expect(api.fetchMatchDetail).toHaveBeenCalledTimes(3);
  });

  it("keeps live and ended match lists isolated", async () => {
    const endedMatch: MonitorMatch = {
      ...match("ended"),
      lifecycle: "ended",
      history_eligible: true,
    };
    api.fetchBootstrap.mockResolvedValue({
      ...snapshot,
      matches: [match("live")],
    });
    api.fetchMonitorHistory.mockResolvedValue({
      items: [endedMatch],
      next_cursor: null,
      has_more: false,
    });

    render(<App />);

    expect(await screen.findByRole("button", { name: "select-live" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "select-ended" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "历史比赛" }));
    expect(await screen.findByRole("button", { name: "select-ended" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "select-live" })).not.toBeInTheDocument();
  });

  it("fails closed when an ended match is missing the archive eligibility flag", async () => {
    const malformed = { ...match("missing-eligibility"), lifecycle: "ended" } as MonitorMatch;
    delete (malformed as Partial<MonitorMatch>).history_eligible;
    api.fetchMonitorHistory.mockResolvedValue({
      items: [malformed],
      next_cursor: null,
      has_more: false,
    });

    render(<App />);

    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "select-missing-eligibility" }))
        .not.toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: "历史比赛" }));
    expect(screen.queryByRole("button", { name: "select-missing-eligibility" })).not.toBeInTheDocument();
  });

  it("moves long-stale replay evidence out of the live list without relabeling it ended", async () => {
    const archivedMatch: MonitorMatch = {
      ...match("archived"),
      history_eligible: true,
      lifecycle: "degraded",
    };
    api.fetchBootstrap.mockResolvedValue({
      ...snapshot,
      matches: [match("live")],
    });
    api.fetchMonitorHistory.mockResolvedValue({
      items: [archivedMatch],
      next_cursor: null,
      has_more: false,
    });

    render(<App />);

    expect(await screen.findByRole("button", { name: "select-live" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "select-archived" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "历史比赛" }));
    expect(await screen.findByRole("button", { name: "select-archived" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "select-live" })).not.toBeInTheDocument();
  });

  it("retries the initial bootstrap after a transient failure", async () => {
    vi.useFakeTimers();
    try {
      api.fetchBootstrap
        .mockRejectedValueOnce(new Error("temporary outage"))
        .mockResolvedValue(snapshot);
      render(<App />);

      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(screen.getByRole("alert")).toHaveTextContent("temporary outage");

      await act(async () => {
        vi.advanceTimersByTime(5_000);
        await Promise.resolve();
        await Promise.resolve();
      });

      expect(api.fetchBootstrap).toHaveBeenCalledTimes(2);
      fireEvent.click(screen.getByRole("button", { name: "系统运行" }));
      expect(screen.getByTestId("selected-match")).toHaveTextContent("a");
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("refreshes managed component status every five seconds", async () => {
    vi.useFakeTimers();
    render(<App />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    fireEvent.click(screen.getByRole("button", { name: "系统运行" }));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText("component-raybet_collector-stopped")).toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(5_000);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(api.fetchControlComponents).toHaveBeenCalledWith("csrf", expect.any(AbortSignal));
    expect(screen.getByText("component-raybet_collector-running")).toBeInTheDocument();
  });

  it("renews the control session before its advertised expiry", async () => {
    vi.useFakeTimers();
    api.createControlSession.mockResolvedValue({
      csrf_token: "short-lived",
      expires_in: 61,
      client_host: "127.0.0.1",
      components: [stoppedComponent],
    });
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "系统运行" }));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    await act(async () => {
      vi.advanceTimersByTime(5_000);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(api.createControlSession).toHaveBeenCalledTimes(2);
  });

  it("does not mark an alert acknowledged when the API reports no change", async () => {
    api.fetchBootstrap.mockResolvedValue({ ...snapshot, alerts: [activeAlert] });
    api.acknowledgeAlert.mockResolvedValue({ acknowledged: false });
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "系统运行" }));
    expect(await screen.findByText("incident-77-unacknowledged")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "ack-first-alert" }));

    expect(await screen.findByTestId("control-message")).toHaveTextContent(
      "告警状态已变化",
    );
    expect(screen.getByText("incident-77-unacknowledged")).toBeInTheDocument();
    expect(screen.queryByText("incident-77-acknowledged")).not.toBeInTheDocument();
  });
});
