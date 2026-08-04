import { describe, expect, it } from "vitest";

import {
  getMatchAttentionState,
  matchesAttentionFilter,
  sortMatchesByAttention,
} from "./matchPresentation";
import type { MonitorMatch, StrategyDecision } from "./types";

const ready = {
  odds: { status: "ready" as const },
  mapping: { status: "ready" as const },
  vision: { status: "ready" as const },
  model: { status: "ready" as const },
  strategy: { status: "ready" as const },
};

function decision(overrides: Partial<StrategyDecision> = {}): StrategyDecision {
  return {
    decided_at: "2026-07-30T12:00:00+00:00",
    map_number: 1,
    underdog_side: "team_one",
    market_probability: 0.4,
    model_probability: 0.52,
    edge: 0.12,
    eligible: 0,
    reason: "edge_below_threshold",
    strategy_version: "test-v1",
    ...overrides,
  };
}

function match(
  raybetMatchId: string,
  overrides: Partial<MonitorMatch> = {},
): MonitorMatch {
  return {
    raybet_match_id: raybetMatchId,
    tournament: "Test event",
    team_one: `${raybetMatchId} one`,
    team_two: `${raybetMatchId} two`,
    scheduled_at: "2026-07-30T13:00:00+00:00",
    best_of: 3,
    provider_status: "2",
    updated_at: "2026-07-30T12:00:00+00:00",
    lifecycle: "live",
    history_eligible: false,
    winner: null,
    latest_vision: null,
    latest_decision: null,
    readiness: ready,
    ...overrides,
  };
}

describe("match attention presentation", () => {
  it("maps live matches into the stable attention priority", () => {
    const matches = [
      match("review", { latest_decision: decision({ eligible: 1, reason: "rosh_profile_mismatch" }) }),
      match("eligible", {
        latest_decision: decision({ eligible: 1, reason: "eligible" }),
        readiness: { ...ready, odds: { status: "stale" } },
      }),
      match("degraded", {
        lifecycle: "degraded",
      }),
      match("blocked", { latest_decision: decision() }),
      match("waiting"),
      match("upcoming", { lifecycle: "upcoming" }),
    ];

    expect(matches.map((item) => getMatchAttentionState(item).decision)).toEqual([
      "review",
      "eligible",
      "waiting",
      "blocked",
      "waiting",
      "waiting",
    ]);
    expect(matches.map((item) => getMatchAttentionState(item).health)).toEqual([
      "healthy",
      "delayed",
      "delayed",
      "healthy",
      "healthy",
      "healthy",
    ]);
    expect(matches.map((item) => getMatchAttentionState(item).priority)).toEqual([
      0,
      1,
      2,
      3,
      4,
      5,
    ]);
    expect(getMatchAttentionState(matches[1])).toMatchObject({
      primaryLabel: "策略合格",
      primaryDetail: "关注 eligible one",
      healthLabel: "赔率过期",
      actionable: true,
    });
    expect(getMatchAttentionState(match("manual-review", {
      latest_decision: decision({ eligible: 0, reason: "manual_review_required" }),
    })).decision).toBe("review");
  });

  it("treats first model or strategy output as waiting instead of degraded", () => {
    const firstJudgmentPending = match("first-judgment", {
      readiness: {
        ...ready,
        model: { status: "missing" },
        strategy: { status: "missing" },
      },
    });
    const oddsMissing = match("odds-missing", {
      readiness: { ...ready, odds: { status: "missing" } },
    });
    const existingDecisionMissingModel = match("missing-after-decision", {
      latest_decision: decision({ eligible: 1, reason: "eligible" }),
      readiness: { ...ready, model: { status: "missing" } },
    });

    expect(getMatchAttentionState(firstJudgmentPending)).toMatchObject({
      decision: "waiting",
      health: "healthy",
      primaryLabel: "等待判断",
      healthLabel: null,
      actionable: false,
    });
    expect(getMatchAttentionState(oddsMissing)).toMatchObject({
      decision: "waiting",
      health: "delayed",
      healthLabel: "赔率缺失",
      actionable: true,
    });
    expect(getMatchAttentionState(existingDecisionMissingModel)).toMatchObject({
      decision: "eligible",
      health: "delayed",
      healthLabel: "模型缺失",
      actionable: true,
    });
  });

  it("defines the actionable filter without inventing unread or alert state", () => {
    const review = match("review", {
      latest_decision: decision({ reason: "vision_payload_invalid" }),
    });
    const eligible = match("eligible", {
      latest_decision: decision({ eligible: 1, reason: "eligible" }),
    });
    const degraded = match("degraded", { lifecycle: "degraded" });
    const blocked = match("blocked", { latest_decision: decision() });

    expect([review, eligible, degraded, blocked].filter(
      (item) => matchesAttentionFilter(item, "action"),
    )).toEqual([review, eligible, degraded]);
  });

  it("sorts by priority, then latest update, then match id without mutating input", () => {
    const laterEligible = match("eligible-b", {
      latest_decision: decision({
        decided_at: "2026-07-30T12:02:00+00:00",
        eligible: 1,
        reason: "eligible",
      }),
    });
    const tiedEligible = match("eligible-a", {
      latest_decision: decision({
        decided_at: "2026-07-30T12:02:00+00:00",
        eligible: 1,
        reason: "eligible",
      }),
    });
    const review = match("review", {
      latest_decision: decision({ reason: "evidence_invalid" }),
    });
    const original = [laterEligible, review, tiedEligible];

    expect(sortMatchesByAttention(original, "priority").map(
      (item) => item.raybet_match_id,
    )).toEqual(["review", "eligible-a", "eligible-b"]);
    expect(original.map((item) => item.raybet_match_id)).toEqual([
      "eligible-b",
      "review",
      "eligible-a",
    ]);
  });

  it("supports latest-update and scheduled-time ordering", () => {
    const olderStart = match("older-start", {
      scheduled_at: "2026-07-30T12:30:00+00:00",
      updated_at: "2026-07-30T12:01:00+00:00",
    });
    const newerUpdate = match("newer-update", {
      scheduled_at: "2026-07-30T14:00:00+00:00",
      updated_at: "2026-07-30T12:03:00+00:00",
    });
    const noStart = match("no-start", {
      scheduled_at: null,
      updated_at: "2026-07-30T12:02:00+00:00",
    });

    expect(sortMatchesByAttention(
      [olderStart, newerUpdate, noStart],
      "updated",
    ).map((item) => item.raybet_match_id)).toEqual([
      "newer-update",
      "no-start",
      "older-start",
    ]);
    expect(sortMatchesByAttention(
      [newerUpdate, noStart, olderStart],
      "scheduled",
    ).map((item) => item.raybet_match_id)).toEqual([
      "older-start",
      "newer-update",
      "no-start",
    ]);
  });

  it("sorts archived matches by valid evidence instead of later transport metadata", () => {
    const olderEvidence = match("older-evidence", {
      lifecycle: "ended",
      history_eligible: true,
      updated_at: "2026-07-30T12:10:00+00:00",
      latest_odds_activity_at: "2026-07-30T12:10:00+00:00",
      winner: {
        complete: true,
        observed_at: "2026-07-30T10:00:00+00:00",
      },
    });
    const newerEvidence = match("newer-evidence", {
      lifecycle: "ended",
      history_eligible: true,
      updated_at: "2026-07-30T12:05:00+00:00",
      latest_odds_activity_at: "2026-07-30T12:05:00+00:00",
      winner: {
        complete: true,
        observed_at: "2026-07-30T11:00:00+00:00",
      },
    });

    expect(sortMatchesByAttention(
      [olderEvidence, newerEvidence],
      "updated",
    ).map((item) => item.raybet_match_id)).toEqual([
      "newer-evidence",
      "older-evidence",
    ]);
    expect(getMatchAttentionState(olderEvidence).updatedAt)
      .toBe("2026-07-30T10:00:00+00:00");
  });
});
