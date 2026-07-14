import type { MatchDetail, MonitorSnapshot } from "./types";

const MONITOR_API = "/api/monitor";

async function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, {
    signal,
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    const message = await response.text();
    throw new Error(message || `请求失败 (${response.status})`);
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

export function snapshotStream(cursor?: string): EventSource {
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
  return new EventSource(`${MONITOR_API}/events${query}`);
}
