import type {
  ControlComponent,
  ControlResult,
  ControlSession,
  ExactPostmatchAttribution,
  IntelligenceMatchDetail,
  IntelligenceMatchPage,
  IntelligenceOverview,
  IntelligencePlayerPage,
  IntelligenceTeamPage,
  LiveDraftMapping,
  LiveDraftSlot,
  LiveGameSnapshot,
  MappingRecord,
  MatchDetail,
  MonitorHistoryPage,
  MonitorSnapshot,
  PrematchDraft,
  PrematchHeroGrid,
  PrematchLeague,
  PrematchRecentMatch,
  PrematchTeam,
  RoshAnalysisRequest,
  RoshAnalysisRunResponse,
} from "./types";

const MONITOR_API = "/api/monitor";
const INTELLIGENCE_API = "/api/intelligence";

async function getJson<T>(
  url: string,
  signal?: AbortSignal,
  headers: Record<string, string> = {},
): Promise<T> {
  const response = await fetch(url, {
    signal,
    headers: { Accept: "application/json", ...headers },
  });
  if (!response.ok) {
    const message = await response.text();
    throw new Error(message || `请求失败 (${response.status})`);
  }
  return response.json() as Promise<T>;
}

async function mutateJson<T>(
  url: string,
  csrfToken: string,
  body: Record<string, unknown>,
): Promise<T> {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "X-Monitor-CSRF": csrfToken,
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const payload = await response.text();
    throw new Error(payload || `操作失败 (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export function fetchBootstrap(signal?: AbortSignal): Promise<MonitorSnapshot> {
  return getJson<MonitorSnapshot>(`${MONITOR_API}/bootstrap`, signal);
}

export function fetchMonitorHistory(
  cursor?: string | null,
  signal?: AbortSignal,
): Promise<MonitorHistoryPage> {
  const query = new URLSearchParams({ limit: "20" });
  if (cursor) query.set("cursor", cursor);
  return getJson<MonitorHistoryPage>(
    `${MONITOR_API}/history?${query.toString()}`,
    signal,
  );
}

export function fetchMatchDetail(
  matchId: string,
  signal?: AbortSignal,
): Promise<MatchDetail> {
  return getJson<MatchDetail>(
    `${MONITOR_API}/matches/${encodeURIComponent(matchId)}`,
    signal,
  );
}

export function saveLiveDraftMapping(
  matchId: string,
  mapNumber: number,
  slots: LiveDraftSlot[],
  isLocked: boolean,
  csrfToken: string,
): Promise<LiveDraftMapping> {
  return mutateJson<LiveDraftMapping>(
    `${MONITOR_API}/matches/${encodeURIComponent(matchId)}/maps/${mapNumber}/draft-mapping`,
    csrfToken,
    { slots, is_locked: isLocked, actor: "local-operator" },
  );
}

export function correctLiveGameSnapshot(
  matchId: string,
  mapNumber: number,
  values: Pick<
    LiveGameSnapshot,
    | "game_time_seconds"
    | "radiant_networth"
    | "dire_networth"
    | "radiant_kills"
    | "dire_kills"
  >,
  csrfToken: string,
): Promise<LiveGameSnapshot> {
  return mutateJson<LiveGameSnapshot>(
    `${MONITOR_API}/matches/${encodeURIComponent(matchId)}/maps/${mapNumber}/game-snapshots`,
    csrfToken,
    { ...values, actor: "local-operator" },
  );
}

export function fetchExactPostmatchAttribution(
  matchId: string,
  mapNumber: number,
  signal?: AbortSignal,
): Promise<ExactPostmatchAttribution> {
  return getJson<ExactPostmatchAttribution>(
    `${MONITOR_API}/matches/${encodeURIComponent(matchId)}/maps/${encodeURIComponent(mapNumber)}/postmatch`,
    signal,
  );
}

export function snapshotStream(cursor?: string): EventSource {
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
  return new EventSource(`${MONITOR_API}/events${query}`);
}

export function createControlSession(signal?: AbortSignal): Promise<ControlSession> {
  return getJson<ControlSession>(`${MONITOR_API}/control/session`, signal);
}

export async function fetchControlComponents(
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ControlComponent[]> {
  const payload = await getJson<{ components: ControlComponent[] }>(
    `${MONITOR_API}/control/components`,
    signal,
    { "X-Monitor-CSRF": csrfToken },
  );
  return payload.components;
}

export function controlComponent(
  component: ControlComponent["component"],
  action: ControlResult["action"],
  csrfToken: string,
): Promise<ControlResult> {
  return mutateJson<ControlResult>(
    `${MONITOR_API}/control/${encodeURIComponent(component)}/${action}`,
    csrfToken,
    { request_id: crypto.randomUUID() },
  );
}

export function acknowledgeAlert(incidentId: number, csrfToken: string): Promise<{ acknowledged: boolean }> {
  return mutateJson(
    `${MONITOR_API}/control/alerts/${incidentId}/acknowledge`,
    csrfToken,
    { actor: "local-operator" },
  );
}

export async function fetchMappings(matchId: string, signal?: AbortSignal): Promise<MappingRecord[]> {
  const payload = await getJson<{ mappings: MappingRecord[] }>(
    `${MONITOR_API}/mappings/${encodeURIComponent(matchId)}`,
    signal,
  );
  return payload.mappings;
}

export function approveAutomaticMapping(mappingId: number, csrfToken: string): Promise<{ approval_id: number }> {
  return mutateJson(
    `${MONITOR_API}/mappings/${mappingId}/approve-automatic`,
    csrfToken,
    { actor: "local-operator" },
  );
}

export function invalidateMapping(
  mappingId: number,
  reason: string,
  csrfToken: string,
): Promise<{ invalidation_id: number }> {
  return mutateJson(
    `${MONITOR_API}/mappings/${mappingId}/invalidate`,
    csrfToken,
    { actor: "local-operator", reason },
  );
}

export function createAutomaticMapping(
  sourceMappingId: number,
  mapNumber: number,
  csrfToken: string,
): Promise<{ mapping_id: number }> {
  return mutateJson(
    `${MONITOR_API}/mappings/${sourceMappingId}/automatic/${mapNumber}`,
    csrfToken,
    { actor: "local-operator" },
  );
}

export function fetchIntelligenceOverview(
  signal?: AbortSignal,
): Promise<IntelligenceOverview> {
  return getJson<IntelligenceOverview>(`${INTELLIGENCE_API}/overview`, signal);
}

export function fetchIntelligenceMatches(
  options: {
    page?: number;
    pageSize?: number;
    label?: string;
    search?: string;
  } = {},
  signal?: AbortSignal,
): Promise<IntelligenceMatchPage> {
  const query = new URLSearchParams({
    page: String(options.page || 1),
    page_size: String(options.pageSize || 20),
  });
  if (options.label) query.set("label", options.label);
  if (options.search) query.set("search", options.search);
  return getJson<IntelligenceMatchPage>(
    `${INTELLIGENCE_API}/matches?${query.toString()}`,
    signal,
  );
}

export function fetchIntelligenceMatchDetail(
  matchId: number,
  signal?: AbortSignal,
): Promise<IntelligenceMatchDetail> {
  return getJson<IntelligenceMatchDetail>(
    `${INTELLIGENCE_API}/matches/${encodeURIComponent(matchId)}`,
    signal,
  );
}

export function fetchIntelligencePlayers(
  options: {
    page?: number;
    pageSize?: number;
    position?: number;
    search?: string;
  } = {},
  signal?: AbortSignal,
): Promise<IntelligencePlayerPage> {
  const query = new URLSearchParams({
    page: String(options.page || 1),
    page_size: String(options.pageSize || 20),
  });
  if (options.position) query.set("position", String(options.position));
  if (options.search) query.set("search", options.search);
  return getJson<IntelligencePlayerPage>(
    `${INTELLIGENCE_API}/players?${query.toString()}`,
    signal,
  );
}

export function fetchIntelligenceTeams(
  signal?: AbortSignal,
): Promise<IntelligenceTeamPage> {
  return getJson<IntelligenceTeamPage>(`${INTELLIGENCE_API}/teams`, signal);
}

export function fetchPrematchTeams(signal?: AbortSignal): Promise<PrematchTeam[]> {
  return getJson<PrematchTeam[]>("/api/teams", signal);
}

export function fetchPrematchLeagues(signal?: AbortSignal): Promise<PrematchLeague[]> {
  return getJson<PrematchLeague[]>("/api/leagues", signal);
}

export function fetchPrematchHeroGrid(signal?: AbortSignal): Promise<PrematchHeroGrid> {
  return getJson<PrematchHeroGrid>("/api/hero-grid", signal);
}

export function fetchPrematchRecentMatches(
  signal?: AbortSignal,
): Promise<PrematchRecentMatch[]> {
  return getJson<PrematchRecentMatch[]>("/api/recent-matches?limit=30", signal);
}

export function fetchPrematchDraft(
  matchId: number,
  signal?: AbortSignal,
): Promise<PrematchDraft> {
  return getJson<PrematchDraft>(
    `/api/matches/${encodeURIComponent(matchId)}/draft`,
    signal,
  );
}

export async function createRoshAnalysis(
  payload: RoshAnalysisRequest,
  signal?: AbortSignal,
): Promise<RoshAnalysisRunResponse> {
  const response = await fetch("/api/prematch/rosh-analysis", {
    method: "POST",
    signal,
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => null) as {
      detail?: string | { message?: string; error_code?: string };
    } | null;
    const detail = error?.detail;
    const message = typeof detail === "string"
      ? detail
      : detail?.message || detail?.error_code;
    throw new Error(message || `请求失败 (${response.status})`);
  }
  return response.json() as Promise<RoshAnalysisRunResponse>;
}

export function fetchRoshAnalysis(
  runId: string,
  signal?: AbortSignal,
): Promise<RoshAnalysisRunResponse> {
  return getJson<RoshAnalysisRunResponse>(
    `/api/prematch/rosh-analysis/${encodeURIComponent(runId)}`,
    signal,
  );
}

export async function triggerPrematchFetch(
  matchId?: number,
): Promise<{ status: string; message: string }> {
  const query = new URLSearchParams();
  if (matchId) {
    query.set("match_id", String(matchId));
    query.set("force", "true");
  }
  const response = await fetch(`/api/fetch-latest${query.size ? `?${query}` : ""}`, {
    method: "POST",
    headers: { Accept: "application/json", "X-Dota2-Admin-Action": "fetch" },
  });
  const payload = await response.json().catch(() => null) as {
    status?: string;
    message?: string;
    detail?: string;
  } | null;
  if (!response.ok) {
    throw new Error(payload?.detail || `请求失败 (${response.status})`);
  }
  return {
    status: payload?.status || "started",
    message: payload?.message || "抓取任务已启动",
  };
}
