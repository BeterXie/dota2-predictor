import { describe, expect, it } from "vitest";

import type { StrategyDecision } from "../../types";
import { compareDecisions, latestDecisionDelta } from "./decisionDelta";

function decision(overrides: Partial<StrategyDecision> = {}): StrategyDecision {
  return {
    decision_key: "a".repeat(32),
    decided_at: "2026-07-30T12:00:00+00:00",
    map_number: 1,
    underdog_side: "team_one",
    market_probability: 0.362,
    model_probability: 0.428,
    edge: 0.066,
    data_quality: 0.92,
    eligible: 1,
    reason: "eligible",
    strategy_version: "test-v1",
    inputs: {
      comeback_entry: { rosh_underdog_probability: 0.55 },
    },
    ...overrides,
  };
}

describe("decision delta", () => {
  it("explains a same-map eligible-to-blocked transition", () => {
    const previous = decision();
    const current = decision({
      decision_key: "b".repeat(32),
      decided_at: "2026-07-30T12:01:00+00:00",
      market_probability: 0.38,
      model_probability: 0.391,
      edge: 0.011,
      data_quality: 0.88,
      eligible: 0,
      reason: "edge_below_threshold",
      inputs: {
        comeback_entry: { rosh_underdog_probability: 0.48 },
      },
    });

    const delta = compareDecisions(previous, current);
    expect(delta).toMatchObject({
      previousVerdict: "eligible",
      currentVerdict: "blocked",
      verdictChanged: true,
      directionChanged: false,
      reasonChanged: true,
      previousRoshDirection: "supports",
      currentRoshDirection: "opposes",
      roshDirectionChanged: true,
      summary: "市场概率上升 1.8 个百分点，Rosh 方向转为不支持弱势方，最终策略由合格变为拒绝。",
    });
    expect(delta?.marketProbabilityDelta).toBeCloseTo(0.018);
    expect(delta?.modelProbabilityDelta).toBeCloseTo(-0.037);
    expect(delta?.edgeDelta).toBeCloseTo(-0.055);
    expect(delta?.dataQualityDelta).toBeCloseTo(-0.04);
  });

  it("does not compare probabilities when the modeled side changes", () => {
    const delta = compareDecisions(decision(), decision({
      decision_key: "b".repeat(32),
      decided_at: "2026-07-30T12:01:00+00:00",
      underdog_side: "team_two",
      strategy_version: "test-v2",
    }));

    expect(delta).toMatchObject({
      directionChanged: true,
      versionChanged: true,
      marketProbabilityDelta: null,
      modelProbabilityDelta: null,
      edgeDelta: null,
      summary: "策略版本已切换，策略关注方向发生变化，策略结论保持合格。",
    });
  });

  it("rejects cross-map, duplicate, and invalid-evidence comparisons", () => {
    const previous = decision();
    expect(compareDecisions(previous, decision({ map_number: 2 }))).toBeNull();
    expect(compareDecisions(previous, decision())).toBeNull();
    expect(compareDecisions(previous, decision({
      decision_key: "b".repeat(32),
      reason: "rosh_profile_mismatch",
    }))).toBeNull();
  });

  it("selects the latest two unique valid decisions from the current map", () => {
    const older = decision({ decided_at: "2026-07-30T11:58:00+00:00" });
    const previous = decision({
      decision_key: "b".repeat(32),
      decided_at: "2026-07-30T11:59:00+00:00",
    });
    const current = decision({
      decision_key: "c".repeat(32),
      decided_at: "2026-07-30T12:00:00+00:00",
      eligible: 0,
      reason: "edge_below_threshold",
    });
    const invalidLatest = decision({
      decision_key: "d".repeat(32),
      decided_at: "2026-07-30T12:02:00+00:00",
      reason: "evidence_invalid",
    });
    const anotherMap = decision({
      decision_key: "e".repeat(32),
      decided_at: "2026-07-30T12:03:00+00:00",
      map_number: 2,
    });

    const delta = latestDecisionDelta([
      older,
      previous,
      { ...previous },
      current,
      invalidLatest,
      anotherMap,
    ], 1);

    expect(delta?.previous.decision_key).toBe(previous.decision_key);
    expect(delta?.current.decision_key).toBe(current.decision_key);
  });
});
