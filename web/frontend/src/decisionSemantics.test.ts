import { describe, expect, it } from "vitest";

import type { StrategyDecision } from "./types";
import {
  decisionReasonRequiresReview,
  orderDecisionsChronologically,
  selectLatestDecision,
} from "./decisionSemantics";

function decision(overrides: Partial<StrategyDecision> = {}): StrategyDecision {
  return {
    decision_key: "a".repeat(32),
    decided_at: "2026-07-30T12:00:00+00:00",
    map_number: 1,
    underdog_side: "team_one",
    market_probability: 0.36,
    model_probability: 0.42,
    edge: 0.06,
    data_quality: 0.9,
    eligible: 1,
    reason: "eligible",
    strategy_version: "test-v1",
    ...overrides,
  };
}

describe("decision semantics", () => {
  it("selects the chronological latest decision from an unordered payload", () => {
    const older = decision({
      decision_key: "1".repeat(32),
      decided_at: "2026-07-30T11:59:00+00:00",
    });
    const tiedFirst = decision({ decision_key: "2".repeat(32) });
    const tiedSecond = decision({ decision_key: "3".repeat(32) });

    expect(orderDecisionsChronologically([tiedSecond, older, tiedFirst]))
      .toEqual([older, tiedFirst, tiedSecond]);
    expect(selectLatestDecision([tiedFirst, older, tiedSecond])).toBe(tiedSecond);
  });

  it("uses one review classification for invalid, mismatch, and review reasons", () => {
    expect(decisionReasonRequiresReview("evidence_invalid")).toBe(true);
    expect(decisionReasonRequiresReview("rosh_profile_mismatch")).toBe(true);
    expect(decisionReasonRequiresReview("manual_review_required")).toBe(true);
    expect(decisionReasonRequiresReview("vision_invalidated")).toBe(true);
    expect(decisionReasonRequiresReview("edge_below_threshold")).toBe(false);
  });
});
