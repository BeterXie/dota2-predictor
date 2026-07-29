import { useCallback, useSyncExternalStore } from "react";

const listeners = new Set<() => void>();
const noopSubscribe = () => () => undefined;

let currentTime = Date.now();
let timer: number | null = null;

function tick(): void {
  currentTime = Date.now();
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (listeners.size === 1) {
    currentTime = Date.now();
    timer = window.setInterval(tick, 1_000);
  }

  return () => {
    listeners.delete(listener);
    if (!listeners.size && timer != null) {
      window.clearInterval(timer);
      timer = null;
    }
  };
}

function getSnapshot(): number {
  return currentTime;
}

export function useLiveClock(nowOverride?: number): number {
  const getOverride = useCallback(() => nowOverride ?? 0, [nowOverride]);
  return useSyncExternalStore(
    nowOverride == null ? subscribe : noopSubscribe,
    nowOverride == null ? getSnapshot : getOverride,
    nowOverride == null ? getSnapshot : getOverride,
  );
}
