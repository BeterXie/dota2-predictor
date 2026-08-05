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

  it("only exposes prospective probabilities when the runtime is ready", async () => {
    const currentModel = model("prospective", "passed", true);
    modelsMock.mockResolvedValueOnce({
      data: [currentModel],
      pagination: { page: 1, page_size: 100, total: 1, total_pages: 1 },
    });
    const currentPrediction = prediction("prospective", "predicted");
    predictionsMock.mockResolvedValueOnce({ data: [currentPrediction], pagination });

    renderView();
    expect((await screen.findAllByText("运行就绪")).length).toBeGreaterThanOrEqual(1);
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

  it("shows C0-C9 structure without applying it to a no-cluster model", async () => {
    predictionsMock.mockResolvedValueOnce({
      data: [withClusterAnalysis(prediction("reconstructed_walk_forward", "predicted"))],
      pagination,
    });

    renderView();
    const evidence = await screen.findByRole("complementary", { name: /比赛 9001 预测证据/ });
    expect(within(evidence).getByText("C0–C9 阵容结构")).toBeInTheDocument();
    expect(within(evidence).getByText(
      "Cluster 是结构修正特征，不是独立胜率，也不是固定权重。",
    )).toBeInTheDocument();
    expect(within(evidence).getByText("80.0% / 320")).toBeInTheDocument();
    expect(within(evidence).getByText("noxville-clusters-7.41-v1")).toBeInTheDocument();
    expect(within(evidence).getByText("当前模型未采用")).toBeInTheDocument();
    expect(within(evidence).getByText(/当前模型为无 Cluster 版本.*最终概率沿用无 Cluster 模型/)).toBeInTheDocument();
    const structure = within(evidence).getByRole("table", { name: "C0-C9 阵营计数" });
    expect(within(structure).getByRole("columnheader", { name: "C9" })).toBeInTheDocument();
    expect(within(structure).getByRole("rowheader", { name: "Radiant" })).toBeInTheDocument();
  });

  it("labels the cluster model and its learned logit correction as applied", async () => {
    const clusterModel = model("reconstructed_walk_forward", "reconstructed_only", false);
    clusterModel.model_kind = "team_plus_draft_rosh_clusters";
    const clusterPrediction = withClusterAnalysis(
      prediction("reconstructed_walk_forward", "predicted"),
    );
    clusterPrediction.model_kind = "team_plus_draft_rosh_clusters";
    clusterPrediction.cluster_logit_delta = 0.032;
    modelsMock.mockResolvedValueOnce({
      data: [clusterModel],
      pagination: { page: 1, page_size: 100, total: 1, total_pages: 1 },
    });
    predictionsMock.mockResolvedValueOnce({ data: [clusterPrediction], pagination });

    renderView();
    const evidence = await screen.findByRole("complementary", { name: /比赛 9001 预测证据/ });
    expect(within(evidence).getByText("球队 + Draft + R.O.S.H. + Cluster")).toBeInTheDocument();
    expect(within(evidence).getByText("+0.032 logit")).toBeInTheDocument();
    expect(within(evidence).getByText("当前模型已采用")).toBeInTheDocument();
    expect(within(evidence).queryByText(/最终概率沿用无 Cluster 模型/)).not.toBeInTheDocument();
  });

  it("fails closed for a failed calibration even when point estimates exist", async () => {
    modelsMock.mockResolvedValueOnce({
      data: [model("prospective", "failed", false)],
      pagination: { page: 1, page_size: 100, total: 1, total_pages: 1 },
    });
    const failedPrediction = prediction("prospective", "predicted");
    failedPrediction.calibration_status = "failed";
    failedPrediction.runtime_ready = false;
    failedPrediction.runtime_block_reason = "prospective deployment calibration is not passed";
    failedPrediction.deployment_key = null;
    predictionsMock.mockResolvedValueOnce({
      data: [failedPrediction],
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
  runtime_ready: boolean,
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
    runtime_ready,
    runtime_block_reason: runtime_ready ? null : "deployment_not_configured",
    deployment_key: runtime_ready ? "z".repeat(64) : null,
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
  const runtime_ready = availability_mode === "prospective";
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
    calibration_status: availability_mode === "prospective" ? "passed" : "reconstructed_only",
    team_base_probability: 0.625,
    raw_probability: 0.625,
    calibrated_probability: 0.571,
    parameter_uncertainty: 0.012,
    draft_logit_delta: 0.11,
    rosh_logit_delta: -0.04,
    cluster_logit_delta: null,
    cluster_coverage: 0,
    cluster_support: 0,
    cluster_resource_version: null,
    cluster_evidence_mode: null,
    cluster_missing_reason: "cluster_evidence_unavailable",
    cluster_counts: {},
    cluster_assignments: { radiant: [], dire: [] },
    top_cluster_contributions: [],
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
    runtime_ready,
    runtime_block_reason: runtime_ready ? null : "deployment_not_configured",
    deployment_key: runtime_ready ? "z".repeat(64) : null,
  };
}

function withClusterAnalysis(value: PrematchPrediction): PrematchPrediction {
  return {
    ...value,
    cluster_coverage: 0.8,
    cluster_support: 320,
    cluster_resource_version: "noxville-clusters-7.41-v1",
    cluster_evidence_mode: "reconstructed_walk_forward",
    cluster_missing_reason: null,
    cluster_counts: {
      C0: { radiant: 2, dire: 0, difference: 2 },
      C1: { radiant: 1, dire: 1, difference: 0 },
      C2: { radiant: 1, dire: 0, difference: 1 },
      C3: { radiant: 0, dire: 1, difference: -1 },
      C4: { radiant: 0, dire: 1, difference: -1 },
      C5: { radiant: 1, dire: 0, difference: 1 },
      C6: { radiant: 0, dire: 1, difference: -1 },
      C7: { radiant: 0, dire: 0, difference: 0 },
      C8: { radiant: 0, dire: 1, difference: -1 },
      C9: { radiant: 0, dire: 0, difference: 0 },
    },
  };
}
