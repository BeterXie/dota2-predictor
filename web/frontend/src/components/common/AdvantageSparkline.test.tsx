import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { IntelligenceRoshMinutePoint } from "../../types";
import { AdvantageSparkline } from "./AdvantageSparkline";

function point(minute: number, winRateGraph: number): IntelligenceRoshMinutePoint {
  return {
    minute,
    time_start: minute,
    time_end: minute + 1,
    win_rate_graph: winRateGraph,
    match_percentage: 50,
    hero_adjustment: 0,
    synergy_adjustment: 0,
    player_adjustment: 0,
  };
}

describe("AdvantageSparkline", () => {
  it("uses separate Radiant and Dire fills around the zero line", () => {
    const { container } = render(
      <AdvantageSparkline
        points={[point(20, 4.2), point(40, -2.5), point(60, 0)]}
        radiantName="Aurora"
        direName="Beacon"
      />,
    );

    const areaPaths = Array.from(container.querySelectorAll("path.advantage-area"));
    expect(areaPaths).toHaveLength(2);
    expect(areaPaths[0].getAttribute("fill")).toContain("radiant-gradient");
    expect(areaPaths[1].getAttribute("fill")).toContain("dire-gradient");
    expect(container.querySelector("path[data-side='radiant']")).toHaveAttribute("stroke", "var(--accent, #61cec1)");
    expect(container.querySelector("path[data-side='dire']")).toHaveAttribute("stroke", "var(--team-two, #ef8b79)");
    expect(container.querySelector("circle title:last-child")?.textContent).not.toBe("60分钟: Beacon +0.0%");
    expect(container.textContent).toContain("60分钟: 均势 0.0%");
    expect(container.querySelector("circle:last-of-type")).toHaveAttribute("fill", "var(--text-dim)");
  });

  it("never renders a red trend line while every minute favors Radiant", () => {
    const { container } = render(
      <AdvantageSparkline points={[point(20, 13), point(40, 12.8), point(60, 13.2)]} />,
    );

    expect(container.querySelector("path[data-side='radiant']")).not.toBeNull();
    expect(container.querySelector("path[data-side='dire']")).toBeNull();
  });

  it("keeps a stable chart height while filling the available width", () => {
    const { container } = render(
      <AdvantageSparkline points={[point(25, 2), point(35, 4)]} height={96} />,
    );

    const chart = container.querySelector("svg");
    expect(chart).toHaveAttribute("height", "96");
    expect(chart).toHaveAttribute("width", "100%");
    expect(chart).toHaveAttribute("aria-label", "25-35 分钟优势变动曲线");
  });
});
