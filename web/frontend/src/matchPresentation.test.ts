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
      match("eligible", { latest_decision: decision({ eligible: 1, reason: "eligible" }) }),
      match("degraded", {
        readiness: { ...ready, odds: { status: "stale" } },
      }),
      match("blocked", { latest_decision: decision() }),
      match("waiting"),
      match("upcoming", { lifecycle: "upcoming" }),
    ];

    expect(matches.map((item) => getMatchAttentionState(item).category)).toEqual([
      "review",
      "eligible",
      "degraded",
      "blocked",
      "waiting",
      "waiting",
    ]);
    expect(matches.map((item) => getMatchAttentionState(item).priority)).toEqual([
      0,
      1,
      2,
      3,
      4,
      5,
    ]);
    expect(getMatchAttentionState(matches[2]).detail).toBe("赔率过期");
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
});
