import { Tab, TabList } from "@fluentui/react-components";
import { Broadcast, ClockCounterClockwise, GearSix } from "@phosphor-icons/react";
import { useEffect, useMemo, useState } from "react";

import {
  acknowledgeAlert,
  approveAutomaticMapping,
  controlComponent,
  createAutomaticMapping,
  createControlSession,
  fetchBootstrap,
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
  ControlComponent,
  ControlResult,
  ControlSession,
  MappingRecord,
  MatchDetail,
  MonitorMatch,
  MonitorSnapshot,
} from "./types";


type ViewMode = "live" | "replay" | "operations";


export default function App() {
  const [view, setView] = useState<ViewMode>(initialView);
  const [snapshot, setSnapshot] = useState<MonitorSnapshot | null>(null);
  const [history, setHistory] = useState<MonitorMatch[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<MatchDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [controlSession, setControlSession] = useState<ControlSession | null>(null);
  const [components, setComponents] = useState<ControlComponent[]>([]);
  const [controlBusy, setControlBusy] = useState<string | null>(null);
  const [controlMessage, setControlMessage] = useState<string | null>(null);
  const [mappings, setMappings] = useState<MappingRecord[]>([]);

  useEffect(() => {
    const controller = new AbortController();
    let source: EventSource | null = null;
    fetchBootstrap(controller.signal).then((value) => {
      setSnapshot(value);
      setSelectedId((current) => current || preferredMatch(value.matches));
      setError(null);
      source = snapshotStream(value.cursor);
      source.addEventListener("snapshot", (event) => {
        const next = JSON.parse((event as MessageEvent<string>).data) as MonitorSnapshot;
        setSnapshot(next);
        setError(null);
      });
      source.onerror = () => setError("实时事件流不可用，正在等待重新连接");
    }).catch((reason: Error) => setError(reason.message || "无法加载实时赛事"));
    return () => {
      controller.abort();
      source?.close();
    };
  }, []);

  useEffect(() => {
    if (view !== "replay") return undefined;
    const controller = new AbortController();
    fetchMonitorHistory(null, controller.signal).then((page) => {
      setHistory(page.items);
      setSelectedId((current) => current || page.items[0]?.raybet_match_id || null);
    }).catch((reason: Error) => {
      if (reason.name !== "AbortError") setError(reason.message || "无法加载历史赛事");
    });
    return () => controller.abort();
  }, [view]);

  useEffect(() => {
    if (view !== "live" && view !== "operations") return undefined;
    const controller = new AbortController();
    createControlSession(controller.signal).then((session) => {
      setControlSession(session);
      setComponents(session.components);
    }).catch(() => setControlMessage("进程控制不可用，监控仍保持只读"));
    return () => controller.abort();
  }, [view]);

  useEffect(() => {
    if (!selectedId || view === "operations") {
      setDetail(null);
      return undefined;
    }
    const controller = new AbortController();
    setLoading(true);
    fetchMatchDetail(selectedId, controller.signal).then((value) => {
      setDetail(value);
      setError(null);
    }).catch((reason: Error) => {
      if (reason.name !== "AbortError") setError(reason.message || "无法加载赛事详情");
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [selectedId, view, snapshot?.cursor]);

  useEffect(() => {
    if (!selectedId || view !== "operations") {
      setMappings([]);
      return undefined;
    }
    const controller = new AbortController();
    fetchMappings(selectedId, controller.signal).then(setMappings).catch(() => {
      setMappings([]);
      setControlMessage("无法读取 strict mapping");
    });
    return () => controller.abort();
  }, [selectedId, view, snapshot?.mapping_revision]);

  const matches = view === "replay" ? history : snapshot?.matches || [];
  const visibleMatches = useMemo(
    () => view === "replay"
      ? matches.filter((match) => match.history_eligible)
      : matches.filter((match) => match.lifecycle !== "ended"),
    [matches, view],
  );
  const selectedMatch = matches.find((match) => match.raybet_match_id === selectedId)
    || detail
    || null;

  const changeView = (next: ViewMode) => {
    setView(next);
    setSelectedId(null);
    setDetail(null);
    const query = next === "live" ? "" : `?view=${next}`;
    window.history.pushState(null, "", `/monitor${query}`);
  };

  const requireControl = (): string | null => {
    if (!controlSession) {
      setControlMessage("控制会话不可用");
      return null;
    }
    return controlSession.csrf_token;
  };

  const runControl = async (
    component: ControlComponent["component"],
    action: ControlResult["action"],
  ) => {
    const token = requireControl();
    if (!token) return;
    setControlBusy(`${component}:${action}`);
    try {
      const result = await controlComponent(component, action, token);
      setControlMessage(result.detail || result.status);
      const session = await createControlSession();
      setControlSession(session);
      setComponents(session.components);
    } catch (reason) {
      setControlMessage(reason instanceof Error ? reason.message : "进程控制失败");
    } finally {
      setControlBusy(null);
    }
  };

  const mutateMapping = async (operation: (token: string) => Promise<unknown>) => {
    const token = requireControl();
    if (!token || !selectedId) return;
    try {
      await operation(token);
      setMappings(await fetchMappings(selectedId));
    } catch (reason) {
      setControlMessage(reason instanceof Error ? reason.message : "mapping 操作失败");
    }
  };

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand-block">
          <Broadcast size={24} aria-hidden="true" />
          <div><strong>Dota 2 实时阵容预测</strong><span>RayBet · HUD · Team Rating · R.O.S.H.</span></div>
        </div>
        <TabList selectedValue={view} onTabSelect={(_, data) => changeView(data.value as ViewMode)}>
          <Tab icon={<Broadcast size={17} />} value="live">实时赛事</Tab>
          <Tab icon={<ClockCounterClockwise size={17} />} value="replay">历史结果</Tab>
          <Tab icon={<GearSix size={17} />} value="operations">运行控制</Tab>
        </TabList>
      </header>

      {error && <div className="global-error" role="alert">{error}</div>}
      <div className="app-content">
        <MatchRail
          matches={visibleMatches}
          mode={view === "replay" ? "history" : "live"}
          onSelect={setSelectedId}
          selectedId={selectedId}
        />
        {view === "operations" ? (
          <OperationsPanel
            alerts={snapshot?.alerts || []}
            busyKey={controlBusy}
            components={components}
            controlMessage={controlMessage}
            controlsEnabled={Boolean(controlSession)}
            health={snapshot?.health || []}
            mappings={mappings}
            match={selectedMatch}
            onAcknowledge={(id) => {
              const token = requireControl();
              if (token) void acknowledgeAlert(id, token);
            }}
            onApproveMapping={(id) => void mutateMapping((token) => approveAutomaticMapping(id, token))}
            onControl={(component, action) => void runControl(component, action)}
            onCreateAutomaticMap={(id, map) => void mutateMapping((token) => createAutomaticMapping(id, map, token))}
            onInvalidateMapping={(id) => void mutateMapping((token) => invalidateMapping(id, "operator_invalidated", token))}
          />
        ) : (
          <MatchWorkspace
            csrfToken={controlSession?.csrf_token || null}
            detail={detail}
            error={error}
            loading={loading}
            match={selectedMatch}
            replay={view === "replay"}
          />
        )}
      </div>
    </div>
  );
}


function initialView(): ViewMode {
  const value = new URLSearchParams(window.location.search).get("view");
  return value === "replay" || value === "operations" ? value : "live";
}


function preferredMatch(matches: MonitorMatch[]): string | null {
  return matches.find((match) => match.lifecycle === "live")?.raybet_match_id
    || matches.find((match) => match.lifecycle !== "ended")?.raybet_match_id
    || null;
}
