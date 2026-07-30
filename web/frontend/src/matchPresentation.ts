import { decisionReasonRequiresReview } from "./decisionSemantics";
import type { MonitorMatch, ReadinessStatus, VisionPoint } from "./types";

export type MatchDecisionAttention = "eligible" | "blocked" | "waiting" | "review";
export type MatchHealthAttention = "healthy" | "delayed" | "invalid";
export type MatchAttentionFilter = "all" | "action" | "eligible" | "review" | "degraded" | "upcoming";
export type MatchAttentionSort = "priority" | "updated" | "scheduled";

export interface MatchAttentionState {
  decision: MatchDecisionAttention;
  health: MatchHealthAttention;
  primaryLabel: string;
  primaryDetail: string;
  healthLabel: string | null;
  actionable: boolean;
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
const healthRules: Record<keyof MonitorMatch["readiness"], Set<ReadinessStatus>> = {
  odds: new Set(["delayed", "stale", "missing", "degraded", "unhealthy", "stopped"]),
  mapping: new Set(["missing", "degraded", "unhealthy", "stopped"]),
  vision: new Set(["delayed", "stale", "degraded", "unhealthy", "stopped"]),
  model: new Set(["stale", "degraded", "unhealthy", "stopped"]),
  strategy: new Set(["degraded", "unhealthy", "stopped"]),
};

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
  const updatedAt = latestMatchUpdate(match);
  const decisionState = match.lifecycle === "upcoming"
    ? {
        decision: "waiting" as const,
        primaryLabel: "待开赛",
        primaryDetail: "尚未形成策略结论",
      }
    : decision && decisionReasonRequiresReview(decision.reason)
      ? {
          decision: "review" as const,
          primaryLabel: "证据需复核",
          primaryDetail: direction ? `涉及 ${direction}` : "策略证据无效或版本不匹配",
        }
      : decision?.eligible === 1
        ? {
            decision: "eligible" as const,
            primaryLabel: "策略合格",
            primaryDetail: direction ? `关注 ${direction}` : "纸面候选已通过",
          }
        : decision
          ? {
              decision: "blocked" as const,
              primaryLabel: "策略拒绝",
              primaryDetail: direction ? `已拒绝 ${direction}` : "未达到策略条件",
            }
          : match.lifecycle === "ended"
            ? {
                decision: "waiting" as const,
                primaryLabel: "比赛已结束",
                primaryDetail: "查看历史赔率与策略记录",
              }
            : {
                decision: "waiting" as const,
                primaryLabel: "等待判断",
                primaryDetail: "等待下一次可信输入",
              };
  const healthState = matchHealthState(match);
  const inactive = match.lifecycle === "upcoming" || match.lifecycle === "ended";
  const actionable = !inactive && (
    decisionState.decision === "eligible"
    || decisionState.decision === "review"
    || healthState.health !== "healthy"
  );
  const priority = match.lifecycle === "upcoming"
    ? 5
    : match.lifecycle === "ended" ? 6
      : decisionState.decision === "review" || healthState.health === "invalid" ? 0
        : decisionState.decision === "eligible" ? 1
          : healthState.health === "delayed" ? 2
            : decisionState.decision === "blocked" ? 3 : 4;

  return {
    ...decisionState,
    ...healthState,
    actionable,
    priority,
    updatedAt,
  };
}

export function matchesAttentionFilter(
  match: MonitorMatch,
  filter: MatchAttentionFilter,
): boolean {
  if (filter === "all") return true;
  if (filter === "upcoming") return match.lifecycle === "upcoming";
  const state = getMatchAttentionState(match);
  if (filter === "action") return state.actionable;
  if (filter === "eligible") return state.decision === "eligible";
  if (filter === "review") return state.decision === "review" || state.health === "invalid";
  return state.health !== "healthy";
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

function matchHealthState(match: MonitorMatch): Pick<
  MatchAttentionState,
  "health" | "healthLabel"
> {
  if (match.lifecycle === "upcoming" || match.lifecycle === "ended") {
    return { health: "healthy", healthLabel: null };
  }
  const invalidSource = readinessSources.find(
    (source) => match.readiness[source].status === "invalid",
  );
  if (invalidSource) {
    return {
      health: "invalid",
      healthLabel: `${sourceLabels[invalidSource]}${readinessLabels.invalid}`,
    };
  }
  const delayedSource = readinessSources.find((source) => (
    healthRules[source].has(match.readiness[source].status)
  ));
  if (delayedSource) {
    const status = match.readiness[delayedSource].status;
    return {
      health: "delayed",
      healthLabel: `${sourceLabels[delayedSource]}${readinessLabels[status]}`,
    };
  }
  const missingOutputSource = match.latest_decision
    ? (["model", "strategy"] as const).find(
        (source) => match.readiness[source].status === "missing",
      )
    : null;
  if (missingOutputSource) {
    return {
      health: "delayed",
      healthLabel: `${sourceLabels[missingOutputSource]}${readinessLabels.missing}`,
    };
  }
  if (match.lifecycle === "degraded") {
    return { health: "delayed", healthLabel: "赛事数据降级" };
  }
  return { health: "healthy", healthLabel: null };
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
