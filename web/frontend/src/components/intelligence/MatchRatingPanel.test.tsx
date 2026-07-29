import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { IntelligenceMatchRating } from "../../types";
import { MatchRatingPanel } from "./MatchRatingPanel";

describe("MatchRatingPanel", () => {
  it("keeps a real zero execution score instead of replacing it with neutral 50", () => {
    const rating: IntelligenceMatchRating = {
      rating_version: "rating-v1",
      rounding: "decimal-half-up-2dp",
      source_score_version: "score-v1",
      benchmark_cutoff: "2026-07-01T00:00:00Z",
      player_count: 10,
      overall: { execution_score: 25, result_adjusted_score: 25, coverage: 1 },
      radiant: { execution_score: 0, result_adjusted_score: 0, coverage: 1 },
      dire: { execution_score: 50, result_adjusted_score: 50, coverage: 1 },
    };

    render(<MatchRatingPanel rating={rating} radiant="Radiant" dire="Dire" />);

    expect(screen.getByText("Radiant 0.00分")).toBeInTheDocument();
    expect(screen.getByTitle("Radiant: 0.00分")).toHaveStyle({ width: "0%" });
  });
});
