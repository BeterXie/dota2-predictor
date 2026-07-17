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
  MappingRecord,
  MatchDetail,
  MonitorSnapshot,
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

export function fetchMatchDetail(
  matchId: string,
  signal?: AbortSignal,
): Promise<MatchDetail> {
  return getJson<MatchDetail>(
    `${MONITOR_API}/matches/${encodeURIComponent(matchId)}`,
    signal,
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
