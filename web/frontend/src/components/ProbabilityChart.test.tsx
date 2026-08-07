import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { WinnerTimelinePoint } from "../types";
import {
  isCompleteDeVigPoint,
  ProbabilityChart,
  resolveProbabilityPeriod,
  withGaps,
} from "./ProbabilityChart";


vi.mock("echarts-for-react/lib/core", () => ({
  default: () => <div data-testid="probability-chart-canvas" />,
}));


const timeline: WinnerTimelinePoint[] = [{
  observed_at: "2026-08-07T10:00:00Z",
  period: "map_1",
  prices: { team_one: 1.8, team_two: 2.1 },
  probabilities: { team_one: 0.54, team_two: 0.46 },
  status: { team_one: "open", team_two: "open" },
}, {
  observed_at: "2026-08-07T11:00:00Z",
  period: "map_2",
  prices: { team_one: 1.7, team_two: 2.2 },
  probabilities: { team_one: 0.57, team_two: 0.43 },
  status: { team_one: "open", team_two: "open" },
}];


describe("ProbabilityChart", () => {
  it("uses the current period and keeps a visible same-data summary", () => {
    render(
      <ProbabilityChart
        preferredPeriod="map_2"
        teamOne="Aurora"
        teamTwo="Beacon"
        timeline={timeline}
      />,
    );

    expect(screen.getByTestId("probability-chart-canvas")).toBeInTheDocument();
    expect(screen.getByText("57.0%")).toBeInTheDocument();
    expect(screen.getByText("43.0%")).toBeInTheDocument();

    fireEvent.change(screen.getByRole("combobox", { name: "市场概率走势局数" }), {
      target: { value: "map_1" },
    });
    expect(screen.getByText("54.0%")).toBeInTheDocument();
    expect(screen.getByText("46.0%")).toBeInTheDocument();
  });

  it("explains why no complete de-vig snapshot can be shown", () => {
    render(
      <ProbabilityChart
        teamOne="Aurora"
        teamTwo="Beacon"
        timeline={[{
          ...timeline[0],
          prices: { team_one: 1.8, team_two: Number.NaN },
        }]}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("暂无完整胜负盘快照");
    expect(screen.queryByTestId("probability-chart-canvas")).not.toBeInTheDocument();
  });

  it("breaks the line across collection gaps longer than 150 seconds", () => {
    const points = [
      ["2026-08-07T10:00:00Z", 0.54],
      ["2026-08-07T10:02:00Z", 0.55],
      ["2026-08-07T10:04:31Z", 0.56],
    ].map(([observedAt, probability]) => ({
      ...timeline[0],
      observed_at: String(observedAt),
      probabilities: {
        team_one: Number(probability),
        team_two: 1 - Number(probability),
      },
    }));

    expect(withGaps(points, "team_one").filter(([, value]) => value == null))
      .toHaveLength(2);
  });
});


describe("ProbabilityChart contracts", () => {
  it("accepts only complete normalized two-sided snapshots", () => {
    expect(isCompleteDeVigPoint(timeline[0])).toBe(true);
    expect(isCompleteDeVigPoint({
      ...timeline[0],
      probabilities: { team_one: 0.54, team_two: 0.50 },
    })).toBe(false);
    expect(isCompleteDeVigPoint({
      ...timeline[0],
      prices: { team_one: 1.8, team_two: Number.NaN },
    })).toBe(false);
  });

  it("prefers an explicit selection, then the backend period, then the latest period", () => {
    const periods = ["map_1", "map_2", "map_3"];
    expect(resolveProbabilityPeriod(periods, "map_1", "map_2")).toBe("map_1");
    expect(resolveProbabilityPeriod(periods, null, "map_2")).toBe("map_2");
    expect(resolveProbabilityPeriod(periods, null, null)).toBe("map_3");
  });
});
