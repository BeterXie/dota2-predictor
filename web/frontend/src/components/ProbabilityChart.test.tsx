import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { mapNumberForPeriod, resolvePeriod } from "../probability-period";
import { ProbabilityChart, withGaps } from "./ProbabilityChart";

vi.mock("echarts-for-react/lib/core", () => ({
  default: () => <div data-testid="probability-chart-canvas" />,
}));

afterEach(cleanup);

describe("resolvePeriod", () => {
  const periods = ["map_1", "map_2", "map_3"];

  it("defaults to the first map instead of a future map", () => {
    expect(resolvePeriod(periods, null, null)).toBe("map_1");
  });

  it("follows the current backend period until the user selects another map", () => {
    expect(resolvePeriod(periods, null, "map_2")).toBe("map_2");
    expect(resolvePeriod(periods, "map_3", "map_2")).toBe("map_3");
  });

  it("opens historical replay on the latest observed map when no period is pinned", () => {
    expect(resolvePeriod(periods, null, null, true)).toBe("map_3");
  });

  it("only derives an exact positive map number from a map period", () => {
    expect(mapNumberForPeriod("map_2")).toBe(2);
    expect(mapNumberForPeriod("match_winner")).toBeNull();
    expect(mapNumberForPeriod("map_0")).toBeNull();
    expect(mapNumberForPeriod("map_2_extra")).toBeNull();
  });
});

describe("ProbabilityChart controls", () => {
  it("keeps the default two-minute collection cadence connected", () => {
    const points = [
      ["2026-08-02T00:00:00Z", 0.55],
      ["2026-08-02T00:02:00Z", 0.57],
      ["2026-08-02T00:04:31Z", 0.59],
    ].map(([observedAt, probability]) => ({
      observed_at: String(observedAt),
      period: "map_1",
      prices: { team_one: 1.8, team_two: 2.1 },
      probabilities: {
        team_one: Number(probability),
        team_two: 1 - Number(probability),
      },
      status: { team_one: "open", team_two: "open" },
    }));

    const series = withGaps(points, "team_one");

    expect(series.filter(([, value]) => value == null)).toHaveLength(2);
    expect(series.slice(0, 2).map(([, value]) => value)).toEqual([0.55, 0.57]);
  });

  it("keeps the map selector in a dedicated normal-flow row above the chart canvas", () => {
    const onPeriodChange = vi.fn();
    render(
      <ProbabilityChart
        decisions={[]}
        onPeriodChange={onPeriodChange}
        selectedPeriod="map_2"
        teamOne="Aurora"
        teamTwo="Beacon"
        timeline={[{
          observed_at: "2026-07-17T00:00:00Z",
          period: "map_1",
          prices: { team_one: 1.8, team_two: 2.1 },
          probabilities: { team_one: 0.54, team_two: 0.46 },
          status: { team_one: "open", team_two: "open" },
        }, {
          observed_at: "2026-07-17T01:00:00Z",
          period: "map_2",
          prices: { team_one: 1.7, team_two: 2.2 },
          probabilities: { team_one: 0.57, team_two: 0.43 },
          status: { team_one: "open", team_two: "open" },
        }]}
      />,
    );

    const select = screen.getByRole("combobox", { name: "局数" });
    const controls = select.closest(".chart-controls");
    expect(controls).not.toBeNull();
    expect(controls?.nextElementSibling).toHaveClass("probability-chart-canvas");
    fireEvent.change(select, { target: { value: "map_1" } });
    expect(onPeriodChange).toHaveBeenCalledWith("map_1");
  });
});
