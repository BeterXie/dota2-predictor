import { Switch, Tab, TabList } from "@fluentui/react-components";
import {
  Bell,
  BellRinging,
  Broadcast,
  ClockCounterClockwise,
  Database,
  Pulse,
  SpeakerHigh,
} from "@phosphor-icons/react";
import { useEffect, useMemo, useRef, useState } from "react";

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
  invalidateMapping,
  snapshotStream,
} from "./api";
import { MatchRail } from "./components/MatchRail";
import { MatchWorkspace } from "./components/MatchWorkspace";
import { IntelligenceDashboard } from "./components/IntelligenceDashboard";
import { OperationsPanel } from "./components/OperationsPanel";
import type {
  ConnectionState,
  ControlComponent,
  ControlResult,
  ControlSession,
  MappingRecord,
  MatchDetail,
  MonitorSnapshot,
} from "./types";

type ViewMode = "live" | "replay" | "intelligence";

export default function App() {
  const [snapshot, setSnapshot] = useState<MonitorSnapshot | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<MatchDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [view, setView] = useState<ViewMode>(initialView);
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
  const notifiedAlerts = useRef(new Set<number>());

  useEffect(() => {
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
          setSelectedId((current) => current || preferredMatch(data, "live"));
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
  }, []);

  useEffect(() => {
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
  }, []);

  useEffect(() => {
    if (!controlSession) return;
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
  }, [controlSession]);

  useEffect(() => {
    if (!snapshot) return;
    let source: EventSource | null = null;
    let pollTimer: number | null = null;
    let reconnectTimer: number | null = null;
    let disposed = false;

    const poll = () => {
      if (pollTimer != null) return;
      setConnection("fallback");
      pollTimer = window.setInterval(() => {
        fetchBootstrap()
          .then((data) => {
            cursorRef.current = data.cursor;
            setSnapshot(data);
            setError(null);
          })
          .catch((reason: Error) => setError(reason.message || "轮询失败"));
      }, 5_000);
    };

    const connect = () => {
      if (disposed) return;
      source = snapshotStream(cursorRef.current);
      setConnection("connecting");
      source.onopen = () => {
        if (pollTimer != null) {
          window.clearInterval(pollTimer);
          pollTimer = null;
        }
        setConnection("live");
      };
      source.addEventListener("snapshot", (event) => {
        const data = JSON.parse((event as MessageEvent<string>).data) as MonitorSnapshot;
        cursorRef.current = data.cursor;
        setSnapshot(data);
        setError(null);
      });
      source.onerror = () => {
        source?.close();
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
      if (pollTimer != null) window.clearInterval(pollTimer);
      if (reconnectTimer != null) window.clearTimeout(reconnectTimer);
    };
  }, [Boolean(snapshot)]);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    const controller = new AbortController();
    setDetailLoading(true);
    fetchMatchDetail(selectedId, controller.signal)
      .then((data) => {
        setDetail(data);
        setDetailError(null);
      })
      .catch((reason: Error) => {
        if (reason.name !== "AbortError") setDetailError(reason.message || "无法加载赛事详情");
      })
      .finally(() => setDetailLoading(false));
    return () => controller.abort();
  }, [selectedId, snapshot?.cursor]);

  useEffect(() => {
    if (!selectedId) {
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
  }, [selectedId, mappingVersion, snapshot?.mapping_revision]);

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

  const matches = snapshot?.matches || [];
  const selectedMatch = matches.find((match) => match.raybet_match_id === selectedId) || null;
  const mappings = mappingState.matchId === selectedId && mappingState.version === mappingVersion
    ? mappingState.records
    : [];
  const alertCount = useMemo(
    () => (snapshot?.alerts || []).filter((item) => !item.acknowledged_at).length,
    [snapshot?.alerts],
  );

  const changeView = (next: ViewMode) => {
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
    if (snapshot && next !== "intelligence") {
      setSelectedId(preferredMatch(snapshot, next));
    }
  };

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
          aria-label="监控视图"
          selectedValue={view}
          onTabSelect={(_, data) => changeView(data.value as ViewMode)}
          size="small"
        >
          <Tab icon={<Broadcast size={17} />} value="live">实时监控</Tab>
          <Tab icon={<ClockCounterClockwise size={17} />} value="replay">历史复盘</Tab>
          <Tab icon={<Database size={17} />} value="intelligence">历史智库</Tab>
        </TabList>

        <div className="topbar-status">
          <ConnectionBadge state={connection} />
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

      {view === "intelligence" ? (
        <IntelligenceDashboard />
      ) : (
        <>
          {snapshot && (
            <section className="summary-bar" aria-label="赛事与系统摘要">
              <SummaryItem label="滚球确认" value={snapshot.summary.live} tone="live" />
              <SummaryItem label="数据降级" value={snapshot.summary.degraded} tone="warning" />
              <SummaryItem label="即将开始" value={snapshot.summary.upcoming} />
              <SummaryItem label="异常进程" value={snapshot.summary.unhealthy_components} tone="critical" />
              <span className="snapshot-time">快照 {new Date(snapshot.generated_at).toLocaleTimeString("zh-CN", { hour12: false })}</span>
            </section>
          )}

          {error && (
            <div className="global-error" role="alert">
              <strong>监控连接异常</strong>
              <span>{error}</span>
            </div>
          )}

          <div className="cockpit-grid">
            <MatchRail
              matches={matches}
              now={now}
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
        </>
      )}
    </div>
  );
}

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

function initialView(): ViewMode {
  const requested = new URLSearchParams(window.location.search).get("view");
  return requested === "replay" || requested === "intelligence" ? requested : "live";
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
  snapshot: MonitorSnapshot,
  view: Exclude<ViewMode, "intelligence">,
): string | null {
  const preferred = view === "replay"
    ? snapshot.matches.find((match) => match.lifecycle === "ended")
    : snapshot.matches.find((match) => match.lifecycle === "live")
      || snapshot.matches.find((match) => match.lifecycle === "degraded")
      || snapshot.matches.find((match) => match.lifecycle === "upcoming");
  return preferred?.raybet_match_id || snapshot.matches[0]?.raybet_match_id || null;
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
