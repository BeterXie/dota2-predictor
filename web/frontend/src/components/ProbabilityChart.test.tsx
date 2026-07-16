import { describe, expect, it } from "vitest";

import { resolvePeriod } from "./ProbabilityChart";


describe("resolvePeriod", () => {
  const periods = ["map_1", "map_2", "map_3"];

  it("defaults to the first map instead of a future map", () => {
    expect(resolvePeriod(periods, null, null)).toBe("map_1");
  });

  it("follows the current backend period until the user selects another map", () => {
    expect(resolvePeriod(periods, null, "map_2")).toBe("map_2");
    expect(resolvePeriod(periods, "map_3", "map_2")).toBe("map_3");
  });
});
