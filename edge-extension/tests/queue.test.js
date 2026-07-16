import test from "node:test";
import assert from "node:assert/strict";

import {acknowledge, enqueueBounded, makeBatch, retryDelayMs} from "../src/queue.js";

function event(id, payload = {}) {
  return {event_id: id, event_type: "odds", captured_at_utc: "2026-07-13T00:00:00.000Z", payload};
}

test("queue deduplicates and drops oldest at exact count bound", () => {
  let items = [];
  for (let index = 0; index < 4; index += 1) {
    items = enqueueBounded(items, event(String(index)), {maxEvents: 3, maxBytes: 10000}).events;
  }
  assert.deepEqual(items.map((item) => item.event_id), ["1", "2", "3"]);
  items = enqueueBounded(items, event("2", {new: true}), {maxEvents: 3, maxBytes: 10000}).events;
  assert.deepEqual(items.map((item) => item.event_id), ["1", "3", "2"]);
});
test("batch and acknowledgement preserve unacknowledged order", () => {
  const items = [event("a"), event("b"), event("c")];
  assert.deepEqual(makeBatch(items, {maxBatchEvents: 2, maxBatchBytes: 10000}).events.map((x) => x.event_id), ["a", "b"]);
  assert.deepEqual(acknowledge(items, ["a", "c"]).map((x) => x.event_id), ["b"]);
});

test("retry is bounded", () => {
  assert.equal(retryDelayMs(0, () => 0.5), 1000);
  assert.equal(retryDelayMs(20, () => 0.5), 60000);
  assert.equal(retryDelayMs(20, () => 1), 60000);
});
