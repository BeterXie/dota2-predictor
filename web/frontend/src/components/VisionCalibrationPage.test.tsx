import { FluentProvider, webDarkTheme } from "@fluentui/react-components";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  VisionCalibrationBootstrap,
  VisionCalibrationCandidate,
  VisionCalibrationEvaluation,
  VisionCalibrationEvent,
  VisionCalibrationLabel,
} from "../types";

const api = vi.hoisted(() => ({
  buildVisionCalibrationCandidate: vi.fn(),
  fetchHeroGrid: vi.fn(),
  fetchVisionCalibration: vi.fn(),
  runVisionCalibrationEvaluation: vi.fn(),
  saveVisionCalibrationLabel: vi.fn(),
}));

vi.mock("../api", () => api);

import { VisionCalibrationPage } from "./VisionCalibrationPage";

const event: VisionCalibrationEvent = {
  event_id: "0123456789abcdef0123",
  relative_path: "standard/event-1",
  captured_at: "2026-08-08T12:00:00+00:00",
  layout: "standard_dota_hud_1080p",
  profile_id: "standard_dota_hud_1080p",
  reason: "ambiguous_match",
  blocker_code: "draft_incomplete",
  screen_state: "game",
  replay_gate_status: "live",
  layout_state: "locked",
  quality_reason: null,
  quality_usable: true,
  crop_count: 10,
  frame_url: "/api/frame.jpg",
  crop_urls: Array.from({ length: 10 }, (_, index) => `/api/hero_slot_${index + 1}.jpg`),
  slot_diagnostics: Array.from({ length: 10 }, (_, index) => ({
    side: index < 5 ? "radiant" as const : "dire" as const,
    slot: index % 5 + 1,
    accepted: true,
    best_hero_id: index + 1,
    best_score: 0.94,
    margin: 0.18,
    reason: "accepted",
  })),
  label: null,
};

const label: VisionCalibrationLabel = {
  label_id: event.event_id,
  event_id: event.event_id,
  event_relative_path: event.relative_path,
  layout: event.layout,
  profile_id: event.profile_id,
  hero_ids: Array.from({ length: 10 }, (_, index) => index + 1),
  raybet_match_id: "38417147",
  map_number: 1,
  note: null,
  updated_at: "2026-08-08T12:10:00+00:00",
};

const candidate: VisionCalibrationCandidate = {
  candidate_id: `candidate-${event.event_id}-deadbeef`,
  label_id: "source-event-from-same-profile",
  layout: event.layout,
  profile_id: event.profile_id,
  hero_ids: label.hero_ids,
  created_at: "2026-08-08T12:11:00+00:00",
  feature_sha256: "a".repeat(64),
  production_feature_sha256: "b".repeat(64),
  promoted: false,
};

const evaluation: VisionCalibrationEvaluation = {
  evaluation_id: "evaluation-1",
  label_id: event.event_id,
  candidate_id: candidate.candidate_id,
  observation_file: "holdout.jsonl",
  raybet_match_id: "38417147",
  map_number: 1,
  layout_profile: "standard_dota_hud_1080p",
  mode: "perception",
  created_at: "2026-08-08T12:12:00+00:00",
  total_files: 30,
  trackable_frames: 28,
  best_candidate_accuracy: 0.97,
  accepted_precision: 0.99,
  final_locked_slots: 10,
  final_correct_locked_slots: 9,
  wrong_lock_count: 1,
  lock_latency_seconds: 4.2,
  exact_post_lock_rate: 0.96,
  candidate_feature_sha256: candidate.feature_sha256,
};

const bootstrap: VisionCalibrationBootstrap = {
  events: [event],
  profiles: [{
    profile_id: "standard_dota_hud_1080p",
    layout: "standard_dota_hud_1080p",
    event_count: 1,
    labeled_event_count: 0,
    candidate_count: 0,
    latest_captured_at: event.captured_at,
  }],
  candidates: [],
  evaluations: [],
  match_summaries: [{
    match_id: "38417147",
    observation_file: "holdout.jsonl",
    status: "live",
    status_label: "比赛进行中",
    phase: "game_started",
    observation_count: 420,
    evidence_frame_count: 38,
    manifest_event_count: 40,
    periodic_count: 36,
    draft_started: true,
    game_started: true,
    ended_final: false,
    first_captured_at: "2026-08-08T12:00:00+00:00",
    last_captured_at: "2026-08-08T12:30:00+00:00",
    latest_screen_state: "game",
    layout_profile: "standard_dota_hud_1080p",
    maps: [1],
    capture_status: "producing_trusted",
    heartbeat_fresh: true,
    raybet_match_id: "38417147",
    official_match_id: "8123456789",
    display_name: "官方 Match ID 8123456789 · Team A vs Team B · The International 2026",
  }],
  observation_files: [{
    name: "holdout.jsonl",
    bytes: 2048,
    raybet_match_id: "38417147",
    official_match_id: "8123456789",
    display_name: "官方 Match ID 8123456789 · Team A vs Team B · The International 2026",
  }],
  observation_root: "C:\\vision-corpus\\vision_observations",
  layout_profiles: ["standard_dota_hud_1080p"],
  production_feature_path: "vision/templates/hero_features.npz",
  candidate_boundary: "Candidates never overwrite production.",
};

const heroGrid = {
  str: Array.from({ length: 10 }, (_, index) => ({
    hero_id: index + 1,
    localized_name: `Hero ${index + 1}`,
    hero_key: `hero_${index + 1}`,
    image_url: `/hero/${index + 1}.png`,
  })),
  agi: [],
  int: [],
  all: [],
};

function renderPage(csrfToken: string | null = "csrf") {
  return render(
    <FluentProvider theme={webDarkTheme}>
      <VisionCalibrationPage csrfToken={csrfToken} />
    </FluentProvider>,
  );
}

describe("VisionCalibrationPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchVisionCalibration.mockResolvedValue(bootstrap);
    api.fetchHeroGrid.mockResolvedValue(heroGrid);
  });

  it("shows the empty real-frame collection state", async () => {
    api.fetchVisionCalibration.mockResolvedValue({ ...bootstrap, events: [] });
    renderPage();

    expect(await screen.findByText("还没有可校正的真实帧")).toBeInTheDocument();
    expect(screen.getByText("data/live_betting/vision_debug")).toBeInTheDocument();
  });

  it("renders the full frame and ten ordered crops", async () => {
    renderPage();

    const frame = await screen.findByAltText("选中 Vision 校正样本的完整直播帧");
    expect(frame).toHaveAttribute(
      "src",
      event.frame_url,
    );
    expect(screen.getAllByAltText(/英雄 crop$/)).toHaveLength(10);
    expect(screen.getByText("R1")).toBeInTheDocument();
    expect(screen.getByText("D5")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "上一样本" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "放大帧" }));
    expect(frame.closest(".vision-frame-canvas")).toHaveStyle({ width: "125%" });
  });

  it("summarizes the Vision corpus once per match", async () => {
    renderPage();

    const summary = await screen.findByRole("region", { name: "比赛汇总" });
    expect(within(summary).getByText("1 局")).toBeInTheDocument();
    expect(within(summary).getByText("比赛进行中")).toBeInTheDocument();
    expect(within(summary).getByText("420")).toBeInTheDocument();
    expect(within(summary).getByText("38")).toBeInTheDocument();
    expect(within(summary).getByText("BP 已确认")).toBeInTheDocument();
    expect(within(summary).getByText("开局已确认")).toBeInTheDocument();
    expect(within(summary).getByText("game_started")).toBeInTheDocument();
    expect(within(summary).getByRole("button", {
      name: "打开 官方 Match ID 8123456789 · Team A vs Team B · The International 2026 的校正记录",
    })).toHaveAttribute("aria-pressed", "false");
  });

  it("opens a match card on its retained label and evaluation history", async () => {
    api.fetchVisionCalibration.mockResolvedValue({
      ...bootstrap,
      events: [{ ...event, label }],
      profiles: [{
        profile_id: event.profile_id,
        layout: event.layout,
        event_count: 1,
        labeled_event_count: 1,
        candidate_count: 1,
        latest_captured_at: event.captured_at,
      }],
      candidates: [candidate],
      evaluations: [evaluation],
    });
    renderPage();

    const openMatch = await screen.findByRole("button", {
      name: "打开 官方 Match ID 8123456789 · Team A vs Team B · The International 2026 的校正记录",
    });
    fireEvent.click(openMatch);

    expect(openMatch).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("combobox", { name: "赛事 UI profile" })).toHaveValue(event.profile_id);
    expect(screen.getByRole("tab", { name: "留出评估" })).toHaveAttribute("aria-selected", "true");
    expect(within(screen.getByRole("region", { name: "最近评估" })).getByText("9 / 10")).toBeInTheDocument();
  });

  it("lists only matching Observation metadata without selecting a sequence", async () => {
    api.fetchVisionCalibration.mockResolvedValue({
      ...bootstrap,
      events: [{ ...event, label }],
      observation_files: [
        ...bootstrap.observation_files,
        {
          name: "38416111.jsonl",
          bytes: 1024,
          raybet_match_id: "38416111",
          display_name: "不匹配的比赛",
        },
      ],
    });
    renderPage();

    const sampleQueue = await screen.findByRole("region", { name: "Vision 校正样本" });
    expect(within(sampleQueue).getByRole("heading", { name: "校正队列" })).toBeInTheDocument();
    expect(within(sampleQueue).getByText("真实帧样本")).toBeInTheDocument();
    expect(within(sampleQueue).getByText(
      "官方 Match ID 8123456789 · Team A vs Team B · The International 2026",
    )).toBeInTheDocument();
    expect(within(sampleQueue).getByText("Map 1 · ambiguous_match")).toBeInTheDocument();
    expect(within(sampleQueue).getByText("已校正")).toBeInTheDocument();
    expect(within(sampleQueue).getByText("standard_dota_hud_1080p")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "留出评估" }));
    const selector = await screen.findByRole("combobox", { name: "Observation JSONL" });
    expect(selector).toHaveValue("");
    expect(screen.getByRole("option", {
      name: "官方 Match ID 8123456789 · Team A vs Team B · The International 2026",
    })).toHaveValue("holdout.jsonl");
    expect(screen.queryByRole("option", { name: "不匹配的比赛" })).not.toBeInTheDocument();
  });

  it("explains why Observation JSONL is unavailable when the corpus is empty", async () => {
    api.fetchVisionCalibration.mockResolvedValue({
      ...bootstrap,
      observation_files: [],
    });
    renderPage();

    fireEvent.click(await screen.findByRole("tab", { name: "留出评估" }));
    const selector = await screen.findByRole("combobox", { name: "Observation JSONL" });
    expect(selector).toBeDisabled();
    expect(screen.getByText("当前比赛没有可用序列")).toBeInTheDocument();
    expect(screen.getByText(/C:\\vision-corpus\\vision_observations/)).toBeInTheDocument();
  });

  it("requires ten unique heroes and saves the HUD-order truth with CSRF", async () => {
    renderPage();
    const save = await screen.findByRole("button", { name: "保存真实值标签" });
    expect(save).toBeDisabled();

    fireEvent.change(screen.getByLabelText("RayBet match ID"), { target: { value: "38417147" } });
    fireEvent.change(screen.getByLabelText("Map"), { target: { value: "2" } });
    expect(save).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "清除 Dire slot 5 英雄" }));
    expect(save).toBeDisabled();
    expect(screen.getByText("需要十个互不重复的英雄真实值。")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "选择 Dire slot 5 英雄" }));
    const picker = screen.getByRole("dialog", { name: "HUD 真实值英雄选择器" });
    expect(within(picker).getByRole("button", { name: "选择英雄 Hero 1" })).toBeDisabled();
    fireEvent.click(within(picker).getByRole("button", { name: "选择英雄 Hero 10" }));
    expect(save).toBeEnabled();
    api.saveVisionCalibrationLabel.mockResolvedValue({
      ...label,
      map_number: 2,
    });
    fireEvent.click(save);

    await waitFor(() => expect(api.saveVisionCalibrationLabel).toHaveBeenCalledWith(
      event.event_id,
      expect.objectContaining({
        hero_ids: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        raybet_match_id: "38417147",
        map_number: 2,
      }),
      "csrf",
    ));
  });

  it("disables evaluation for unsaved inputs or a mismatched layout", async () => {
    api.fetchVisionCalibration.mockResolvedValue({
      ...bootstrap,
      events: [{ ...event, label }],
      candidates: [candidate],
      layout_profiles: [event.profile_id, "wxc_gotf_2026_live_1080p"],
    });
    renderPage();

    fireEvent.click(await screen.findByRole("tab", { name: "留出评估" }));
    const run = await screen.findByRole("button", { name: "运行留出评估" });
    expect(run).toBeDisabled();

    fireEvent.change(screen.getByRole("combobox", { name: "Observation JSONL" }), {
      target: { value: "holdout.jsonl" },
    });
    expect(run).toBeEnabled();

    fireEvent.click(screen.getByRole("tab", { name: "真实值" }));
    fireEvent.change(screen.getByLabelText("Map"), { target: { value: "2" } });

    fireEvent.click(screen.getByRole("tab", { name: "留出评估" }));
    expect(screen.getByRole("button", { name: "运行留出评估" })).toBeDisabled();
    expect(screen.getByText("比赛、Map 或英雄真值有未保存的修改。")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "真实值" }));
    fireEvent.change(screen.getByLabelText("Map"), { target: { value: "1" } });

    fireEvent.click(screen.getByRole("tab", { name: "留出评估" }));
    fireEvent.change(screen.getByLabelText("Layout profile"), {
      target: { value: "wxc_gotf_2026_live_1080p" },
    });
    expect(screen.getByRole("button", { name: "运行留出评估" })).toBeDisabled();
    expect(screen.getByText("Layout profile 必须与真值标签一致。")).toBeInTheDocument();
  });

  it("builds only an isolated candidate and exposes lock-safety evaluation metrics", async () => {
    const staleEvaluation: VisionCalibrationEvaluation = {
      ...evaluation,
      evaluation_id: "stale-evaluation",
      observation_file: "38416111.jsonl",
      raybet_match_id: "38416111",
      map_number: 2,
    };
    api.fetchVisionCalibration.mockResolvedValue({
      ...bootstrap,
      events: [{ ...event, label }],
      profiles: [{
        profile_id: event.profile_id,
        layout: event.layout,
        event_count: 1,
        labeled_event_count: 1,
        candidate_count: 1,
        latest_captured_at: event.captured_at,
      }],
      candidates: [candidate],
      evaluations: [staleEvaluation, evaluation],
    });
    api.buildVisionCalibrationCandidate.mockResolvedValue(candidate);
    api.runVisionCalibrationEvaluation.mockResolvedValue(evaluation);
    renderPage();

    expect(await screen.findByText("生产边界已锁定")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "留出评估" }));
    const results = screen.getByRole("region", { name: "最近评估" });
    expect(within(results).getByText("9 / 10")).toBeInTheDocument();
    expect(within(results).getByText("正确锁定 / 总锁定")).toBeInTheDocument();
    expect(within(results).getByText("错误锁定")).toBeInTheDocument();
    expect(within(results).getByText("Map 1")).toBeInTheDocument();
    expect(within(results).getByText("锁定延迟 4.2 秒")).toBeInTheDocument();
    expect(screen.queryByText("38416111.jsonl")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "候选模板" }));
    fireEvent.click(screen.getByRole("button", { name: "从当前标签构建候选" }));
    await waitFor(() => expect(api.buildVisionCalibrationCandidate).toHaveBeenCalledWith(
      event.event_id,
      "csrf",
    ));

    fireEvent.click(screen.getByRole("tab", { name: "留出评估" }));
    fireEvent.change(screen.getByRole("combobox", { name: "Observation JSONL" }), {
      target: { value: "holdout.jsonl" },
    });
    fireEvent.click(screen.getByRole("button", { name: "运行留出评估" }));
    await waitFor(() => expect(api.runVisionCalibrationEvaluation).toHaveBeenCalledWith(
      expect.objectContaining({
        label_id: event.event_id,
        candidate_id: candidate.candidate_id,
        observation_file: "holdout.jsonl",
      }),
      "csrf",
    ));
  });
});
