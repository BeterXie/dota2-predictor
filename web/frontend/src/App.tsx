import { Switch, Tab, TabList } from "@fluentui/react-components";
import {
  Bell,
  BellRinging,
  Broadcast,
  ClockCounterClockwise,
  Database,
  GearSix,
  Pulse,
  SpeakerHigh,
} from "@phosphor-icons/react";
import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";

import {
  acknowledgeAlert,
  approveAutomaticMapping,
  controlComponent,
  createAutomaticMapping,
  createControlSession,
  fetchBootstrap,
  fetchControlComponents,
  fetchMappings,
  fetchMatchDetail,
  fetchMonitorHistory,
  invalidateMapping,
  snapshotStream,
} from "./api";
import { MatchRail } from "./components/MatchRail";
import { MatchWorkspace } from "./components/MatchWorkspace";
import { OperationsPanel } from "./components/OperationsPanel";
import type {
  ConnectionState,
  ControlComponent,
  ControlResult,
  ControlSession,
  MappingRecord,
  MatchDetail,
  MonitorMatch,
  MonitorLifecycleCounts,
  MonitorSnapshot,
} from "./types";

type HistoryView = "replay" | "intelligence";
type PrimaryView = "live" | "history" | "operations";
type ViewMode = "live" | HistoryView | "operations";

const LIVE_DETAIL_REFRESH_MS = 5_000;

const IntelligenceDashboard = lazy(() =>
  import("./components/IntelligenceDashboard").then((module) => ({
    default: module.IntelligenceDashboard,
  })),
);

export default function App() {
  const [snapshot, setSnapshot] = useState<MonitorSnapshot | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<MatchDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [view, setView] = useState<ViewMode>(initialView);
  const [historyMatches, setHistoryMatches] = useState<MonitorMatch[]>([]);
  const [historyCursor, setHistoryCursor] = useState<string | null>(null);
  const [historyHasMore, setHistoryHasMore] = useState(false);
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const historyViewRef = useRef<HistoryView>(
    view === "intelligence" ? "intelligence" : "replay",
  );
  const [now, setNow] = useState(Date.now());
  const [controlSession, setControlSession] = useState<ControlSession | null>(null);
  const [components, setComponents] = useState<ControlComponent[]>([]);
  const [controlBusy, setControlBusy] = useState<string | null>(null);
  const [controlMessage, setControlMessage] = useState<string | null>(null);
  const [mappingState, setMappingState] = useState<{
    matchId: string | null;
    version: number;
    records: MappingRecord[];
  }>({ matchId: null, version: 0, records: [] });
  const [mappingVersion, setMappingVersion] = useState(0);
  const [soundEnabled, setSoundEnabled] = useState(
    () => localStorage.getItem("dota2-monitor-sound") === "on",
  );
  const [browserAlerts, setBrowserAlerts] = useState(
    () => localStorage.getItem("dota2-monitor-browser-alerts") === "on",
  );
  const cursorRef = useRef<string | undefined>(undefined);
  const historyPageRequestRef = useRef<AbortController | null>(null);
  const notifiedAlerts = useRef(new Set<number>());
  const realtimeEnabled = view === "live" || view === "operations";

  useEffect(() => {
    if (!realtimeEnabled || snapshot) return;
    let controller: AbortController | null = null;
    let retryTimer: number | null = null;
    let disposed = false;
    const load = () => {
      controller = new AbortController();
      fetchBootstrap(controller.signal)
        .then((data) => {
          if (disposed) return;
          cursorRef.current = data.cursor;
          setSnapshot(data);
          setSelectedId((current) => (
            current && data.matches.some((match) => match.raybet_match_id === current)
              ? current
              : preferredMatch(data.matches, "live")
          ));
          setError(null);
        })
        .catch((reason: Error) => {
          if (disposed || reason.name === "AbortError") return;
          setError(reason.message || "无法加载监控数据");
          setConnection("offline");
          retryTimer = window.setTimeout(load, 5_000);
        });
    };
    load();
    return () => {
      disposed = true;
      controller?.abort();
      if (retryTimer != null) window.clearTimeout(retryTimer);
    };
  }, [realtimeEnabled]);

  useEffect(() => {
    if (view !== "replay") return;
    const controller = new AbortController();
    let disposed = false;
    setHistoryLoading(true);
    fetchMonitorHistory(null, controller.signal)
      .then((page) => {
        if (disposed) return;
        if (page.has_more && !page.next_cursor) {
          throw new Error("历史分页响应缺少游标");
        }
        setHistoryMatches(dedupeMatches(page.items));
        setHistoryCursor(page.next_cursor);
        setHistoryHasMore(page.has_more);
        setHistoryLoaded(true);
        setHistoryLoading(false);
        setHistoryError(null);
        setSelectedId((current) => (
          current && page.items.some((match) => match.raybet_match_id === current)
              ? current
              : page.items[0]?.raybet_match_id || null
        ));
      })
      .catch((reason: Error) => {
        if (!disposed && reason.name !== "AbortError") {
          setHistoryLoading(false);
          setHistoryError(reason.message || "无法加载历史比赛");
        }
      });
    return () => {
      disposed = true;
      controller.abort();
    };
  }, [view]);

  useEffect(() => {
    if (view === "replay") return;
    historyPageRequestRef.current?.abort();
    historyPageRequestRef.current = null;
    setHistoryLoading(false);
  }, [view]);

  useEffect(() => () => {
    historyPageRequestRef.current?.abort();
    historyPageRequestRef.current = null;
  }, []);

  useEffect(() => {
    if (view !== "operations") {
      setControlSession(null);
      setComponents([]);
      return;
    }
    let controller: AbortController | null = null;
    let renewalTimer: number | null = null;
    let disposed = false;
    const establish = () => {
      controller = new AbortController();
      createControlSession(controller.signal)
        .then((session) => {
          if (disposed) return;
          setControlSession(session);
          setComponents(session.components);
          const renewAfter = Math.max(5_000, (session.expires_in - 60) * 1_000);
          renewalTimer = window.setTimeout(establish, renewAfter);
        })
        .catch((reason: Error) => {
          if (disposed || reason.name === "AbortError") return;
          setControlMessage("进程控制不可用，仅保留监控功能");
          renewalTimer = window.setTimeout(establish, 5_000);
        });
    };
    establish();
    return () => {
      disposed = true;
      controller?.abort();
      if (renewalTimer != null) window.clearTimeout(renewalTimer);
    };
  }, [view]);

  useEffect(() => {
    if (!controlSession) return;
    if (view !== "operations") return;
    const controller = new AbortController();
    let disposed = false;
    let refreshing = false;
    const refresh = async () => {
      if (refreshing) return;
      refreshing = true;
      try {
        const next = await fetchControlComponents(
          controlSession.csrf_token,
          controller.signal,
        );
        if (!disposed) setComponents(next);
      } catch (reason) {
        if (!disposed && (!(reason instanceof Error) || reason.name !== "AbortError")) {
          setComponents([]);
          try {
            const session = await createControlSession(controller.signal);
            if (!disposed) {
              setControlSession(session);
              setComponents(session.components);
            }
          } catch (renewalReason) {
            if (
              !disposed
              && (!(renewalReason instanceof Error) || renewalReason.name !== "AbortError")
            ) {
              setControlMessage("进程控制会话已失效，正在重试");
            }
          }
        }
      } finally {
        refreshing = false;
      }
    };
    const timer = window.setInterval(() => void refresh(), 5_000);
    return () => {
      disposed = true;
      controller.abort();
      window.clearInterval(timer);
    };
  }, [controlSession, view]);

  useEffect(() => {
    if (!snapshot || !realtimeEnabled) return;
    let source: EventSource | null = null;
    let pollTimer: number | null = null;
    let reconnectTimer: number | null = null;
    let pollController: AbortController | null = null;
    let pollGeneration = 0;
    let disposed = false;

    const cancelPoll = () => {
      pollGeneration += 1;
      pollController?.abort();
      pollController = null;
      if (pollTimer != null) {
        window.clearInterval(pollTimer);
        pollTimer = null;
      }
    };

    const refreshFromPoll = async () => {
      if (disposed || pollController !== null) return;
      const generation = pollGeneration;
      const controller = new AbortController();
      pollController = controller;
      try {
        const data = await fetchBootstrap(controller.signal);
        if (
          disposed
          || controller.signal.aborted
          || generation !== pollGeneration
          || pollController !== controller
        ) return;
        cursorRef.current = data.cursor;
        setSnapshot(data);
        setError(null);
      } catch (reason) {
        if (
          !disposed
          && !controller.signal.aborted
          && generation === pollGeneration
          && pollController === controller
        ) {
          setError(errorText(reason, "轮询失败"));
        }
      } finally {
        if (pollController === controller) pollController = null;
      }
    };

    const poll = () => {
      if (pollTimer != null) return;
      setConnection("fallback");
      pollTimer = window.setInterval(() => void refreshFromPoll(), 5_000);
    };

    const connect = () => {
      if (disposed) return;
      const connectedSource = snapshotStream(cursorRef.current);
      source = connectedSource;
      setConnection("connecting");
      connectedSource.onopen = () => {
        if (disposed || source !== connectedSource) return;
        cancelPoll();
        setConnection("live");
      };
      connectedSource.addEventListener("snapshot", (event) => {
        if (disposed || source !== connectedSource) return;
        cancelPoll();
        const data = JSON.parse((event as MessageEvent<string>).data) as MonitorSnapshot;
        cursorRef.current = data.cursor;
        setSnapshot(data);
        setError(null);
        setConnection("live");
      });
      connectedSource.onerror = () => {
        if (disposed || source !== connectedSource) return;
        connectedSource.close();
        source = null;
        poll();
        if (reconnectTimer == null) {
          reconnectTimer = window.setTimeout(() => {
            reconnectTimer = null;
            connect();
          }, 10_000);
        }
      };
    };

    connect();
    return () => {
      disposed = true;
      source?.close();
      cancelPoll();
      if (reconnectTimer != null) window.clearTimeout(reconnectTimer);
    };
  }, [Boolean(snapshot), realtimeEnabled]);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!selectedId || (view !== "live" && view !== "replay")) {
      setDetail(null);
      setDetailLoading(false);
      return;
    }
    let disposed = false;
    let controller: AbortController | null = null;
    let refreshTimer: number | null = null;

    const load = async () => {
      if (disposed || controller) return;
      const request = new AbortController();
      controller = request;
      setDetailLoading(true);
      try {
        const data = await fetchMatchDetail(selectedId, request.signal);
        if (disposed || request.signal.aborted) return;
        setDetail(data);
        setDetailError(null);
      } catch (reason) {
        if (
          !disposed
          && (!(reason instanceof Error) || reason.name !== "AbortError")
        ) {
          setDetailError(errorText(reason, "无法加载赛事详情"));
        }
      } finally {
        if (controller === request) controller = null;
        if (!disposed) {
          setDetailLoading(false);
          if (view === "live") {
            refreshTimer = window.setTimeout(() => void load(), LIVE_DETAIL_REFRESH_MS);
          }
        }
      }
    };

    void load();
    return () => {
      disposed = true;
      controller?.abort();
      if (refreshTimer != null) window.clearTimeout(refreshTimer);
    };
  }, [selectedId, view]);

  useEffect(() => {
    if (view === "live" && snapshot) {
      const visible = matchesForView(snapshot.matches, "live");
      setSelectedId((current) => (
        current && visible.some((match) => match.raybet_match_id === current)
          ? current
          : preferredMatch(snapshot.matches, "live")
      ));
      return;
    }
    if (view === "replay" && historyLoaded) {
      setSelectedId((current) => (
        current && historyMatches.some((match) => match.raybet_match_id === current)
          ? current
          : historyMatches[0]?.raybet_match_id || null
      ));
    }
  }, [historyLoaded, historyMatches, snapshot, view]);

  useEffect(() => {
    if (!selectedId || view !== "operations") {
      setMappingState({ matchId: null, version: mappingVersion, records: [] });
      return;
    }
    let active = true;
    const controller = new AbortController();
    fetchMappings(selectedId, controller.signal)
      .then((records) => {
        if (active) {
          setMappingState({ matchId: selectedId, version: mappingVersion, records });
        }
      })
      .catch((reason: Error) => {
        if (active && reason.name !== "AbortError") {
          setMappingState({ matchId: selectedId, version: mappingVersion, records: [] });
          setControlMessage("无法读取映射证据");
        }
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [selectedId, mappingVersion, snapshot?.mapping_revision, view]);

  useEffect(() => {
    const active = snapshot?.alerts || [];
    for (const alert of active) {
      if (alert.acknowledged_at || notifiedAlerts.current.has(alert.incident_id)) continue;
      notifiedAlerts.current.add(alert.incident_id);
      if (browserAlerts && "Notification" in window && Notification.permission === "granted") {
        new Notification(alert.title, { body: alert.body, tag: alert.dedupe_key });
      }
      if (soundEnabled) playAlertTone(alert.severity === "critical");
    }
  }, [browserAlerts, snapshot?.alerts, soundEnabled]);

  const loadMoreHistory = async () => {
    if (
      historyPageRequestRef.current
      || historyLoading
      || (historyLoaded && !historyHasMore)
    ) return;
    const requestedCursor = historyLoaded ? historyCursor : null;
    if (historyLoaded && !requestedCursor) {
      setHistoryHasMore(false);
      setHistoryError("历史分页游标已失效");
      return;
    }
    const controller = new AbortController();
    historyPageRequestRef.current = controller;
    setHistoryLoading(true);
    try {
      const page = await fetchMonitorHistory(requestedCursor, controller.signal);
      if (
        controller.signal.aborted
        || historyPageRequestRef.current !== controller
      ) return;
      if (
        page.has_more
        && (!page.next_cursor || page.next_cursor === requestedCursor)
      ) {
        setHistoryHasMore(false);
        throw new Error("历史分页游标没有向前推进");
      }
      setHistoryMatches((current) => dedupeMatches([...current, ...page.items]));
      setHistoryCursor(page.next_cursor);
      setHistoryHasMore(page.has_more);
      setHistoryLoaded(true);
      setHistoryError(null);
    } catch (reason) {
      if (
        historyPageRequestRef.current === controller
        && (!(reason instanceof Error) || reason.name !== "AbortError")
      ) {
        setHistoryError(errorText(reason, "无法加载更多历史比赛"));
      }
    } finally {
      if (historyPageRequestRef.current === controller) {
        historyPageRequestRef.current = null;
        setHistoryLoading(false);
      }
    }
  };

  const liveMatches = snapshot?.matches || [];
  const matches = view === "replay" ? historyMatches : liveMatches;
  const railMatches = view === "replay" || view === "live"
    ? matchesForView(matches, view)
    : [];
  const selectedMatch = matches.find((match) => match.raybet_match_id === selectedId) || null;
  const mappings = mappingState.matchId === selectedId && mappingState.version === mappingVersion
    ? mappingState.records
    : [];
  const alertCount = useMemo(
    () => (snapshot?.alerts || []).filter((item) => !item.acknowledged_at).length,
    [snapshot?.alerts],
  );
  const viewSummary = snapshot ? summaryForView(snapshot.summary, view) : null;
  const historySummary = lifecycleCounts(historyMatches);
  const displayedError = view === "replay" ? historyError : error;

  const changeView = (next: ViewMode) => {
    if (next === "replay" || next === "intelligence") {
      historyViewRef.current = next;
    }
    setView(next);
    const query = new URLSearchParams(window.location.search);
    if (next === "live") query.delete("view");
    else query.set("view", next);
    const search = query.toString();
    window.history.replaceState(
      null,
      "",
      `${window.location.pathname}${search ? `?${search}` : ""}${window.location.hash}`,
    );
    if (next === "live" && snapshot) {
      setSelectedId(preferredMatch(snapshot.matches, "live"));
    } else if (
      next === "operations"
      && snapshot
      && !snapshot.matches.some((match) => match.raybet_match_id === selectedId)
    ) {
      setSelectedId(preferredMatch(snapshot.matches, "live"));
    } else if (next === "replay") {
      setSelectedId(historyMatches[0]?.raybet_match_id || null);
    }
  };

  const changePrimaryView = (next: PrimaryView) => {
    changeView(next === "history" ? historyViewRef.current : next);
  };

  const primaryView: PrimaryView = view === "live"
    ? "live"
    : view === "operations"
      ? "operations"
      : "history";

  const runControl = async (
    component: ControlComponent["component"],
    action: ControlResult["action"],
  ) => {
    if (!controlSession) return;
    const componentLabel = components.find((item) => item.component === component)?.label || component;
    const actionLabel = { start: "启动", stop: "停止", restart: "重启" }[action];
    if (!window.confirm(`确认${actionLabel} ${componentLabel}？`)) return;
    const busyKey = `${component}:${action}`;
    setControlBusy(busyKey);
    setControlMessage(null);
    try {
      const result = await controlComponent(component, action, controlSession.csrf_token);
      setControlMessage(`${componentLabel}: ${result.result}`);
      setComponents(await fetchControlComponents(controlSession.csrf_token));
    } catch (reason) {
      setControlMessage(errorText(reason, "进程操作失败"));
    } finally {
      setControlBusy(null);
    }
  };

  const approveMapping = async (mappingId: number) => {
    if (!controlSession || !window.confirm("确认该 exact 证据已人工核验，可用于同系列后续局？")) return;
    setControlBusy(`mapping:${mappingId}:approve`);
    try {
      await approveAutomaticMapping(mappingId, controlSession.csrf_token);
      setControlMessage("自动 exact 证据已批准");
      setMappingVersion((value) => value + 1);
    } catch (reason) {
      setControlMessage(errorText(reason, "证据批准失败"));
    } finally {
      setControlBusy(null);
    }
  };

  const invalidateSelectedMapping = async (mappingId: number) => {
    if (!controlSession) return;
    const reason = window.prompt("请输入失效原因（至少 5 个字符）");
    if (!reason || reason.trim().length < 5) return;
    if (!window.confirm("确认追加失效记录？旧映射和依赖结果会保留并标记受影响。")) return;
    setControlBusy(`mapping:${mappingId}:invalidate`);
    try {
      await invalidateMapping(mappingId, reason.trim(), controlSession.csrf_token);
      setControlMessage("映射已失效，依赖结果已标记");
      setMappingVersion((value) => value + 1);
    } catch (error) {
      setControlMessage(errorText(error, "映射失效失败"));
    } finally {
      setControlBusy(null);
    }
  };

  const addAutomaticMap = async (sourceMappingId: number, mapNumber: number) => {
    if (!controlSession || !window.confirm(`确认使用已批准证据登记第 ${mapNumber} 局 automatic_exact？`)) return;
    setControlBusy(`mapping:${sourceMappingId}:map:${mapNumber}`);
    try {
      await createAutomaticMapping(sourceMappingId, mapNumber, controlSession.csrf_token);
      setControlMessage(`第 ${mapNumber} 局映射已登记`);
      setMappingVersion((value) => value + 1);
    } catch (error) {
      setControlMessage(errorText(error, "自动映射失败"));
    } finally {
      setControlBusy(null);
    }
  };

  const acknowledge = async (incidentId: number) => {
    if (!controlSession) return;
    try {
      const result = await acknowledgeAlert(incidentId, controlSession.csrf_token);
      if (!result.acknowledged) {
        setControlMessage("告警状态已变化，等待下一次快照同步");
        return;
      }
      setSnapshot((current) => current ? {
        ...current,
        alerts: current.alerts.map((item) => item.incident_id === incidentId
          ? { ...item, acknowledged_at: new Date().toISOString(), acknowledged_by: "local-operator" }
          : item),
      } : current);
    } catch (reason) {
      setControlMessage(errorText(reason, "告警确认失败"));
    }
  };

  const toggleBrowserAlerts = async (checked: boolean) => {
    if (!checked) {
      setBrowserAlerts(false);
      localStorage.setItem("dota2-monitor-browser-alerts", "off");
      return;
    }
    if (!("Notification" in window)) {
      setControlMessage("当前浏览器不支持系统通知");
      return;
    }
    const permission = await Notification.requestPermission();
    const enabled = permission === "granted";
    setBrowserAlerts(enabled);
    localStorage.setItem("dota2-monitor-browser-alerts", enabled ? "on" : "off");
    if (!enabled) setControlMessage("浏览器通知权限未授予");
  };

  const toggleSound = (checked: boolean) => {
    setSoundEnabled(checked);
    localStorage.setItem("dota2-monitor-sound", checked ? "on" : "off");
    if (checked) playAlertTone(false);
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-lockup">
          <div className="brand-mark"><Pulse size={21} weight="bold" aria-hidden="true" /></div>
          <div>
            <strong>Dota 2 滚球监控台</strong>
            <span>本机只读证据与纸面策略</span>
          </div>
        </div>

        <TabList
          aria-label="主视图"
          selectedValue={primaryView}
          onTabSelect={(_, data) => changePrimaryView(data.value as PrimaryView)}
          size="small"
        >
          <Tab icon={<Broadcast size={17} />} value="live">滚球列表</Tab>
          <Tab icon={<ClockCounterClockwise size={17} />} value="history">历史比赛</Tab>
          <Tab icon={<GearSix size={17} />} value="operations">系统运行</Tab>
        </TabList>

        <div className="topbar-status">
          {realtimeEnabled && <ConnectionBadge state={connection} />}
          <span className="compact-switch" title="声音告警">
            <SpeakerHigh size={16} aria-hidden="true" />
            <Switch
              aria-label="声音告警"
              checked={soundEnabled}
              label="声音"
              onChange={(_, data) => toggleSound(data.checked)}
            />
          </span>
          <span className="compact-switch" title="浏览器系统通知">
            <BellRinging size={16} aria-hidden="true" />
            <Switch
              aria-label="浏览器系统通知"
              checked={browserAlerts}
              label="系统通知"
              onChange={(_, data) => void toggleBrowserAlerts(data.checked)}
            />
          </span>
          <span className="alert-count" title="未确认告警">
            <Bell size={17} aria-hidden="true" />
            {alertCount}
          </span>
        </div>
      </header>

      {(view === "replay" || view === "intelligence") && (
        <div className="history-mode-bar">
          <TabList
            aria-label="历史比赛内容"
            selectedValue={view}
            onTabSelect={(_, data) => changeView(data.value as HistoryView)}
            size="small"
          >
            <Tab icon={<ClockCounterClockwise size={17} />} value="replay">赔率复盘</Tab>
            <Tab icon={<Database size={17} />} value="intelligence">OpenDota 赛后情报</Tab>
          </TabList>
        </div>
      )}

      {view === "replay" ? (
        <section className="summary-bar" aria-label="历史赔率加载摘要">
          <SummaryItem
            label="已加载历史"
            value={historyMatches.length}
          />
          <SummaryItem
            label="历史降级"
            value={historySummary.degraded}
            tone="warning"
          />
          <SummaryItem label="已完赛" value={historySummary.ended} />
          <span className="snapshot-time">
            {historyLoading
              ? "正在加载"
              : historyHasMore
                ? "还有更多"
                : historyLoaded ? "已全部加载" : "尚未加载"}
          </span>
        </section>
      ) : view !== "intelligence" && snapshot && (
        <section className="summary-bar" aria-label="赛事与系统摘要">
          <SummaryItem
            label="滚球确认"
            value={viewSummary?.live || 0}
            tone="live"
          />
          <SummaryItem
            label="数据降级"
            value={viewSummary?.degraded || 0}
            tone="warning"
          />
          <SummaryItem
            label="即将开始"
            value={viewSummary?.upcoming || 0}
          />
          <SummaryItem label="异常进程" value={snapshot.summary.unhealthy_components} tone="critical" />
          <span className="snapshot-time">快照 {new Date(snapshot.generated_at).toLocaleTimeString("zh-CN", { hour12: false })}</span>
        </section>
      )}

      {view !== "intelligence" && displayedError && (
        <div className="global-error" role="alert">
          <strong>{view === "replay" ? "历史赔率加载异常" : "监控连接异常"}</strong>
          <span>{displayedError}</span>
        </div>
      )}

      {view === "intelligence" ? (
        <Suspense fallback={<div className="view-loading" role="status">正在加载赛后情报</div>}>
          <IntelligenceDashboard />
        </Suspense>
      ) : view === "operations" ? (
        <div className="operations-view">
          <OperationsPanel
            alerts={snapshot?.alerts || []}
            busyKey={controlBusy}
            components={components}
            controlMessage={controlMessage}
            controlsEnabled={Boolean(controlSession)}
            health={snapshot?.health || []}
            mappings={mappings}
            match={selectedMatch}
            onAcknowledge={acknowledge}
            onApproveMapping={approveMapping}
            onControl={runControl}
            onCreateAutomaticMap={addAutomaticMap}
            onInvalidateMapping={invalidateSelectedMapping}
          />
        </div>
      ) : (
        <div className={`cockpit-grid ${view === "replay" ? "history-replay" : ""}`}>
          <MatchRail
            historyHasMore={historyHasMore || (!historyLoaded && Boolean(historyError))}
            historyLoading={historyLoading}
            matches={railMatches}
            mode={view === "replay" ? "history" : "live"}
            now={now}
            onLoadMore={() => void loadMoreHistory()}
            onSelect={setSelectedId}
            selectedId={selectedId}
          />
          <MatchWorkspace
            detail={detail?.raybet_match_id === selectedId ? detail : null}
            error={detailError}
            loading={detailLoading}
            match={selectedMatch}
            now={now}
            replay={view === "replay"}
          />
        </div>
      )}
    </div>
  );
}

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

function initialView(): ViewMode {
  const requested = new URLSearchParams(window.location.search).get("view");
  return requested === "replay"
    || requested === "intelligence"
    || requested === "operations"
    ? requested
    : "live";
}

function playAlertTone(critical: boolean): void {
  const AudioContextClass = window.AudioContext
    || (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!AudioContextClass) return;
  const context = new AudioContextClass();
  const oscillator = context.createOscillator();
  const gain = context.createGain();
  oscillator.type = "sine";
  oscillator.frequency.value = critical ? 740 : 520;
  gain.gain.setValueAtTime(0.0001, context.currentTime);
  gain.gain.exponentialRampToValueAtTime(0.09, context.currentTime + 0.015);
  gain.gain.exponentialRampToValueAtTime(0.0001, context.currentTime + 0.18);
  oscillator.connect(gain).connect(context.destination);
  oscillator.start();
  oscillator.stop(context.currentTime + 0.2);
  oscillator.addEventListener("ended", () => void context.close(), { once: true });
}

function preferredMatch(
  matches: MonitorMatch[],
  view: "live" | "replay",
): string | null {
  const preferred = view === "replay"
    ? matches.find(isHistoricalMatch)
    : matches.find((match) => isLiveEligible(match) && match.lifecycle === "live")
      || matches.find((match) => isLiveEligible(match) && match.lifecycle === "degraded")
      || matches.find((match) => isLiveEligible(match) && match.lifecycle === "upcoming");
  return preferred?.raybet_match_id || null;
}

function dedupeMatches(matches: MonitorMatch[]): MonitorMatch[] {
  const seen = new Set<string>();
  return matches.filter((match) => {
    if (seen.has(match.raybet_match_id)) return false;
    seen.add(match.raybet_match_id);
    return true;
  });
}

function lifecycleCounts(matches: MonitorMatch[]): MonitorLifecycleCounts {
  return {
    total: matches.length,
    upcoming: matches.filter((match) => match.lifecycle === "upcoming").length,
    live: matches.filter((match) => match.lifecycle === "live").length,
    degraded: matches.filter((match) => match.lifecycle === "degraded").length,
    ended: matches.filter((match) => match.lifecycle === "ended").length,
  };
}

function matchesForView(
  matches: MonitorSnapshot["matches"],
  view: "live" | "replay",
): MonitorSnapshot["matches"] {
  return matches.filter((match) => (
    view === "replay" ? isHistoricalMatch(match) : isLiveEligible(match)
  ));
}

function summaryForView(
  summary: MonitorSnapshot["summary"],
  view: ViewMode,
): MonitorLifecycleCounts {
  if (view === "replay") return summary.history_view;
  if (view === "live") return summary.live_view;
  return summary;
}

function isHistoricalMatch(match: MonitorSnapshot["matches"][number]): boolean {
  return match.history_eligible === true;
}

function isLiveEligible(match: MonitorSnapshot["matches"][number]): boolean {
  return match.history_eligible === false;
}

function ConnectionBadge({ state }: { state: ConnectionState }) {
  const labels: Record<ConnectionState, string> = {
    connecting: "正在连接",
    live: "SSE 实时",
    fallback: "轮询降级",
    offline: "离线",
  };
  return (
    <span className={`connection-badge ${state}`}>
      <i aria-hidden="true" />
      {labels[state]}
    </span>
  );
}

function SummaryItem({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: number;
  tone?: "neutral" | "live" | "warning" | "critical";
}) {
  return (
    <span className={`summary-item ${tone}`}>
      <strong>{value}</strong>
      {label}
    </span>
  );
}
