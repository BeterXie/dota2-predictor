import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchVisionCalibration, saveVisionCalibrationLabel } from "./api";


describe("API errors", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows a FastAPI string detail for read failures", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: "Observation JSONL belongs to a different RayBet match" }),
      { status: 422, headers: { "Content-Type": "application/json" } },
    )));

    await expect(fetchVisionCalibration()).rejects.toThrow(
      "Observation JSONL belongs to a different RayBet match",
    );
  });

  it("shows FastAPI validation messages for mutation failures", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: [{ msg: "Field required" }, { msg: "Input should be greater than 0" }] }),
      { status: 422, headers: { "Content-Type": "application/json" } },
    )));

    await expect(saveVisionCalibrationLabel("event", {
      hero_ids: Array.from({ length: 10 }, (_, index) => index + 1),
      raybet_match_id: "42",
      map_number: 1,
      note: null,
    }, "csrf")).rejects.toThrow("Field required；Input should be greater than 0");
  });
});
