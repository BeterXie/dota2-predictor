import type {
  LiveDraftMapping,
  LiveDraftProspectivePrediction,
  MatchDetail,
} from "../../types";


export const LIVE_DRAFT_VALUE_STRATEGY_VERSION = "live-draft-value-shadow-v1";
export const LIVE_DRAFT_VALUE_MIN_EDGE = 0.08;

export type LiveDraftValueDecisionStatus = "waiting" | "no_bet" | "candidate";

export interface LiveDraftValueDecision {
  status: LiveDraftValueDecisionStatus;
  reason: string;
  strategyVersion: string;
  selectedMatchSide: "team_one" | "team_two" | null;
  selectedTeamName: string | null;
  modelProbability: number | null;
  marketProbability: number | null;
  edge: number | null;
  price: number | null;
  oddsObservedAt: string | null;
  predictionHash: string | null;
}

function waiting(reason: string, predictionHash: string | null = null): LiveDraftValueDecision {
  return {
    status: "waiting",
    reason,
    strategyVersion: LIVE_DRAFT_VALUE_STRATEGY_VERSION,
    selectedMatchSide: null,
    selectedTeamName: null,
    modelProbability: null,
    marketProbability: null,
    edge: null,
    price: null,
    oddsObservedAt: null,
    predictionHash,
  };
}

function validProbability(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
}

export function deriveLiveDraftValueDecision(
  detail: MatchDetail,
  mapping: LiveDraftMapping | null,
  prediction: LiveDraftProspectivePrediction | null,
): LiveDraftValueDecision {
  if (!mapping?.is_locked || !prediction) return waiting("prediction_unavailable");
  const predictionHash = prediction.prediction_hash;
  if (
    prediction.identity.raybet_match_id !== detail.raybet_match_id
    || prediction.identity.map_number !== mapping.map_number
    || prediction.identity.mapping_version !== mapping.version
  ) {
    return waiting("prediction_identity_mismatch", predictionHash);
  }
  const p1 = prediction.p1_probability;
  if (prediction.record_status !== "paired" || !validProbability(p1)) {
    return waiting("paired_p1_unavailable", predictionHash);
  }
  if (
    prediction.causal_evidence.live_state_input_used
    || prediction.causal_evidence.causal_status !== "eligible"
  ) {
    return waiting("prediction_causal_gate_failed", predictionHash);
  }

  const winner = detail.winner;
  const expectedPeriod = `map_${mapping.map_number}`;
  if (
    !winner?.complete
    || winner.period !== expectedPeriod
    || !validProbability(winner.probabilities?.team_one)
    || !validProbability(winner.probabilities?.team_two)
  ) {
    return waiting("complete_current_map_odds_unavailable", predictionHash);
  }
  const teamOnePrice = winner.prices?.team_one;
  const teamTwoPrice = winner.prices?.team_two;
  if (
    typeof teamOnePrice !== "number" || !Number.isFinite(teamOnePrice) || teamOnePrice <= 1
    || typeof teamTwoPrice !== "number" || !Number.isFinite(teamTwoPrice) || teamTwoPrice <= 1
  ) {
    return waiting("valid_current_map_prices_unavailable", predictionHash);
  }

  const radiantTeamIds = new Set(
    mapping.slots.filter((slot) => slot.side === "radiant").map((slot) => slot.team_id),
  );
  if (radiantTeamIds.size !== 1) return waiting("radiant_team_identity_invalid", predictionHash);
  const radiantTeamId = [...radiantTeamIds][0];
  const radiantContext = detail.draft_context?.status === "ready"
    ? detail.draft_context.teams.find((team) => team.team_id === radiantTeamId)
    : undefined;
  if (!radiantContext) return waiting("radiant_match_side_unavailable", predictionHash);

  const modelProbabilities = radiantContext.match_side === "team_one"
    ? { team_one: p1, team_two: 1 - p1 }
    : { team_one: 1 - p1, team_two: p1 };
  const marketProbabilities = winner.probabilities;
  const edges = {
    team_one: modelProbabilities.team_one - marketProbabilities.team_one,
    team_two: modelProbabilities.team_two - marketProbabilities.team_two,
  };
  const selectedMatchSide = edges.team_one >= edges.team_two ? "team_one" : "team_two";
  const selectedContext = detail.draft_context?.teams.find(
    (team) => team.match_side === selectedMatchSide,
  );
  const edge = edges[selectedMatchSide];

  return {
    status: edge >= LIVE_DRAFT_VALUE_MIN_EDGE ? "candidate" : "no_bet",
    reason: edge >= LIVE_DRAFT_VALUE_MIN_EDGE ? "minimum_edge_met" : "edge_below_threshold",
    strategyVersion: LIVE_DRAFT_VALUE_STRATEGY_VERSION,
    selectedMatchSide,
    selectedTeamName: selectedContext?.team_name
      ?? (selectedMatchSide === "team_one" ? detail.team_one : detail.team_two),
    modelProbability: modelProbabilities[selectedMatchSide],
    marketProbability: marketProbabilities[selectedMatchSide],
    edge,
    price: selectedMatchSide === "team_one" ? teamOnePrice : teamTwoPrice,
    oddsObservedAt: winner.observed_at,
    predictionHash,
  };
}
