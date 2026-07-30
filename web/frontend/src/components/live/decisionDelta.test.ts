import { describe, expect, it } from "vitest";

import type { StrategyDecision } from "../../types";
import {
  compareDecisions,
  decisionReasonLabel,
  latestDecisionDelta,
} from "./decisionDelta";

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
      summary: "模型 Edge 从 +6.6% 降至 +1.1%，已低于最终阈值；同时 Rosh 方向转为不支持弱势方，因此最终策略由合格变为拒绝。",
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
      summary: "策略版本由 test-v1 切换至 test-v2，数值不可完全直接对比；同时策略关注方向发生变化，因此策略结论保持合格。",
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

  it("selects the latest two unique valid decisions from an unordered current map", () => {
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
    const delta = latestDecisionDelta([
      current,
      older,
      { ...previous },
      previous,
    ]);

    expect(delta?.previous.decision_key).toBe(previous.decision_key);
    expect(delta?.current.decision_key).toBe(current.decision_key);
  });

  it("does not skip over the latest decision when its evidence needs review", () => {
    const previous = decision({ decided_at: "2026-07-30T11:59:00+00:00" });
    const current = decision({
      decision_key: "b".repeat(32),
      decided_at: "2026-07-30T12:00:00+00:00",
      eligible: 0,
      reason: "edge_below_threshold",
    });
    const invalidLatest = decision({
      decision_key: "c".repeat(32),
      decided_at: "2026-07-30T12:02:00+00:00",
      eligible: 0,
      reason: "evidence_invalid",
    });

    expect(latestDecisionDelta([previous, current, invalidLatest])).toBeNull();
  });

  it("prioritizes data quality when that is the rejection reason", () => {
    const delta = compareDecisions(decision(), decision({
      decision_key: "b".repeat(32),
      decided_at: "2026-07-30T12:01:00+00:00",
      data_quality: 0.7,
      eligible: 0,
      reason: "insufficient_data_quality",
    }));

    expect(delta?.summary).toBe(
      "数据质量从 92.0% 降至 70.0%，未达到策略门槛，因此最终策略由合格变为拒绝。",
    );
  });

  it("explains a conservative probability rejection against the market", () => {
    const delta = compareDecisions(decision(), decision({
      decision_key: "b".repeat(32),
      decided_at: "2026-07-30T12:01:00+00:00",
      market_probability: 0.38,
      model_probability: 0.391,
      edge: 0.011,
      eligible: 0,
      reason: "conservative_probability_not_above_market",
      inputs: {
        conservative_probability: 0.37,
        comeback_entry: { rosh_underdog_probability: 0.55 },
      },
    }));

    expect(delta?.summary).toBe(
      "保守概率 37.0% 未高于市场概率 38.0%；同时模型 Edge 下降 5.5 个百分点，因此最终策略由合格变为拒绝。",
    );
  });

  it("puts the Rosh direction first when it is the rejection reason", () => {
    const delta = compareDecisions(decision(), decision({
      decision_key: "b".repeat(32),
      decided_at: "2026-07-30T12:01:00+00:00",
      eligible: 0,
      reason: "rosh_direction_opposes_underdog",
      inputs: {
        comeback_entry: { rosh_underdog_probability: 0.48 },
      },
    }));

    expect(delta?.summary).toBe(
      "Rosh 方向转为不支持弱势方，因此最终策略由合格变为拒绝。",
    );
  });

  it("does not expose unknown reason codes as primary copy", () => {
    expect(decisionReasonLabel("new_backend_reason_code")).toBe("其他策略条件");
  });
});
