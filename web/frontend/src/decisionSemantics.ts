import type { StrategyDecision } from "./types";

const REVIEW_REASON_TOKENS = new Set([
  "invalid",
  "invalidated",
  "mismatch",
  "review",
]);

export function decisionReasonRequiresReview(reason: string): boolean {
  return reason
    .toLocaleLowerCase("en-US")
    .split(/[^a-z0-9]+/)
    .some((token) => REVIEW_REASON_TOKENS.has(token));
}

export function orderDecisionsChronologically(
  decisions: readonly StrategyDecision[],
): StrategyDecision[] {
  return decisions
    .map((decision, index) => ({ decision, index }))
    .sort((left, right) => {
      const timeOrder = decisionTimestamp(left.decision) - decisionTimestamp(right.decision);
      if (timeOrder) return timeOrder;
      const keyOrder = compareText(
        left.decision.decision_key || "",
        right.decision.decision_key || "",
      );
      return keyOrder || left.index - right.index;
    })
    .map(({ decision }) => decision);
}

export function selectLatestDecision(
  decisions: readonly StrategyDecision[],
): StrategyDecision | null {
  const ordered = orderDecisionsChronologically(decisions);
  return ordered[ordered.length - 1] || null;
}

function decisionTimestamp(decision: StrategyDecision): number {
  const value = Date.parse(decision.decided_at);
  return Number.isFinite(value) ? value : Number.NEGATIVE_INFINITY;
}

function compareText(left: string, right: string): number {
  if (left === right) return 0;
  return left < right ? -1 : 1;
}
