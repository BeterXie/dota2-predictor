import type {
  IntelligenceAvailabilityMode,
  IntelligenceDraftQualitySlice,
  IntelligenceMatchDetail,
  IntelligenceOverview,
  IntelligencePlayerMapScore,
  IntelligencePlayerPerformance,
  IntelligenceRoshMinutePoint,
  IntelligenceTeamProfile,
} from "../types";

export function normalizedDetail(detail: IntelligenceMatchDetail) {
  if ("match" in detail) {
    return {
      match: detail.match,
      radiantState: detail.radiant_state,
      direState: detail.dire_state,
      playerPerformance: detail.player_performance || [],
      playerScores: detail.player_scores,
      matchRating: detail.match_rating,
      roshLineupScore: detail.rosh_lineup_score ?? null,
      draftPredictions: detail.draft_predictions,
    };
  }
  return {
    match: detail,
    radiantState: detail.states?.radiant ?? detail.radiant_state ?? null,
    direState: detail.states?.dire ?? detail.dire_state ?? null,
    playerPerformance: detail.player_performance || [],
    playerScores: detail.player_scores,
    matchRating: detail.match_rating,
    roshLineupScore: detail.rosh_lineup_score ?? null,
    draftPredictions: detail.draft_predictions,
  };
}

export function qualitySlices(overview: IntelligenceOverview): IntelligenceDraftQualitySlice[] {
  if (overview.draft_quality_slices) return overview.draft_quality_slices;
  if (Array.isArray(overview.draft_quality)) return overview.draft_quality;
  return overview.draft_quality?.slices || [];
}

export function availabilityStatus(
  overview: IntelligenceOverview,
  slices: IntelligenceDraftQualitySlice[],
  mode: IntelligenceAvailabilityMode,
): boolean {
  if (overview.availability?.[mode] != null) return Boolean(overview.availability[mode]);
  if (overview.draft_quality && !Array.isArray(overview.draft_quality)) {
    const status = overview.draft_quality.availability?.[mode];
    if (status != null) return Boolean(status);
  }
  return slices.some((item) => item.availability_mode === mode && item.availability_status === "available");
}

export function posteriorRate(team: IntelligenceTeamProfile, metric: string): {
  mean: number;
  opportunities: number;
} | null {
  if (!Array.isArray(team.posterior_rates)) return null;
  const value = team.posterior_rates.find((item) => (
    typeof item === "object" && item !== null && (item as Record<string, unknown>).metric === metric
  ));
  if (!value || typeof value !== "object") return null;
  const row = value as Record<string, unknown>;
  return typeof row.mean === "number" && typeof row.opportunities === "number"
    ? { mean: row.mean, opportunities: row.opportunities }
    : null;
}

export function selectRoshMinutePoints(
  points: IntelligenceRoshMinutePoint[],
): IntelligenceRoshMinutePoint[] {
  if (points.length <= 10) return points;
  return points.filter((point, index) => {
    if (index === 0 || index === points.length - 1 || point.minute % 5 === 0) return true;
    return Math.sign(point.win_rate_graph) !== Math.sign(points[index - 1].win_rate_graph);
  });
}

export function uniqueNonEmpty(items: (string | null | undefined)[]): string[] {
  return Array.from(new Set(items.filter((item): item is string => Boolean(item && item.trim()))));
}

export function summarizeCutoffs(values: string[]): {
  count: number;
  first: string;
  last: string;
} | null {
  const cutoffs = uniqueNonEmpty(values).sort((left, right) => left.localeCompare(right));
  if (!cutoffs.length) return null;
  return { count: cutoffs.length, first: cutoffs[0], last: cutoffs.at(-1) || cutoffs[0] };
}

export function collectScoreEvidence(scores: IntelligencePlayerMapScore[]): {
  scoreVersions: string[];
  benchmarkCutoffs: string[];
} {
  return {
    scoreVersions: uniqueNonEmpty(scores.map((score) => score.score_version)),
    benchmarkCutoffs: uniqueNonEmpty(scores.map((score) => score.benchmark_cutoff)),
  };
}

export function mergePlayerRows(
  scores: IntelligencePlayerMapScore[],
  performance: IntelligencePlayerPerformance[],
): (IntelligencePlayerMapScore | IntelligencePlayerPerformance)[] {
  const scoresBySlot = new Map(scores.map((row) => [row.player_slot, row]));
  const performanceBySlot = new Map(performance.map((row) => [row.player_slot, row]));
  const slots = Array.from(new Set([
    ...scoresBySlot.keys(),
    ...performanceBySlot.keys(),
  ])).sort((left, right) => left - right);

  return slots.map((slot) => {
    const score = scoresBySlot.get(slot);
    const archived = performanceBySlot.get(slot);
    if (!score) return archived!;
    if (!archived) return score;
    return {
      ...archived,
      ...score,
      account_id: score.account_id ?? archived.account_id,
      player_name: score.player_name ?? archived.player_name,
      team_id: score.team_id ?? archived.team_id,
      side: score.side ?? archived.side,
      hero_id: score.hero_id ?? archived.hero_id,
      hero_name: score.hero_name ?? archived.hero_name,
      performance: score.performance ?? archived.performance,
    };
  });
}
