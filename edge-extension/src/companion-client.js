const BASE_URL = "http://127.0.0.1:8765";
const EXTENSION_VERSION = "0.1.0";
const PROTOCOL_VERSION = 1;
const STATUS_TIMEOUT_MS = 3000;
const EVENT_TIMEOUT_MS = 10_000;

export class CompanionError extends Error {
  constructor(message, status = 0, code = "companion_error") {
    super(message);
    this.name = "CompanionError";
    this.status = status;
    this.code = code;
  }
}

async function parseResponse(response) {
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  if (!response.ok) {
    throw new CompanionError(
      payload.detail || payload.message || `Companion returned HTTP ${response.status}`,
      response.status,
      payload.code || "http_error",
    );
  }
  return payload;
}

function requireProtocolVersion(payload, response) {
  if (payload.protocol_version !== PROTOCOL_VERSION) {
    throw new CompanionError(
      `Unsupported companion protocol version: ${String(payload.protocol_version)}`,
      response.status,
      "unsupported_protocol_version",
    );
  }
  return payload;
}

function requireEventAcknowledgements(payload, response, events) {
  const expectedIds = new Set(events.map((event) => event?.event_id));
  const seenIds = new Set();
  if (!Array.isArray(payload.results) || payload.results.length !== events.length
      || expectedIds.size !== events.length) {
    throw new CompanionError(
      "Companion returned an invalid acknowledgement set",
      response.status,
      "invalid_protocol_response",
    );
  }
  for (const result of payload.results) {
    const eventId = result?.event_id;
    if (typeof eventId !== "string"
        || !expectedIds.has(eventId)
        || seenIds.has(eventId)
        || !["accepted", "duplicate", "rejected"].includes(result?.status)) {
      throw new CompanionError(
        "Companion returned an invalid event acknowledgement",
        response.status,
        "invalid_protocol_response",
      );
    }
    seenIds.add(eventId);
  }
  return payload;
}

async function fetchWithTimeout(fetchImpl, url, init, timeoutMs) {
  const controller = new AbortController();
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new CompanionError("Companion request timed out", 0, "companion_timeout"));
    }, timeoutMs);
  });
  try {
    return await Promise.race([
      fetchImpl(url, {...init, signal: controller.signal}),
      timeout,
    ]);
  } finally {
    clearTimeout(timer);
  }
}

export async function sendEventBatch(
  events,
  fetchImpl = fetch,
  timeoutMs = EVENT_TIMEOUT_MS,
) {
  const body = JSON.stringify(events);
  const response = await fetchWithTimeout(fetchImpl, `${BASE_URL}/v1/events`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Dota-Extension-Version": EXTENSION_VERSION,
    },
    body,
  }, timeoutMs);
  const payload = requireProtocolVersion(await parseResponse(response), response);
  return requireEventAcknowledgements(payload, response, events);
}

export async function fetchCompanionStatus(fetchImpl = fetch, timeoutMs = STATUS_TIMEOUT_MS) {
  const response = await fetchWithTimeout(fetchImpl, `${BASE_URL}/v1/status`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Dota-Extension-Version": EXTENSION_VERSION,
    },
    body: "{}",
  }, timeoutMs);
  return requireProtocolVersion(await parseResponse(response), response);
}

export const companionConstants = Object.freeze({
  BASE_URL,
  EXTENSION_VERSION,
  PROTOCOL_VERSION,
  STATUS_TIMEOUT_MS,
  EVENT_TIMEOUT_MS,
});
