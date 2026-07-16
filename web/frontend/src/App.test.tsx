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
  MatchRail: ({ matches, onSelect }: {
    matches: MonitorMatch[];
    onSelect: (matchId: string) => void;
  }) => (
    <nav>
      {matches.map((match) => (
        <button key={match.raybet_match_id} onClick={() => onSelect(match.raybet_match_id)}>
          select-{match.raybet_match_id}
        </button>
      ))}
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
    vi.clearAllMocks();
    localStorage.clear();
    window.history.replaceState(null, "", "/");
    api.fetchBootstrap.mockResolvedValue(snapshot);
    api.fetchMatchDetail.mockImplementation((matchId: string) => (
      Promise.resolve({ ...match(matchId), winner_timeline: [], decisions: [], vision: [], markets: [] })
    ));
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
    expect(await screen.findByTestId("selected-match")).toHaveTextContent("none");
    expect(screen.queryByText("opendota-postmatch")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "select-a" })).not.toBeInTheDocument();
  });

  it("uses view-specific summary counters on the history tab", async () => {
    api.fetchBootstrap.mockResolvedValue({
      ...snapshot,
      summary: {
        ...snapshot.summary,
        live_view: { total: 4, upcoming: 2, live: 1, degraded: 1, ended: 0 },
        history_view: { total: 7, upcoming: 0, live: 0, degraded: 2, ended: 5 },
      },
    });
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "历史比赛" }));
    expect(await screen.findByText("历史比赛")).toBeInTheDocument();
    expect(screen.getByText("7")).toBeInTheDocument();
    expect(screen.getByText("历史降级")).toBeInTheDocument();
    expect(screen.getByText("已完赛")).toBeInTheDocument();
  });

  it("keeps legacy history deep links working", async () => {
    window.history.replaceState(null, "", "/?view=intelligence");

    render(<App />);

    expect(await screen.findByText("opendota-postmatch")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "OpenDota 赛后情报" })).toBeInTheDocument();
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

  it("keeps live and ended match lists isolated", async () => {
    const endedMatch: MonitorMatch = {
      ...match("ended"),
      lifecycle: "ended",
      history_eligible: true,
    };
    api.fetchBootstrap.mockResolvedValue({
      ...snapshot,
      matches: [match("live"), endedMatch],
    });

    render(<App />);

    expect(await screen.findByRole("button", { name: "select-live" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "select-ended" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "历史比赛" }));
    expect(screen.getByRole("button", { name: "select-ended" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "select-live" })).not.toBeInTheDocument();
  });

  it("fails closed when an ended match is missing the archive eligibility flag", async () => {
    const malformed = { ...match("missing-eligibility"), lifecycle: "ended" } as MonitorMatch;
    delete (malformed as Partial<MonitorMatch>).history_eligible;
    api.fetchBootstrap.mockResolvedValue({
      ...snapshot,
      matches: [malformed],
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
      matches: [match("live"), archivedMatch],
    });

    render(<App />);

    expect(await screen.findByRole("button", { name: "select-live" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "select-archived" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "历史比赛" }));
    expect(screen.getByRole("button", { name: "select-archived" })).toBeInTheDocument();
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
