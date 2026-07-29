import type { MonitorMatch, ReadinessStatus, VisionPoint } from "./types";

export type MatchAttentionCategory = "review" | "eligible" | "degraded" | "blocked" | "waiting";
export type MatchAttentionFilter = "all" | "action" | "eligible" | "review" | "degraded" | "upcoming";
export type MatchAttentionSort = "priority" | "updated" | "scheduled";

export interface MatchAttentionState {
  category: MatchAttentionCategory;
  label: string;
  detail: string;
  priority: number;
  updatedAt: string;
}

const readinessSources: Array<keyof MonitorMatch["readiness"]> = [
  "odds",
  "mapping",
  "vision",
  "model",
  "strategy",
];
const sourceLabels: Record<keyof MonitorMatch["readiness"], string> = {
  odds: "赔率",
  mapping: "映射",
  vision: "视觉证据",
  model: "模型",
  strategy: "策略",
};
const readinessLabels: Record<ReadinessStatus, string> = {
  ready: "就绪",
  delayed: "延迟",
  stale: "过期",
  missing: "缺失",
  invalid: "无效",
  unconfirmed: "未确认",
  degraded: "降级",
  unhealthy: "异常",
  stopped: "停止",
};
const degradedStatuses = new Set<ReadinessStatus>([
  "delayed",
  "stale",
  "missing",
  "unconfirmed",
  "degraded",
  "unhealthy",
  "stopped",
]);

export function getTrustedVision(match: MonitorMatch): VisionPoint | null {
  const vision = match.latest_vision;
  const status = match.readiness.vision.status;
  return vision?.confirmed === 1 && (status === "ready" || status === "delayed")
    ? vision
    : null;
}

export function getMatchAttentionState(match: MonitorMatch): MatchAttentionState {
  const decision = match.latest_decision;
  const direction = decision?.underdog_side === "team_one"
    ? match.team_one
    : decision?.underdog_side === "team_two" ? match.team_two : null;
  const reason = decision?.reason.toLocaleLowerCase("en-US") || "";
  const invalidSource = readinessSources.find(
    (source) => match.readiness[source].status === "invalid",
  );
  const updatedAt = latestMatchUpdate(match);

  if (reason.includes("invalid") || reason.includes("mismatch") || invalidSource) {
    return {
      category: "review",
      label: "证据需复核",
      detail: direction
        ? `涉及 ${direction}`
        : invalidSource ? `${sourceLabels[invalidSource]}证据无效` : "策略证据无效或版本不匹配",
      priority: 0,
      updatedAt,
    };
  }

  if (match.lifecycle === "upcoming") {
    return {
      category: "waiting",
      label: "待开赛",
      detail: "尚未形成策略结论",
      priority: 5,
      updatedAt,
    };
  }

  if (decision?.eligible === 1) {
    return {
      category: "eligible",
      label: "策略合格",
      detail: direction ? `关注 ${direction}` : "纸面候选已通过",
      priority: 1,
      updatedAt,
    };
  }

  const degradedSource = readinessSources.find(
    (source) => degradedStatuses.has(match.readiness[source].status),
  );
  if (match.lifecycle === "degraded" || (match.lifecycle === "live" && degradedSource)) {
    const status = degradedSource ? match.readiness[degradedSource].status : null;
    return {
      category: "degraded",
      label: "数据降级",
      detail: degradedSource && status
        ? `${sourceLabels[degradedSource]}${readinessLabels[status]}`
        : "赛事数据处于降级状态",
      priority: 2,
      updatedAt,
    };
  }

  if (decision) {
    return {
      category: "blocked",
      label: "策略拒绝",
      detail: direction ? `已拒绝 ${direction}` : "未达到策略条件",
      priority: 3,
      updatedAt,
    };
  }

  if (match.lifecycle === "ended") {
    return {
      category: "waiting",
      label: "比赛已结束",
      detail: "查看历史赔率与策略记录",
      priority: 6,
      updatedAt,
    };
  }

  return {
    category: "waiting",
    label: "等待判断",
    detail: "等待下一次可信输入",
    priority: 4,
    updatedAt,
  };
}

export function matchesAttentionFilter(
  match: MonitorMatch,
  filter: MatchAttentionFilter,
): boolean {
  if (filter === "all") return true;
  if (filter === "upcoming") return match.lifecycle === "upcoming";
  const category = getMatchAttentionState(match).category;
  if (filter === "action") {
    return category === "review" || category === "eligible" || category === "degraded";
  }
  return category === filter;
}

export function sortMatchesByAttention(
  matches: MonitorMatch[],
  sort: MatchAttentionSort,
): MonitorMatch[] {
  return [...matches].sort((left, right) => {
    const leftState = getMatchAttentionState(left);
    const rightState = getMatchAttentionState(right);
    if (sort === "priority") {
      const priorityOrder = leftState.priority - rightState.priority;
      if (priorityOrder) return priorityOrder;
      const updateOrder = timestamp(rightState.updatedAt) - timestamp(leftState.updatedAt);
      if (updateOrder) return updateOrder;
    } else if (sort === "updated") {
      const updateOrder = timestamp(rightState.updatedAt) - timestamp(leftState.updatedAt);
      if (updateOrder) return updateOrder;
    } else {
      const scheduledOrder = scheduledTimestamp(left.scheduled_at)
        - scheduledTimestamp(right.scheduled_at);
      if (scheduledOrder) return scheduledOrder;
    }
    return compareIds(left.raybet_match_id, right.raybet_match_id);
  });
}

function latestMatchUpdate(match: MonitorMatch): string {
  const candidates = [
    match.updated_at,
    match.latest_odds_activity_at,
    match.winner?.observed_at,
    match.latest_vision?.observed_at,
    match.latest_vision?.captured_at,
    match.latest_decision?.observed_at,
    match.latest_decision?.decided_at,
  ].filter((value): value is string => Boolean(value));
  return candidates.reduce((latest, candidate) => (
    timestamp(candidate) > timestamp(latest) ? candidate : latest
  ), candidates[0] || "");
}

function timestamp(value: string | null | undefined): number {
  if (!value) return Number.NEGATIVE_INFINITY;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY;
}

function scheduledTimestamp(value: string | null | undefined): number {
  const parsed = timestamp(value);
  return parsed === Number.NEGATIVE_INFINITY ? Number.POSITIVE_INFINITY : parsed;
}

function compareIds(left: string, right: string): number {
  if (left === right) return 0;
  return left < right ? -1 : 1;
}
