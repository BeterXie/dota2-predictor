import { describe, expect, it } from "vitest";

import type {
  LiveDraftMapping,
  LiveDraftProspectivePrediction,
  MatchDetail,
} from "../../types";
import {
  deriveLiveDraftValueDecision,
  LIVE_DRAFT_VALUE_STRATEGY_VERSION,
} from "./liveDraftValueDecision";


const mapping: LiveDraftMapping = {
  raybet_match_id: "match-1",
  map_number: 1,
  version: 2,
  source: "manual",
  is_locked: true,
  created_by: "operator",
  created_at: "2026-08-10T00:00:00+00:00",
  slots: (["radiant", "dire"] as const).flatMap((side, sideIndex) =>
    Array.from({ length: 5 }, (_, index) => ({
      team_id: sideIndex + 1,
      side,
      position: index + 1,
      hero_id: sideIndex * 5 + index + 1,
      player_id: null,
    })),
  ),
};

const prediction: LiveDraftProspectivePrediction = {
  prediction_hash: "a".repeat(64),
  version: "live-draft-prospective-bridge-v1",
  identity: {
    raybet_match_id: "match-1",
    map_number: 1,
    mapping_version: 2,
    mapping_hash: "b".repeat(64),
  },
  operator_locked_at: "2026-08-10T00:00:00+00:00",
  confirmed_at: "2026-08-10T00:00:01+00:00",
  record_status: "paired",
  p0_probability: 0.6,
  p1_probability: 0.65,
  pure_rosh_score: 1,
  standardized_rosh_score: 0.5,
  rosh_logit_contribution: 0.2,
  missing_reason: null,
  candidate_hash: "c".repeat(64),
  causal_evidence: {
    game_clock_seconds: null,
    vision_frame_timestamp: null,
    draft_state_marker: "draft_complete",
    live_state_input_used: false,
    causal_status: "eligible",
    causal_reason: null,
  },
  created_at: "2026-08-10T00:00:02+00:00",
};

const detail: MatchDetail = {
  raybet_match_id: "match-1",
  tournament: "Event",
  team_one: "Radiant Team",
  team_two: "Dire Team",
  scheduled_at: "2026-08-10T00:00:00+00:00",
  best_of: 3,
  provider_status: "2",
  updated_at: "2026-08-10T00:10:00+00:00",
  lifecycle: "live",
  history_eligible: false,
  current_map_number: 1,
  winner: {
    observed_at: "2026-08-10T00:10:00+00:00",
    period: "map_1",
    complete: true,
    prices: { team_one: 1.82, team_two: 2.08 },
    probabilities: { team_one: 0.55, team_two: 0.45 },
  },
  prematch_winner: null,
  latest_vision: null,
  winner_timeline: [],
  vision: [],
  markets: [],
  draft_mapping: mapping,
  draft_context: {
    status: "ready",
    reason: "mapped",
    source: "mapping",
    teams: [
      { match_side: "team_one", team_id: 1, team_name: "Radiant Team" },
      { match_side: "team_two", team_id: 2, team_name: "Dire Team" },
    ],
  },
};

describe("deriveLiveDraftValueDecision", () => {
  it("produces a versioned candidate only when paired P1 clears the edge gate", () => {
    const decision = deriveLiveDraftValueDecision(detail, mapping, prediction);

    expect(decision).toMatchObject({
      status: "candidate",
      reason: "minimum_edge_met",
      strategyVersion: LIVE_DRAFT_VALUE_STRATEGY_VERSION,
      selectedMatchSide: "team_one",
      selectedTeamName: "Radiant Team",
      modelProbability: 0.65,
      marketProbability: 0.55,
      price: 1.82,
      predictionHash: prediction.prediction_hash,
    });
    expect(decision.edge).toBeCloseTo(0.1);
  });

  it("returns no-bet when the best model edge is below eight percent", () => {
    const decision = deriveLiveDraftValueDecision(detail, mapping, {
      ...prediction,
      p1_probability: 0.6,
    });

    expect(decision.status).toBe("no_bet");
    expect(decision.reason).toBe("edge_below_threshold");
    expect(decision.edge).toBeCloseTo(0.05);
  });

  it("fails closed for P0-only predictions and non-current-map odds", () => {
    expect(deriveLiveDraftValueDecision(detail, mapping, {
      ...prediction,
      record_status: "p0_only",
      p1_probability: null,
    }).reason).toBe("paired_p1_unavailable");

    expect(deriveLiveDraftValueDecision({
      ...detail,
      winner: { ...detail.winner!, period: "map_2" },
    }, mapping, prediction).reason).toBe("complete_current_map_odds_unavailable");
  });
});
