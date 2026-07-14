import test from "node:test";
import assert from "node:assert/strict";

const {fetchCompanionStatus, sendEventBatch, signedHeaders} = await import("../src/companion-client.js");

test("HMAC headers are deterministic for a fixed vector", async () => {
  const secret = Buffer.alloc(32, 7).toString("base64url");
  const headers = await signedHeaders({
    secret,
    method: "POST",
    path: "/v1/events",
    body: "[]",
    now: () => 1234567890,
    nonce: "ab".repeat(16),
  });
  assert.equal(headers["X-Dota-Timestamp"], "1234567890");
  assert.equal(headers["X-Dota-Nonce"], "ab".repeat(16));
  assert.match(headers["X-Dota-Signature"], /^[0-9a-f]{64}$/);
  assert.equal(headers["X-Dota-Signature"], "2e6726ee8d1669754abc95a396d9ff4c5a82c93a95016dc33a869038148b5864");
});

test("event request signs the exact body sent", async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({url, init});
    return {ok: true, status: 200, json: async () => ({accepted: ["a"]})};
  };
  const secret = Buffer.alloc(32, 3).toString("base64url");
  await sendEventBatch([{event_id: "a"}], secret, fetchImpl, {
    now: () => 5,
    nonce: "01".repeat(16),
  });
  assert.equal(calls[0].url, "http://127.0.0.1:8765/v1/events");
  assert.equal(calls[0].init.body, '[{"event_id":"a"}]');
  assert.match(calls[0].init.headers["X-Dota-Signature"], /^[0-9a-f]{64}$/);
});

test("status request uses signed POST so Edge includes the extension origin", async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({url, init});
    return {ok: true, status: 200, json: async () => ({protocol_version: 1})};
  };
  const secret = Buffer.alloc(32, 5).toString("base64url");
  await fetchCompanionStatus(secret, fetchImpl, {
    now: () => 7,
    nonce: "02".repeat(16),
  });
  assert.equal(calls[0].url, "http://127.0.0.1:8765/v1/status");
  assert.equal(calls[0].init.method, "POST");
  assert.match(calls[0].init.headers["X-Dota-Signature"], /^[0-9a-f]{64}$/);
});
