(function installClassification(root) {
  "use strict";
  const api = root.RaybetMonitor || (root.RaybetMonitor = {});
  const { DOTA2_GAME_ID, RAYBET_HOSTS } = api;
  if (!DOTA2_GAME_ID) throw new Error("RaybetMonitor constants must load before classification");

const MATCH_FIELDS = Object.freeze([
  "id", "match_id", "game_id", "status", "game_name", "match_name",
  "match_short_name", "start_time", "end_time", "round", "tournament_name",
  "tournament_short_name",
]);
const TEAM_FIELDS = Object.freeze([
  "team_id", "team_name", "team_short_name", "score", "pos", "id", "match_id",
]);
const ODDS_FIELDS = Object.freeze([
  "odds_group_id", "game_id", "tournament_id", "value", "win", "status", "last_update",
  "match_name", "group_name", "group_short_name", "id", "odds_id", "sort_index", "tag",
  "tab", "match_stage", "team_id", "name", "match_id", "odds",
]);
const WS_MARKET_FIELDS = Object.freeze([
  "odds_id", "odds_group_id", "value", "win", "status", "last_update",
  "group_name", "group_short_name", "tag", "tab", "match_stage", "team_id", "name",
]);

function scalar(value) {
  return value === null || ["string", "number", "boolean"].includes(typeof value);
}

function copyScalars(source, fields) {
  const target = {};
  if (!source || typeof source !== "object" || Array.isArray(source)) return target;
  for (const field of fields) {
    if (Object.hasOwn(source, field) && scalar(source[field])) target[field] = source[field];
  }
  return target;
}

function matchIdFrom(value) {
  if (!value || typeof value !== "object") return null;
  const id = value.match_id ?? value.id;
  return id === null || id === undefined || id === "" ? null : String(id);
}

function gameIdFrom(...values) {
  for (const value of values) {
    if (value === null || value === undefined || value === "") continue;
    const parsed = Number(value);
    if (Number.isInteger(parsed)) return parsed;
  }
  return null;
}

function sourcePathFrom(value, base = "https://www.ray086.com/") {
  if (typeof value !== "string" || value.length === 0) return null;
  try {
    const url = new URL(value, base);
    if (!["https:", "wss:"].includes(url.protocol)
        || !RAYBET_HOSTS.includes(url.hostname)
        || (url.port && url.port !== "443")) return null;
    return url.pathname;
  } catch {
    return null;
  }
}

function queryMatchIdFrom(value, base = "https://www.ray086.com/") {
  if (typeof value !== "string" || value.length === 0) return null;
  try {
    const url = new URL(value, base);
    if (!["https:", "wss:"].includes(url.protocol)
        || !RAYBET_HOSTS.includes(url.hostname)
        || (url.port && url.port !== "443")) return null;
    return url.searchParams.get("match_id");
  } catch {
    return null;
  }
}

function createClassificationState(matchIds = []) {
  return { dotaMatchIds: new Set(Array.from(matchIds, String)) };
}

function extractMatchRow(row) {
  const output = copyScalars(row, MATCH_FIELDS);
  if (Array.isArray(row?.team)) output.team = row.team.map((team) => copyScalars(team, TEAM_FIELDS));
  return output;
}

function extractOddsPayload(payload) {
  const result = payload?.result;
  if (!result || typeof result !== "object" || Array.isArray(result)) return null;
  const safeResult = copyScalars(result, [...MATCH_FIELDS, ...ODDS_FIELDS]);
  if (Array.isArray(result.team)) safeResult.team = result.team.map((team) => copyScalars(team, TEAM_FIELDS));
  if (Array.isArray(result.odds)) safeResult.odds = result.odds.map((odds) => copyScalars(odds, ODDS_FIELDS));
  const output = { result: safeResult };
  if (scalar(payload.code)) output.code = payload.code;
  return output;
}

function extractWebSocketMarketPayload(payload) {
  const nested = payload?.result;
  const result = nested && typeof nested === "object" && !Array.isArray(nested)
    ? nested : payload;
  if (!result || typeof result !== "object" || Array.isArray(result)) return null;
  const hasMarketStructure = Array.isArray(result.odds)
    || WS_MARKET_FIELDS.some(
      (field) => Object.hasOwn(result, field) && scalar(result[field]),
    );
  if (!hasMarketStructure) return null;
  const safeResult = copyScalars(result, [...MATCH_FIELDS, ...ODDS_FIELDS]);
  if (Array.isArray(result.team)) {
    safeResult.team = result.team.map((team) => copyScalars(team, TEAM_FIELDS));
  }
  if (Array.isArray(result.odds)) {
    safeResult.odds = result.odds.map((odds) => copyScalars(odds, ODDS_FIELDS));
  }
  const output = {result: safeResult};
  if (scalar(payload.code)) output.code = payload.code;
  return output;
}

function classifyCandidate(candidate, state = createClassificationState()) {
  const sourcePath = sourcePathFrom(candidate?.sourceUrl ?? candidate?.sourcePath);
  const payload = candidate?.payload;
  if (!sourcePath || !payload || typeof payload !== "object") {
    return { events: [], ignoredReason: "invalid_candidate" };
  }

  if (sourcePath.endsWith("/v2/match") || sourcePath === "/match") {
    const rows = Array.isArray(payload.result) ? payload.result : [];
    const events = [];
    for (const row of rows) {
      if (gameIdFrom(row?.game_id) !== DOTA2_GAME_ID) continue;
      const raybetMatchId = matchIdFrom(row);
      if (!raybetMatchId) continue;
      state.dotaMatchIds.add(raybetMatchId);
      events.push({
        eventType: "match_list",
        gameId: DOTA2_GAME_ID,
        raybetMatchId,
        sourcePath,
        payload: { code: scalar(payload.code) ? payload.code : undefined, result: [extractMatchRow(row)] },
      });
    }
    return { events, ignoredReason: events.length ? null : "non_dota" };
  }

  const result = payload.result;
  const identityValues = [
    candidate.raybetMatchId,
    queryMatchIdFrom(candidate.sourceUrl ?? ""),
    matchIdFrom(result),
    matchIdFrom(payload),
  ].filter((value) => value !== null && value !== undefined && value !== "").map(String);
  const identitySet = new Set(identityValues);
  if (identitySet.size > 1) return { events: [], ignoredReason: "match_id_mismatch" };
  const raybetMatchId = identityValues[0] ?? null;
  const explicitGameId = gameIdFrom(result?.game_id, payload.game_id);
  const isKnownDota = Boolean(raybetMatchId && state.dotaMatchIds.has(raybetMatchId));

  if (explicitGameId !== null && explicitGameId !== DOTA2_GAME_ID) {
    return { events: [], ignoredReason: "non_dota" };
  }
  if (explicitGameId !== DOTA2_GAME_ID && !isKnownDota) {
    return { events: [], ignoredReason: "untrusted_match" };
  }
  if (!raybetMatchId) return { events: [], ignoredReason: "missing_match_id" };

  const oddsEndpoint = sourcePath.endsWith("/v2/odds") || sourcePath === "/odds";
  const websocketMarket = candidate.transport === "websocket"
    ? extractWebSocketMarketPayload(payload) : null;
  if (oddsEndpoint || websocketMarket) {
    const safePayload = oddsEndpoint ? extractOddsPayload(payload) : websocketMarket;
    if (!safePayload) return { events: [], ignoredReason: "invalid_odds" };
    state.dotaMatchIds.add(raybetMatchId);
    return {
      events: [{
        eventType: Array.isArray(safePayload.result?.odds) ? "odds" : "market_update",
        gameId: DOTA2_GAME_ID,
        raybetMatchId,
        sourcePath,
        payload: safePayload,
      }],
      ignoredReason: null,
    };
  }

  if (candidate.transport === "page_state" && sourcePath === "/manualControlData") {
    const currentIndex = payload.currentIndex;
    const time = payload.time;
    if (!scalar(currentIndex) || !scalar(time)) {
      return { events: [], ignoredReason: "invalid_manual_control" };
    }
    return {
      events: [{
        eventType: "manual_control",
        gameId: DOTA2_GAME_ID,
        raybetMatchId,
        sourcePath,
        payload: { currentIndex, time },
        captureReason: "diagnostic_untrusted",
      }],
      ignoredReason: null,
    };
  }

  return {
    events: [{
      eventType: "unknown",
      gameId: DOTA2_GAME_ID,
      raybetMatchId,
      sourcePath,
      payload: {},
      captureReason: "unknown_structure",
    }],
    ignoredReason: null,
  };
}

  Object.assign(api, {
    sourcePathFrom,
    queryMatchIdFrom,
    createClassificationState,
    extractMatchRow,
    extractOddsPayload,
    extractWebSocketMarketPayload,
    classifyCandidate,
  });
})(globalThis);
