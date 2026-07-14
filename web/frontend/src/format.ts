import type { Lifecycle, ReadinessStatus } from "./types";

export const lifecycleLabel: Record<Lifecycle, string> = {
  live: "滚球确认",
  degraded: "数据降级",
  upcoming: "即将开始",
  ended: "已结束",
};

export const readinessLabel: Record<ReadinessStatus, string> = {
  ready: "就绪",
  delayed: "延迟",
  stale: "已过期",
  missing: "无数据",
  invalid: "无效",
  unconfirmed: "待确认",
  degraded: "降级",
  unhealthy: "异常",
  stopped: "已停止",
};

export function formatPercent(value: number | null | undefined, digits = 1): string {
  return value == null || !Number.isFinite(value) ? "-" : `${(value * 100).toFixed(digits)}%`;
}

export function formatOdds(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "-" : value.toFixed(2);
}

export function formatClock(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "--:--";
  const minutes = Math.floor(seconds / 60);
  const rest = Math.floor(seconds % 60);
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "无时间";
  const parsed = parseTimestamp(value);
  if (!parsed) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(parsed);
}

export function ageSeconds(value: string | null | undefined, now = Date.now()): number | null {
  if (!value) return null;
  const parsed = parseTimestamp(value);
  return parsed ? Math.max(0, (now - parsed.getTime()) / 1000) : null;
}

export function formatAge(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "未收到";
  if (seconds < 1) return "刚刚";
  if (seconds < 60) return `${Math.floor(seconds)} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  return `${Math.floor(seconds / 3600)} 小时前`;
}

export function parseTimestamp(value: string): Date | null {
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value)
    ? value
    : value.replace(" ", "T") + "+08:00";
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}
