import type {
  ControlComponent,
  ControlResult,
  ControlSession,
  LiveDraftMapping,
  LiveDraftPredictionResponse,
  LiveDraftSlot,
  LiveGameSnapshot,
  MappingRecord,
  MatchDetail,
  MonitorHistoryPage,
  MonitorSnapshot,
  PrematchHeroGrid,
} from "./types";


const MONITOR_API = "/api/monitor";


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
    const message = await response.text();
    throw new Error(message || `操作失败 (${response.status})`);
  }
  return response.json() as Promise<T>;
}


export function fetchBootstrap(signal?: AbortSignal): Promise<MonitorSnapshot> {
  return getJson(`${MONITOR_API}/bootstrap`, signal);
}


export function fetchMonitorHistory(
  cursor?: string | null,
  signal?: AbortSignal,
): Promise<MonitorHistoryPage> {
  const query = new URLSearchParams({ limit: "20" });
  if (cursor) query.set("cursor", cursor);
  return getJson(`${MONITOR_API}/history?${query}`, signal);
}


export function fetchMatchDetail(matchId: string, signal?: AbortSignal): Promise<MatchDetail> {
  return getJson(`${MONITOR_API}/matches/${encodeURIComponent(matchId)}`, signal);
}


export function saveLiveDraftMapping(
  matchId: string,
  mapNumber: number,
  slots: LiveDraftSlot[],
  isLocked: boolean,
  csrfToken: string,
): Promise<LiveDraftMapping> {
  return mutateJson(
    `${MONITOR_API}/matches/${encodeURIComponent(matchId)}/maps/${mapNumber}/draft-mapping`,
    csrfToken,
    { slots, is_locked: isLocked, actor: "local-operator" },
  );
}


export function fetchLiveDraftPrediction(
  matchId: string,
  mapNumber: number,
  mappingVersion: number,
  signal?: AbortSignal,
): Promise<LiveDraftPredictionResponse> {
  return getJson(
    `${MONITOR_API}/matches/${encodeURIComponent(matchId)}/maps/${mapNumber}/draft-prediction?mapping_version=${mappingVersion}`,
    signal,
  );
}


export function createLiveDraftPrediction(
  matchId: string,
  mapNumber: number,
  mappingVersion: number,
  csrfToken: string,
  gameClockSeconds: number | null,
): Promise<LiveDraftPredictionResponse> {
  return mutateJson(
    `${MONITOR_API}/matches/${encodeURIComponent(matchId)}/maps/${mapNumber}/draft-prediction`,
    csrfToken,
    {
      mapping_version: mappingVersion,
      operator_identity: "local-operator",
      confirmation_text: "本次模型只使用队伍历史与已锁定阵容，不使用击杀、经济、经验、防御塔、肉山、实时赔率或其他游戏内状态。",
      game_clock_seconds: gameClockSeconds,
      vision_frame_timestamp: null,
      draft_state_marker: "draft_complete",
      live_state_input_used: false,
    },
  );
}


export function correctLiveGameSnapshot(
  matchId: string,
  mapNumber: number,
  values: Pick<
    LiveGameSnapshot,
    "game_time_seconds" | "radiant_networth" | "dire_networth" | "radiant_kills" | "dire_kills"
  >,
  csrfToken: string,
): Promise<LiveGameSnapshot> {
  return mutateJson(
    `${MONITOR_API}/matches/${encodeURIComponent(matchId)}/maps/${mapNumber}/game-snapshots`,
    csrfToken,
    { ...values, actor: "local-operator" },
  );
}


export function snapshotStream(cursor?: string): EventSource {
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
  return new EventSource(`${MONITOR_API}/events${query}`);
}


export function createControlSession(signal?: AbortSignal): Promise<ControlSession> {
  return getJson(`${MONITOR_API}/control/session`, signal);
}


export function controlComponent(
  component: ControlComponent["component"],
  action: ControlResult["action"],
  csrfToken: string,
): Promise<ControlResult> {
  return mutateJson(
    `${MONITOR_API}/control/${encodeURIComponent(component)}/${action}`,
    csrfToken,
    { request_id: crypto.randomUUID() },
  );
}


export function acknowledgeAlert(
  incidentId: number,
  csrfToken: string,
): Promise<{ acknowledged: boolean }> {
  return mutateJson(
    `${MONITOR_API}/control/alerts/${incidentId}/acknowledge`,
    csrfToken,
    { actor: "local-operator" },
  );
}


export async function fetchMappings(
  matchId: string,
  signal?: AbortSignal,
): Promise<MappingRecord[]> {
  const payload = await getJson<{ mappings: MappingRecord[] }>(
    `${MONITOR_API}/mappings/${encodeURIComponent(matchId)}`,
    signal,
  );
  return payload.mappings;
}


export function approveAutomaticMapping(
  mappingId: number,
  csrfToken: string,
): Promise<{ approval_id: number }> {
  return mutateJson(`${MONITOR_API}/mappings/${mappingId}/approve-automatic`, csrfToken, {
    actor: "local-operator",
  });
}


export function invalidateMapping(
  mappingId: number,
  reason: string,
  csrfToken: string,
): Promise<{ invalidation_id: number }> {
  return mutateJson(`${MONITOR_API}/mappings/${mappingId}/invalidate`, csrfToken, {
    actor: "local-operator",
    reason,
  });
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


export function fetchHeroGrid(signal?: AbortSignal): Promise<PrematchHeroGrid> {
  return getJson("/api/hero-grid", signal);
}
