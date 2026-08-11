import { Tab, TabList } from "@fluentui/react-components";
import { ArrowLeft, Broadcast, ClockCounterClockwise, Flask, GearSix, Trophy } from "@phosphor-icons/react";
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
import { formatDateTime } from "./format";
import { MatchRail } from "./components/MatchRail";
import { MatchWorkspace } from "./components/MatchWorkspace";
import { FanMatchRecap } from "./components/FanMatchRecap";
import { OperationsPanel } from "./components/OperationsPanel";
import { VisionCalibrationPage } from "./components/VisionCalibrationPage";
import type {
  ControlComponent,
  ControlResult,
  ControlSession,
  MappingRecord,
  MatchDetail,
  MonitorMatch,
  MonitorSnapshot,
} from "./types";


type ViewMode = "live" | "recap" | "replay" | "operations" | "vision";
const MATCH_DETAIL_REFRESH_MS = 5_000;


export default function App() {
  const [view, setView] = useState<ViewMode>(initialView);
  const [snapshot, setSnapshot] = useState<MonitorSnapshot | null>(null);
  const [history, setHistory] = useState<MonitorMatch[]>([]);
  const [historyCursor, setHistoryCursor] = useState<string | null>(null);
  const [historyHasMore, setHistoryHasMore] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
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
    if (view === "vision") {
      setError(null);
      return undefined;
    }
    const controller = new AbortController();
    let source: EventSource | null = null;
    fetchBootstrap(controller.signal).then((value) => {
      setSnapshot(value);
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
  }, [view]);

  useEffect(() => {
    if (view !== "replay" && view !== "recap") return undefined;
    const controller = new AbortController();
    setHistoryLoading(true);
    setHistoryError(null);
    fetchMonitorHistory(null, controller.signal).then((page) => {
      if (page.has_more && !page.next_cursor) {
        throw new Error("历史分页响应缺少游标");
      }
      setHistory(dedupeMatches(page.items));
      setHistoryCursor(page.next_cursor);
      setHistoryHasMore(page.has_more);
    }).catch((reason: Error) => {
      if (reason.name !== "AbortError") {
        setHistoryError(reason.message || "无法加载历史赛事");
      }
    }).finally(() => {
      if (!controller.signal.aborted) setHistoryLoading(false);
    });
    return () => controller.abort();
  }, [view]);

  useEffect(() => {
    if (view !== "live" && view !== "operations" && view !== "vision") return undefined;
    const controller = new AbortController();
    createControlSession(controller.signal).then((session) => {
      setControlSession(session);
      setComponents(session.components);
    }).catch(() => setControlMessage("进程控制不可用，监控仍保持只读"));
    return () => controller.abort();
  }, [view]);

  useEffect(() => {
    if (!selectedId || view === "operations" || view === "vision") {
      setDetail(null);
      return undefined;
    }
    let controller: AbortController | null = null;
    let refreshTimer: number | null = null;
    let stopped = false;
    setLoading(true);
    const load = async () => {
      controller = new AbortController();
      try {
        const value = await fetchMatchDetail(selectedId, controller.signal);
        if (stopped) return;
        setDetail(value);
        setError(null);
      } catch (reason) {
        if (!stopped && (reason as Error).name !== "AbortError") {
          setError((reason as Error).message || "无法加载赛事详情");
        }
      } finally {
        if (!stopped) {
          setLoading(false);
          if (view === "live") {
            refreshTimer = window.setTimeout(() => void load(), MATCH_DETAIL_REFRESH_MS);
          }
        }
      }
    };
    void load();
    return () => {
      stopped = true;
      controller?.abort();
      if (refreshTimer != null) window.clearTimeout(refreshTimer);
    };
  }, [selectedId, view]);

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

  const historyView = view === "replay" || view === "recap";
  const matches = historyView ? history : snapshot?.matches || [];
  const visibleMatches = useMemo(
    () => view === "replay" || view === "recap"
      ? matches.filter((match) => match.history_eligible)
      : matches.filter((match) => match.lifecycle !== "ended" && !match.history_eligible),
    [matches, view],
  );
  useEffect(() => {
    setSelectedId((current) => {
      if (current && visibleMatches.some((match) => match.raybet_match_id === current)) {
        return current;
      }
      if (view === "vision") return null;
      return view === "operations" ? preferredMatch(visibleMatches) : null;
    });
  }, [view, visibleMatches]);
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

  const loadMoreHistory = async () => {
    if (!historyHasMore || !historyCursor || historyLoading) return;
    const requestedCursor = historyCursor;
    setHistoryLoading(true);
    setHistoryError(null);
    try {
      const page = await fetchMonitorHistory(requestedCursor);
      if (page.has_more && (!page.next_cursor || page.next_cursor === requestedCursor)) {
        throw new Error("历史分页游标没有向前推进");
      }
      setHistory((current) => dedupeMatches([...current, ...page.items]));
      setHistoryCursor(page.next_cursor);
      setHistoryHasMore(page.has_more);
    } catch (reason) {
      setHistoryError(reason instanceof Error ? reason.message : "无法加载更多历史赛事");
    } finally {
      setHistoryLoading(false);
    }
  };

  return (
    <div className="app-shell">
      <header className={[
        "app-header",
        view === "vision" ? "vision-header-mode" : "",
        view === "recap" ? "recap-header-mode" : "",
      ].filter(Boolean).join(" ")}>
        <div className="brand-block">
          <Broadcast size={24} aria-hidden="true" />
          <div>
            <strong>{view === "recap" ? "Dota 2 比赛复盘" : "Dota 2 实时阵容预测"}</strong>
            <span>{view === "recap" ? "赛果、阵容与关键走势" : "RayBet · HUD · Team Rating · R.O.S.H."}</span>
          </div>
        </div>
        <TabList
          aria-label="产品导航"
          className="primary-tabs"
          selectedValue={view}
          onTabSelect={(_, data) => changeView(data.value as ViewMode)}
        >
          <Tab icon={<Broadcast size={17} />} value="live">实时赛事</Tab>
          <Tab icon={<Trophy size={17} />} value="recap">比赛复盘</Tab>
          <Tab icon={<ClockCounterClockwise size={17} />} value="replay">历史结果</Tab>
          <Tab icon={<GearSix size={17} />} value="operations">运行控制</Tab>
          <Tab icon={<Flask size={17} />} value="vision">Vision 校正</Tab>
        </TabList>
      </header>

      {error && <div className="global-error" role="alert">{error}</div>}
      <div className={[
        "app-content",
        view === "vision" ? "vision-mode" : "",
        view === "live" || view === "replay" || view === "recap"
          ? selectedId ? "detail-mode" : "list-mode"
          : "",
      ].filter(Boolean).join(" ")}>
        {view === "vision" ? (
          <VisionCalibrationPage csrfToken={controlSession?.csrf_token || null} />
        ) : view === "operations" ? (
          <>
            <MatchRail
              matches={visibleMatches}
              mode="live"
              onSelect={setSelectedId}
              selectedId={selectedId}
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
              onAcknowledge={(id) => {
                const token = requireControl();
                if (token) void acknowledgeAlert(id, token);
              }}
              onApproveMapping={(id) => void mutateMapping((token) => approveAutomaticMapping(id, token))}
              onControl={(component, action) => void runControl(component, action)}
              onCreateAutomaticMap={(id, map) => void mutateMapping((token) => createAutomaticMapping(id, map, token))}
              onInvalidateMapping={(id) => void mutateMapping((token) => invalidateMapping(id, "operator_invalidated", token))}
            />
          </>
        ) : selectedId ? (
          <div className="live-detail-view">
            <div className="live-detail-toolbar">
              <button
                aria-label={view === "recap"
                  ? "返回比赛复盘列表"
                  : view === "replay" ? "返回历史结果列表" : "返回实时与赛前赛事列表"}
                className="live-detail-back"
                onClick={() => {
                  setSelectedId(null);
                  setDetail(null);
                }}
                type="button"
              >
                <ArrowLeft size={17} weight="bold" aria-hidden="true" />
                <span>{view === "recap" ? "比赛复盘" : view === "replay" ? "历史结果" : "赛事列表"}</span>
              </button>
              <div className="live-detail-context">
                <strong>{selectedMatch
                  ? `${selectedMatch.team_one || "队伍一"} vs ${selectedMatch.team_two || "队伍二"}`
                  : "赛事详情"}</strong>
                <span>{view === "recap"
                  ? `${selectedMatch?.tournament || "赛事待确认"} · ${formatDateTime(selectedMatch?.scheduled_at)}`
                  : selectedMatch?.display_name
                    || selectedMatch?.tournament
                    || `RayBet ${selectedId}`}</span>
              </div>
            </div>
            {view === "recap" ? (
              <FanMatchRecap
                detail={detail}
                error={error}
                loading={loading || (historyLoading && history.length === 0)}
                match={selectedMatch}
              />
            ) : (
              <MatchWorkspace
                csrfToken={controlSession?.csrf_token || null}
                detail={detail}
                error={error}
                loading={loading || (view === "replay" && historyLoading && history.length === 0)}
                match={selectedMatch}
                replay={view === "replay"}
              />
            )}
          </div>
        ) : (
          <MatchRail
            hasMore={historyView && historyHasMore}
            loadError={historyView ? historyError : null}
            loadingMore={historyView && historyLoading}
            matches={visibleMatches}
            mode={view === "recap" ? "recap" : view === "replay" ? "history" : "live"}
            onLoadMore={historyView ? () => void loadMoreHistory() : undefined}
            onSelect={setSelectedId}
            selectedId={null}
            variant="page"
          />
        )}
      </div>
    </div>
  );
}


function initialView(): ViewMode {
  const value = new URLSearchParams(window.location.search).get("view");
  return value === "recap" || value === "replay" || value === "operations" || value === "vision"
    ? value
    : "live";
}


function preferredMatch(matches: MonitorMatch[]): string | null {
  return matches.find((match) => match.lifecycle === "live")?.raybet_match_id
    || matches.find((match) => match.lifecycle !== "ended")?.raybet_match_id
    || null;
}


function dedupeMatches(matches: MonitorMatch[]): MonitorMatch[] {
  return [...new Map(matches.map((match) => [match.raybet_match_id, match])).values()];
}
