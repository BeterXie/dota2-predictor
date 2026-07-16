import test from "node:test";
import assert from "node:assert/strict";

const {fetchCompanionStatus, sendEventBatch} = await import("../src/companion-client.js");

test("event request sends the exact JSON body with direct-mode headers", async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({url, init});
    return {
      ok: true,
      status: 200,
      json: async () => ({protocol_version: 1, results: []}),
    };
  };
  await sendEventBatch([{event_id: "a"}], fetchImpl);
  assert.equal(calls[0].url, "http://127.0.0.1:8765/v1/events");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.body, '[{"event_id":"a"}]');
  assert.deepEqual(calls[0].init.headers, {
    "Content-Type": "application/json",
    "X-Dota-Extension-Version": "0.1.0",
  });
});

test("status request is a direct JSON POST so Edge includes Origin", async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({url, init});
    return {ok: true, status: 200, json: async () => ({protocol_version: 1})};
  };
  await fetchCompanionStatus(fetchImpl);
  assert.equal(calls[0].url, "http://127.0.0.1:8765/v1/status");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.body, "{}");
  assert.deepEqual(calls[0].init.headers, {
    "Content-Type": "application/json",
    "X-Dota-Extension-Version": "0.1.0",
  });
});

test("status request rejects unsupported companion protocol versions", async () => {
  for (const protocolVersion of [undefined, "1", 2]) {
    const fetchImpl = async () => ({
      ok: true,
      status: 200,
      json: async () => ({protocol_version: protocolVersion}),
    });
    await assert.rejects(
      fetchCompanionStatus(fetchImpl),
      (error) => error.name === "CompanionError"
        && error.status === 200
        && error.code === "unsupported_protocol_version",
    );
  }
});

test("event acknowledgement rejects unsupported companion protocol versions", async () => {
  for (const protocolVersion of [undefined, "1", 2]) {
    const fetchImpl = async () => ({
      ok: true,
      status: 200,
      json: async () => ({protocol_version: protocolVersion, results: []}),
    });
    await assert.rejects(
      sendEventBatch([{event_id: "a"}], fetchImpl),
      (error) => error.name === "CompanionError"
        && error.status === 200
        && error.code === "unsupported_protocol_version",
    );
  }
});
