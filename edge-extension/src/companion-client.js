const encoder = new TextEncoder();
const BASE_URL = "http://127.0.0.1:8765";
const EXTENSION_VERSION = "0.1.0";

export class CompanionError extends Error {
  constructor(message, status = 0, code = "companion_error") {
    super(message);
    this.name = "CompanionError";
    this.status = status;
    this.code = code;
  }
}

function bytesToHex(bytes) {
  return [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
}

function base64UrlToBytes(value) {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
  const binary = atob(padded);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

export async function sha256Hex(value) {
  const bytes = typeof value === "string" ? encoder.encode(value) : value;
  return bytesToHex(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)));
}

export async function signedHeaders({secret, method, path, body = "", now = Date.now, nonce}) {
  const timestamp = String(now());
  const requestNonce = nonce || bytesToHex(crypto.getRandomValues(new Uint8Array(16)));
  const bodyHash = await sha256Hex(body);
  const message = `${timestamp}\n${requestNonce}\n${method.toUpperCase()}\n${path}\n${bodyHash}`;
  const key = await crypto.subtle.importKey(
    "raw",
    base64UrlToBytes(secret),
    {name: "HMAC", hash: "SHA-256"},
    false,
    ["sign"],
  );
  const signature = bytesToHex(
    new Uint8Array(await crypto.subtle.sign("HMAC", key, encoder.encode(message))),
  );
  return {
    "X-Dota-Extension-Version": EXTENSION_VERSION,
    "X-Dota-Timestamp": timestamp,
    "X-Dota-Nonce": requestNonce,
    "X-Dota-Signature": signature,
  };
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

export async function pairCompanion(code, fetchImpl = fetch) {
  const body = JSON.stringify({code, extension_version: EXTENSION_VERSION});
  const response = await fetchImpl(`${BASE_URL}/v1/pair`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body,
  });
  return parseResponse(response);
}

export async function sendEventBatch(events, secret, fetchImpl = fetch, options = {}) {
  const path = "/v1/events";
  const body = JSON.stringify(events);
  const auth = await signedHeaders({secret, method: "POST", path, body, ...options});
  const response = await fetchImpl(`${BASE_URL}${path}`, {
    method: "POST",
    headers: {"Content-Type": "application/json", ...auth},
    body,
  });
  return parseResponse(response);
}

export async function fetchCompanionStatus(secret, fetchImpl = fetch, options = {}) {
  const path = "/v1/status";
  const auth = await signedHeaders({secret, method: "POST", path, body: "", ...options});
  const response = await fetchImpl(`${BASE_URL}${path}`, {method: "POST", headers: auth});
  return parseResponse(response);
}

export const companionConstants = Object.freeze({BASE_URL, EXTENSION_VERSION});
