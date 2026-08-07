import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { WinnerTimelinePoint } from "../types";
import { OddsProbabilityChart, resolveOddsPeriod } from "./OddsProbabilityChart";


vi.mock("echarts-for-react/lib/core", () => ({
  default: () => <div data-testid="odds-probability-chart-canvas" />,
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


describe("OddsProbabilityChart", () => {
  it("uses the current period and keeps a visible same-data summary", () => {
    render(
      <OddsProbabilityChart
        preferredPeriod="map_2"
        teamOne="Aurora"
        teamTwo="Beacon"
        timeline={timeline}
      />,
    );

    expect(screen.getByTestId("odds-probability-chart-canvas")).toBeInTheDocument();
    expect(screen.getByText("57.0%")).toBeInTheDocument();
    expect(screen.getByText("43.0%")).toBeInTheDocument();

    fireEvent.change(screen.getByRole("combobox", { name: "赔率图局数" }), {
      target: { value: "map_1" },
    });
    expect(screen.getByText("54.0%")).toBeInTheDocument();
    expect(screen.getByText("46.0%")).toBeInTheDocument();
  });

  it("explains why the chart is unavailable", () => {
    render(
      <OddsProbabilityChart
        teamOne="Aurora"
        teamTwo="Beacon"
        timeline={[]}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("暂无完整胜负盘快照");
    expect(screen.queryByTestId("odds-probability-chart-canvas")).not.toBeInTheDocument();
  });
});


describe("resolveOddsPeriod", () => {
  it("prefers an explicit selection, then the backend period, then the latest period", () => {
    const periods = ["map_1", "map_2", "map_3"];
    expect(resolveOddsPeriod(periods, "map_1", "map_2")).toBe("map_1");
    expect(resolveOddsPeriod(periods, null, "map_2")).toBe("map_2");
    expect(resolveOddsPeriod(periods, null, null)).toBe("map_3");
  });
});
