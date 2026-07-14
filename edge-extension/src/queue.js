const encoder = new TextEncoder();

export const QUEUE_LIMITS = Object.freeze({
  maxEvents: 1000,
  maxBytes: 5 * 1024 * 1024,
  maxBatchEvents: 50,
  maxBatchBytes: 1024 * 1024,
});

export function jsonBytes(value) {
  return encoder.encode(JSON.stringify(value)).byteLength;
}
export function queueBytes(events) {
  return events.reduce((total, event) => total + jsonBytes(event), 0);
}

export function enqueueBounded(events, event, limits = QUEUE_LIMITS) {
  const next = events.filter((item) => item.event_id !== event.event_id);
  next.push(event);
  let bytes = queueBytes(next);
  let dropped = 0;
  while (next.length > limits.maxEvents || bytes > limits.maxBytes) {
    const removed = next.shift();
    bytes -= jsonBytes(removed);
    dropped += 1;
  }
  return {events: next, bytes, dropped};
}

export function makeBatch(events, limits = QUEUE_LIMITS) {
  const batch = [];
  let bytes = 2;
  for (const event of events) {
    const itemBytes = jsonBytes(event) + (batch.length ? 1 : 0);
    if (batch.length >= limits.maxBatchEvents || bytes + itemBytes > limits.maxBatchBytes) {
      break;
    }
    batch.push(event);
    bytes += itemBytes;
  }
  return {events: batch, bytes};
}

export function acknowledge(events, eventIds) {
  const acknowledged = new Set(eventIds);
  return events.filter((event) => !acknowledged.has(event.event_id));
}

export function retryDelayMs(attempt, random = Math.random) {
  const base = Math.min(60_000, 1000 * 2 ** Math.max(0, attempt));
  return Math.max(1000, Math.round(base * (0.8 + random() * 0.4)));
}
