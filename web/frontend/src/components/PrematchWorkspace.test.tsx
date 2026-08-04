import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createRoshAnalysis,
  fetchPrematchDraft,
  fetchPrematchHeroGrid,
  fetchPrematchRecentMatches,
  fetchPrematchTeams,
  fetchRoshAnalysisRecords,
} from "../api";
import type { RoshAnalysisRunResponse } from "../types";
import { PredictionResult, PrematchWorkspace } from "./PrematchWorkspace";

vi.mock("../api", () => ({
  createRoshAnalysis: vi.fn(),
  fetchPrematchDraft: vi.fn(),
  fetchPrematchHeroGrid: vi.fn().mockResolvedValue({ str: [], agi: [], int: [], all: [] }),
  fetchPrematchLeagues: vi.fn().mockResolvedValue([]),
  fetchPrematchRecentMatches: vi.fn().mockResolvedValue([]),
  fetchPrematchTeams: vi.fn().mockResolvedValue([]),
  fetchRoshAnalysisRecords: vi.fn().mockResolvedValue({
    query_source: "opendota",
    query_match_id: "",
    records: [],
  }),
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("PrematchWorkspace", () => {
  it("does not expose manual data-fetch controls", () => {
    render(<PrematchWorkspace />);

    expect(screen.queryByRole("button", { name: "重新抓取" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "抓取新数据" })).not.toBeInTheDocument();
  });

  it("disambiguates official match and team identities in selectors", async () => {
    vi.mocked(fetchPrematchRecentMatches).mockResolvedValueOnce([{
      match_id: 8904322271,
      radiant_team_id: 10,
      dire_team_id: 20,
      radiant_name: "Zero Tenacity",
      dire_name: "LGD.Pinghu",
      start_time: 1784478900,
      leagueid: 19785,
      league_name: "The Games of the Future 2026",
    }]);
    vi.mocked(fetchPrematchTeams).mockResolvedValueOnce([{
      team_id: 8254145,
      name: "Execration",
      tag: "XctN",
      logo_url: null,
      match_count: 5,
    }, {
      team_id: 10207960,
      name: "Execration",
      tag: "XctN",
      logo_url: null,
      match_count: 3,
    }]);

    render(<PrematchWorkspace />);

    expect(await screen.findByRole("option", {
      name: /#8904322271.*Zero Tenacity vs LGD\.Pinghu/,
    })).toBeInTheDocument();
    expect(await screen.findAllByRole("option", {
      name: "Execration · #8254145 (5 场)",
    })).toHaveLength(2);
    expect(screen.getAllByRole("option", {
      name: "Execration · #10207960 (3 场)",
    })).toHaveLength(2);
  });

  it("uses the source match end time for historical Rosh identity", async () => {
    vi.mocked(fetchPrematchDraft).mockResolvedValue({
      match_id: 8904322271,
      radiant_team_id: 10,
      dire_team_id: 20,
      league_id: 19785,
      start_time: 1784478900,
      end_time: 1784481374,
      radiant_heroes: [1, 2, 3, 4, 5].map((hero_id) => ({
        hero_id,
        name: `Radiant ${hero_id}`,
        image_url: "",
        account_id: hero_id,
      })),
      dire_heroes: [6, 7, 8, 9, 10].map((hero_id) => ({
        hero_id,
        name: `Dire ${hero_id}`,
        image_url: "",
        account_id: hero_id,
      })),
    });
    vi.mocked(createRoshAnalysis).mockResolvedValue(predictionResult);
    render(<PrematchWorkspace />);

    fireEvent.change(screen.getByRole("textbox", { name: "比赛 ID" }), {
      target: { value: "8904322271" },
    });
    fireEvent.click(screen.getByRole("button", { name: "自动填充" }));
    await screen.findByText(/比赛 8904322271 已载入/);
    fireEvent.click(screen.getByRole("button", { name: "分析阵容" }));

    await waitFor(() => expect(fetchRoshAnalysisRecords).toHaveBeenCalledWith(
      "opendota",
      "8904322271",
    ));
    await waitFor(() => expect(createRoshAnalysis).toHaveBeenCalledWith(
      expect.objectContaining({
        mode: "historical_match",
        match_id: 8904322271,
        date_time: 1784481374,
        match_links: [{
          source: "opendota",
          source_match_id: "8904322271",
        }],
      }),
    ));
  });

  it("uses an existing OpenDota-linked run without calling STRATZ again", async () => {
    vi.mocked(fetchPrematchDraft).mockResolvedValue({
      match_id: 8904322271,
      radiant_team_id: 10,
      dire_team_id: 20,
      league_id: 19785,
      start_time: 1784478900,
      end_time: 1784481374,
      radiant_heroes: [1, 2, 3, 4, 5].map((hero_id) => ({
        hero_id, name: `Radiant ${hero_id}`, image_url: "", account_id: hero_id,
      })),
      dire_heroes: [6, 7, 8, 9, 10].map((hero_id) => ({
        hero_id, name: `Dire ${hero_id}`, image_url: "", account_id: hero_id,
      })),
    });
    const existing = {
      ...predictionResult,
      mode: "historical_match" as const,
      match_id: 8904322271,
      date_time: 1784481374,
    };
    vi.mocked(fetchRoshAnalysisRecords).mockResolvedValueOnce({
      query_source: "opendota",
      query_match_id: "8904322271",
      records: [{
        run: existing,
        links: [
          { source: "opendota", source_match_id: "8904322271" },
          { source: "stratz", source_match_id: "8904322271" },
        ],
      }],
    });
    render(<PrematchWorkspace />);

    fireEvent.change(screen.getByRole("textbox", { name: "比赛 ID" }), {
      target: { value: "8904322271" },
    });
    fireEvent.click(screen.getByRole("button", { name: "自动填充" }));
    await screen.findByText(/比赛 8904322271 已载入/);
    fireEvent.click(screen.getByRole("button", { name: "分析阵容" }));

    await screen.findByText("阵容更偏向 Radiant");
    expect(createRoshAnalysis).not.toHaveBeenCalled();
  });

  it("uses an accessible hero dialog with focus and Escape handling", async () => {
    vi.mocked(fetchPrematchHeroGrid).mockResolvedValue({
      str: [{
        hero_id: 54,
        localized_name: "Lifestealer",
        hero_key: "npc_dota_hero_life_stealer",
        image_url: "",
      }],
      agi: [],
      int: [],
      all: [],
    });
    render(<PrematchWorkspace />);

    const opener = await screen.findByRole("button", { name: "选择 Radiant 1 号位英雄" });
    fireEvent.click(opener);
    const search = await screen.findByRole("textbox", { name: "搜索英雄" });

    expect(screen.getByRole("dialog", { name: "英雄选择器" })).toBeInTheDocument();
    await waitFor(() => expect(search).toHaveFocus());
    fireEvent.keyDown(search, { key: "Escape", code: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "英雄选择器" }))
      .not.toBeInTheDocument());

    fireEvent.click(opener);
    const reopenedSearch = await screen.findByRole("textbox", { name: "搜索英雄" });
    fireEvent.change(reopenedSearch, { target: { value: "Life" } });
    expect(reopenedSearch).toHaveValue("Life");
    fireEvent.click(screen.getByRole("button", { name: "关闭英雄选择器" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "英雄选择器" }))
      .not.toBeInTheDocument());

    fireEvent.click(opener);
    expect(await screen.findByRole("textbox", { name: "搜索英雄" })).toHaveValue("");
  });
});

describe("PredictionResult", () => {
  it("shows the complete minute range instead of only the last ten points", () => {
    render(<PredictionResult result={predictionResult} />);

    fireEvent.click(screen.getByText("查看详细评分与证据"));
    expect(screen.getByText("共 3 个时间点 · 20-60 分钟")).toBeInTheDocument();
    const rows = screen.getAllByRole("row");
    expect(rows.some((row) => within(row).queryByText("20"))).toBe(true);
    expect(rows.some((row) => within(row).queryByText("60"))).toBe(true);
  });

  it("shows official score semantics without pseudo probability", () => {
    render(<PredictionResult result={predictionResult} />);

    expect(screen.getByText("阵容更偏向 Radiant")).toBeInTheDocument();
    expect(screen.getByText("中等")).toBeInTheDocument();
    expect(screen.getByText("约 20 分钟")).toBeInTheDocument();
    expect(screen.getByText("位置对位贡献最大")).toBeInTheDocument();
    expect(screen.getAllByText("+5.8").length).toBeGreaterThan(0);
    expect(screen.getByText(/Rosh 阵容方向评分，不等同于比赛胜率/)).toBeInTheDocument();
    expect(screen.queryByText("55.8%")).not.toBeInTheDocument();
    expect(screen.queryByText(/Radiant 胜率|Dire 胜率/)).not.toBeInTheDocument();
  });

  it("keeps technical evidence collapsed while preserving named hero details", () => {
    const view = render(
      <PredictionResult
        heroNames={new Map([[54, "Lifestealer"]])}
        result={predictionResult}
      />,
    );

    const details = view.container.querySelector(".prematch-result-details");
    expect(details).not.toHaveAttribute("open");
    expect(screen.getByText("英雄分解")).toBeInTheDocument();
    fireEvent.click(screen.getByText("查看详细评分与证据"));
    expect(details).toHaveAttribute("open");
    expect(screen.getByText("Lifestealer")).toBeInTheDocument();
    expect(screen.getByText("#54")).toBeInTheDocument();
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
