import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RoshAnalysisRunResponse } from "../types";
import { PredictionResult, PrematchWorkspace } from "./PrematchWorkspace";

vi.mock("../api", () => ({
  createRoshAnalysis: vi.fn(),
  fetchPrematchDraft: vi.fn(),
  fetchPrematchHeroGrid: vi.fn().mockResolvedValue({ str: [], agi: [], int: [], all: [] }),
  fetchPrematchLeagues: vi.fn().mockResolvedValue([]),
  fetchPrematchRecentMatches: vi.fn().mockResolvedValue([]),
  fetchPrematchTeams: vi.fn().mockResolvedValue([]),
}));

afterEach(cleanup);

describe("PrematchWorkspace", () => {
  it("does not expose manual data-fetch controls", () => {
    render(<PrematchWorkspace />);

    expect(screen.queryByRole("button", { name: "重新抓取" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "抓取新数据" })).not.toBeInTheDocument();
  });
});

describe("PredictionResult", () => {
  it("shows the complete minute range instead of only the last ten points", () => {
    render(<PredictionResult result={predictionResult} />);

    expect(screen.getByText("共 3 个时间点 · 20-60 分钟")).toBeInTheDocument();
    const rows = screen.getAllByRole("row");
    expect(rows.some((row) => within(row).queryByText("20"))).toBe(true);
    expect(rows.some((row) => within(row).queryByText("60"))).toBe(true);
  });

  it("shows official score semantics without pseudo probability", () => {
    render(<PredictionResult result={predictionResult} />);

    expect(screen.getAllByText("+5.8").length).toBeGreaterThan(0);
    expect(screen.getByText("Rosh score，不是胜率")).toBeInTheDocument();
    expect(screen.queryByText("55.8%")).not.toBeInTheDocument();
    expect(screen.queryByText(/Radiant 胜率|Dire 胜率/)).not.toBeInTheDocument();
  });
});

const predictionResult: RoshAnalysisRunResponse = {
  schema: "rosh-analysis-run/v1",
  run_id: "a".repeat(64),
  status: "succeeded",
  mode: "explicit_draft",
  match_id: null,
  date_time: 1_785_000_000,
  draft_hash: "b".repeat(64),
  rosh_profile_id: "stratz-rosh-web-2026-07-28-v2",
  formula_version: "stratz-official-rosh/2026-07-28-v2",
  request_profile_hash: "c".repeat(64),
  upstream_bundle_hash: "d".repeat(64),
  scorer_source_hash: "e".repeat(64),
  canonical_profile_hash: "f".repeat(64),
  serialization_version: "rfc8785-jcs/v1",
  evidence_hash: "1".repeat(64),
  collected_at: "2026-07-28T04:00:00Z",
  radiant_team_score: -4.9,
  dire_team_score: -10.7,
  relative_advantage: 5.8,
  error_code: null,
  hero_components: [{
    team_side: "RADIANT",
    position_id: 1,
    hero_id: 54,
    position_base_diff: 1.2,
    same_team_synergy: 0.3,
    opponent_matchup_synergy: -0.2,
    raw_score: 1.3,
    display_score: 1.3,
  }],
  minute_points: [20, 21, 60].map((minute) => ({
    minute,
    radiant_time_delta: 1,
    dire_time_delta: -1,
    synergy_delta: 3.8,
    raw_score: minute === 21 ? 0 : 5.75,
    display_score: minute === 21 ? 0 : 5.8,
    rank_source_counts: {
      DIVINE_IMMORTAL: 6,
      ALL_RANK_FALLBACK: 4,
    },
    slots: [],
  })),
};
