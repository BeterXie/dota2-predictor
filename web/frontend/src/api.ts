import type {
  CanonicalTeam,
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
  VisionCalibrationBootstrap,
  VisionCalibrationCandidate,
  VisionCalibrationEvaluation,
  VisionCalibrationLabel,
} from "./types";


const MONITOR_API = "/api/monitor";


async function responseError(response: Response, fallback: string): Promise<Error> {
  const text = await response.text();
  if (text) {
    try {
      const payload = JSON.parse(text) as { detail?: unknown };
      if (typeof payload.detail === "string" && payload.detail) {
        return new Error(payload.detail);
      }
      if (Array.isArray(payload.detail)) {
        const messages = payload.detail
          .map((item) => (
            item && typeof item === "object" && "msg" in item
              ? String(item.msg)
              : ""
          ))
          .filter(Boolean);
        if (messages.length) return new Error(messages.join("；"));
      }
    } catch {
      // Non-JSON responses are still useful as a last-resort error message.
    }
  }
  return new Error(text || fallback);
}


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
    throw await responseError(response, `请求失败 (${response.status})`);
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
    throw await responseError(response, `操作失败 (${response.status})`);
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
  evidenceSourceUrl: string | null,
  csrfToken: string,
): Promise<LiveDraftMapping> {
  return mutateJson(
    `${MONITOR_API}/matches/${encodeURIComponent(matchId)}/maps/${mapNumber}/draft-mapping`,
    csrfToken,
    {
      slots,
      is_locked: isLocked,
      actor: "local-operator",
      evidence_source_url: evidenceSourceUrl,
    },
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
): Promise<LiveDraftPredictionResponse> {
  return mutateJson(
    `${MONITOR_API}/matches/${encodeURIComponent(matchId)}/maps/${mapNumber}/draft-prediction`,
    csrfToken,
    {
      mapping_version: mappingVersion,
      operator_identity: "local-operator",
      confirmation_text: "本次模型只使用队伍历史与已锁定阵容，不使用击杀、经济、经验、防御塔、肉山、实时赔率或其他游戏内状态。",
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


export function fetchTeamGrid(signal?: AbortSignal): Promise<CanonicalTeam[]> {
  return getJson("/api/team-grid", signal);
}


export function fetchVisionCalibration(
  signal?: AbortSignal,
): Promise<VisionCalibrationBootstrap> {
  return getJson("/api/vision-calibration/bootstrap", signal);
}


export function saveVisionCalibrationLabel(
  eventId: string,
  payload: {
    hero_ids: number[];
    raybet_match_id: string;
    map_number: number;
    note: string | null;
  },
  csrfToken: string,
): Promise<VisionCalibrationLabel> {
  return mutateJson(
    `/api/vision-calibration/events/${encodeURIComponent(eventId)}/label`,
    csrfToken,
    payload,
  );
}


export function buildVisionCalibrationCandidate(
  labelId: string,
  baseCandidateId: string | null,
  csrfToken: string,
): Promise<VisionCalibrationCandidate> {
  return mutateJson("/api/vision-calibration/candidates", csrfToken, {
    label_id: labelId,
    base_candidate_id: baseCandidateId,
  });
}


export function runVisionCalibrationEvaluation(
  payload: {
    label_id: string;
    candidate_id: string;
    observation_file: string;
    layout_profile: string;
    mode: "perception" | "runtime";
    captured_after: string | null;
    captured_before: string | null;
  },
  csrfToken: string,
): Promise<VisionCalibrationEvaluation> {
  return mutateJson("/api/vision-calibration/evaluations", csrfToken, payload);
}
