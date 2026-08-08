import { FluentProvider, webDarkTheme } from "@fluentui/react-components";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { VisionCalibrationBootstrap, VisionCalibrationEvent } from "../types";

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
  observation_files: [{ name: "holdout.jsonl", bytes: 2048 }],
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

    expect(await screen.findByAltText("选中 Vision debug event 的完整直播帧")).toHaveAttribute(
      "src",
      event.frame_url,
    );
    expect(screen.getAllByAltText(/英雄 crop$/)).toHaveLength(10);
    expect(screen.getByText("R1")).toBeInTheDocument();
    expect(screen.getByText("D5")).toBeInTheDocument();
  });

  it("explains why Observation JSONL is unavailable when the corpus is empty", async () => {
    api.fetchVisionCalibration.mockResolvedValue({
      ...bootstrap,
      observation_files: [],
    });
    renderPage();

    const selector = await screen.findByRole("combobox", { name: "Observation JSONL" });
    expect(selector).toBeDisabled();
    expect(screen.getByText("没有可用序列")).toBeInTheDocument();
    expect(screen.getByText(/C:\\vision-corpus\\vision_observations/)).toBeInTheDocument();
  });

  it("requires ten unique heroes and saves the HUD-order truth with CSRF", async () => {
    renderPage();
    const save = await screen.findByRole("button", { name: "保存真值标签" });
    expect(save).toBeEnabled();

    fireEvent.change(screen.getByLabelText("Dire slot 5"), { target: { value: "1" } });
    expect(save).toBeDisabled();
    expect(screen.getByText("需要十个互不重复的英雄真值。")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Dire slot 5"), { target: { value: "10" } });
    api.saveVisionCalibrationLabel.mockResolvedValue({
      label_id: event.event_id,
      event_id: event.event_id,
      event_relative_path: event.relative_path,
      layout: event.layout,
      profile_id: event.profile_id,
      hero_ids: Array.from({ length: 10 }, (_, index) => index + 1),
      raybet_match_id: null,
      map_number: null,
      note: null,
      updated_at: "2026-08-08T12:10:00+00:00",
    });
    fireEvent.click(save);

    await waitFor(() => expect(api.saveVisionCalibrationLabel).toHaveBeenCalledWith(
      event.event_id,
      expect.objectContaining({ hero_ids: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10] }),
      "csrf",
    ));
  });

  it("builds only an isolated candidate and exposes lock-safety evaluation metrics", async () => {
    const label = {
      label_id: event.event_id,
      event_id: event.event_id,
      event_relative_path: event.relative_path,
      layout: event.layout,
      profile_id: event.profile_id,
      hero_ids: Array.from({ length: 10 }, (_, index) => index + 1),
      raybet_match_id: "42",
      map_number: 1,
      note: null,
      updated_at: "2026-08-08T12:10:00+00:00",
    };
    const candidate = {
      candidate_id: `candidate-${event.event_id}-deadbeef`,
      label_id: "source-event-from-same-profile",
      layout: event.layout,
      profile_id: event.profile_id,
      hero_ids: label.hero_ids,
      created_at: "2026-08-08T12:11:00+00:00",
      feature_sha256: "a".repeat(64),
      production_feature_sha256: "b".repeat(64),
      promoted: false as const,
    };
    const evaluation = {
      evaluation_id: "evaluation-1",
      label_id: event.event_id,
      candidate_id: candidate.candidate_id,
      observation_file: "holdout.jsonl",
      layout_profile: "standard_dota_hud_1080p",
      mode: "perception" as const,
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
    api.buildVisionCalibrationCandidate.mockResolvedValue(candidate);
    api.runVisionCalibrationEvaluation.mockResolvedValue(evaluation);
    renderPage();

    expect(await screen.findByText("生产边界锁定")).toBeInTheDocument();
    expect(screen.getByText("wrong locks")).toBeInTheDocument();
    expect(screen.getByText("1")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "从当前标签构建候选" }));
    await waitFor(() => expect(api.buildVisionCalibrationCandidate).toHaveBeenCalledWith(
      event.event_id,
      "csrf",
    ));

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
