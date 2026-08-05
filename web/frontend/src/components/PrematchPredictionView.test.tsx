import { FluentProvider, webDarkTheme } from "@fluentui/react-components";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  fetchPrematchMatchPredictions,
  fetchPrematchPredictionModels,
  fetchPrematchPredictions,
} from "../api";
import type {
  PrematchModelSummary,
  PrematchPrediction,
  PrematchPredictionPage,
} from "../types";
import { PrematchPredictionView, prematchPredictionViewInternals } from "./PrematchPredictionView";

vi.mock("../api", () => ({
  fetchPrematchMatchPredictions: vi.fn(),
  fetchPrematchPredictionModels: vi.fn(),
  fetchPrematchPredictions: vi.fn(),
}));

const modelsMock = vi.mocked(fetchPrematchPredictionModels);
const predictionsMock = vi.mocked(fetchPrematchPredictions);
const matchMock = vi.mocked(fetchPrematchMatchPredictions);

const pagination = { page: 1, page_size: 20, total: 1, total_pages: 1 };

beforeEach(() => {
  vi.clearAllMocks();
  modelsMock.mockResolvedValue({
    data: [model("reconstructed_walk_forward", "reconstructed_only", false)],
    pagination: { page: 1, page_size: 100, total: 1, total_pages: 1 },
  });
  predictionsMock.mockResolvedValue({
    data: [prediction("reconstructed_walk_forward", "predicted")],
    pagination,
  });
  matchMock.mockResolvedValue({ match_id: 9001, predictions: [] });
});

afterEach(() => cleanup());

function renderView() {
  return render(
    <FluentProvider theme={webDarkTheme}>
      <PrematchPredictionView />
    </FluentProvider>,
  );
}

describe("PrematchPredictionView", () => {
  it("shows the required components while fail-closed for reconstructed data", async () => {
    renderView();

    expect(await screen.findByText("球队基础概率")).toBeInTheDocument();
    expect(screen.getByText("Draft residual 修正")).toBeInTheDocument();
    expect(screen.getByText("官方 R.O.S.H. 指标")).toBeInTheDocument();
    expect(screen.getByText("R.O.S.H. logit 修正")).toBeInTheDocument();
    expect(screen.getByText("原始概率")).toBeInTheDocument();
    expect(screen.getByText("校准概率")).toBeInTheDocument();
    expect(screen.getByText("parameter uncertainty")).toBeInTheDocument();
    expect(screen.getByText("coverage / support")).toBeInTheDocument();
    expect(screen.getByText("模型状态")).toBeInTheDocument();
    expect(screen.getByText("availability mode")).toBeInTheDocument();
    expect(screen.getAllByText("仅历史重建").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("未提供")).toBeInTheDocument();
    expect(screen.getAllByText("62.5%").length).toBeGreaterThanOrEqual(1);
  });

  it("only exposes prospective probabilities with a passed, authorized model", async () => {
    const currentModel = model("prospective", "passed", true);
    modelsMock.mockResolvedValueOnce({
      data: [currentModel],
      pagination: { page: 1, page_size: 100, total: 1, total_pages: 1 },
    });
    const currentPrediction = prediction("prospective", "predicted");
    predictionsMock.mockResolvedValueOnce({ data: [currentPrediction], pagination });

    renderView();
    expect((await screen.findAllByText("运行已授权")).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("62.5%").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("57.1%")).toBeInTheDocument();
    expect(screen.getAllByText("前瞻采集").length).toBeGreaterThanOrEqual(1);

    fireEvent.click(screen.getByRole("row", { name: /#9001/ }));
    await waitFor(() => expect(matchMock).toHaveBeenCalledWith(9001));
  });

  it("does not invent an official R.O.S.H. metric when the API omits it", async () => {
    renderView();
    const evidence = await screen.findByRole("complementary", { name: /比赛 9001 预测证据/ });
    expect(within(evidence).getByText("官方 R.O.S.H. 指标")).toBeInTheDocument();
    expect(within(evidence).getByText("未提供")).toBeInTheDocument();
  });

  it("fails closed for a failed calibration even when point estimates exist", async () => {
    modelsMock.mockResolvedValueOnce({
      data: [model("prospective", "failed", true)],
      pagination: { page: 1, page_size: 100, total: 1, total_pages: 1 },
    });
    predictionsMock.mockResolvedValueOnce({
      data: [prediction("prospective", "predicted")],
      pagination,
    });

    renderView();
    expect((await screen.findAllByText("校准未通过")).length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText("62.5%")).not.toBeInTheDocument();
  });
});

function model(
  availability_mode: "reconstructed_walk_forward" | "prospective",
  calibrationStatus: string,
  runtime_authorized: boolean,
): PrematchModelSummary {
  return {
    run_id: "r".repeat(64),
    model_hash: "m".repeat(64),
    model_version: "prematch-offset-logistic-l2-v1",
    artifact_version: "prematch-model-artifact-v1",
    model_kind: "team_plus_draft_rosh",
    availability_mode,
    training_cutoff: "2026-07-01T00:00:00Z",
    feature_schema_hash: "f".repeat(64),
    training_input_hash: "i".repeat(64),
    metrics: null,
    status: "trained",
    created_at: "2026-07-02T00:00:00Z",
    runtime_authorized,
    calibration: {
      calibration_hash: "c".repeat(64),
      calibration_version: "prematch-platt-v1",
      fit_cutoff: "2026-07-01T00:00:00Z",
      evaluation_cutoff: "2026-07-02T00:00:00Z",
      fit_support: 100,
      evaluation_support: 100,
      parameters: { a: 0.1, b: 0.9 },
      metrics: null,
      status: calibrationStatus,
      gate_passed: calibrationStatus === "passed",
      created_at: "2026-07-02T00:00:00Z",
    },
  };
}

function prediction(
  availability_mode: "reconstructed_walk_forward" | "prospective",
  status: "predicted" | "failed",
): PrematchPrediction {
  return {
    run_id: "r".repeat(64),
    model_hash: "m".repeat(64),
    model_kind: "team_plus_draft_rosh",
    model_status: "trained",
    availability_mode,
    training_cutoff: "2026-07-01T00:00:00Z",
    match_id: 9001,
    prediction_cutoff: "2026-07-03T00:00:00Z",
    cutoff_source: "completed_at",
    input_snapshot_hash: "s".repeat(64),
    artifact_fingerprint: "a".repeat(64),
    dependency_fingerprint: "d".repeat(64),
    dependency_revision: 2,
    calibration_hash: "c".repeat(64),
    team_base_probability: 0.625,
    raw_probability: 0.625,
    calibrated_probability: 0.571,
    parameter_uncertainty: 0.012,
    draft_logit_delta: 0.11,
    rosh_logit_delta: -0.04,
    cluster_logit_delta: null,
    total_adjustment: 0.07,
    coverage: 0.84,
    support: 606,
    eventual_radiant_win: null,
    result_usable_at: null,
    settled_at: null,
    status,
    reason: status === "failed" ? "prediction_failed" : null,
    learned_intercept: 0.02,
    missing_features: [],
    top_contributions: [],
    validation: {
      validation_version: "prematch-input-lineage-v1",
      validated_at: "2026-07-03T00:01:00Z",
    },
  };
}
