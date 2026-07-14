import { Button, Tab, TabList } from "@fluentui/react-components";
import {
  Bell,
  Broadcast,
  ClockCounterClockwise,
  Pulse,
  SpeakerHigh,
} from "@phosphor-icons/react";
import { useEffect, useMemo, useRef, useState } from "react";

import { fetchBootstrap, fetchMatchDetail, snapshotStream } from "./api";
import { MatchRail } from "./components/MatchRail";
import { MatchWorkspace } from "./components/MatchWorkspace";
import { OperationsPanel } from "./components/OperationsPanel";
import type {
  ConnectionState,
  MatchDetail,
  MonitorSnapshot,
} from "./types";

type ViewMode = "live" | "replay";

export default function App() {
  const [snapshot, setSnapshot] = useState<MonitorSnapshot | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<MatchDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [view, setView] = useState<ViewMode>("live");
  const [now, setNow] = useState(Date.now());
  const cursorRef = useRef<string | undefined>(undefined);

  useEffect(() => {
    const controller = new AbortController();
    fetchBootstrap(controller.signal)
      .then((data) => {
        cursorRef.current = data.cursor;
        setSnapshot(data);
        setSelectedId((current) => current || preferredMatch(data, "live"));
        setError(null);
      })
      .catch((reason: Error) => {
        if (reason.name !== "AbortError") {
          setError(reason.message || "无法加载监控数据");
          setConnection("offline");
        }
      });
    return () => controller.abort();
  }, []);

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

  const matches = snapshot?.matches || [];
  const selectedMatch = matches.find((match) => match.raybet_match_id === selectedId) || null;
  const alertCount = useMemo(() => {
    if (!snapshot) return 0;
    const workerAlerts = snapshot.health.filter(
      (item) => item.component.endsWith("_worker") && item.status !== "healthy",
    ).length;
    const readinessAlerts = selectedMatch
      ? Object.values(selectedMatch.readiness).filter((item) => item.status !== "ready").length
      : 0;
    return workerAlerts + readinessAlerts;
  }, [selectedMatch, snapshot]);

  const changeView = (next: ViewMode) => {
    setView(next);
    if (snapshot) setSelectedId(preferredMatch(snapshot, next));
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
        </TabList>

        <div className="topbar-status">
          <ConnectionBadge state={connection} />
          <Button
            appearance="subtle"
            icon={<SpeakerHigh size={17} />}
            size="small"
            title="浏览器和声音通知将在告警授权后启用"
          >
            声音
          </Button>
          <Button appearance="subtle" icon={<Bell size={17} />} size="small">
            告警 {alertCount || ""}
          </Button>
        </div>
      </header>

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
        <OperationsPanel health={snapshot?.health || []} match={selectedMatch} />
      </div>
    </div>
  );
}

function preferredMatch(snapshot: MonitorSnapshot, view: ViewMode): string | null {
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
