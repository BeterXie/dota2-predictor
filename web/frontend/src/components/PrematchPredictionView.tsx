import { Button, Select, Spinner } from "@fluentui/react-components";
import {
  ArrowClockwise,
  CaretLeft,
  CaretRight,
  ChartLineUp,
  Database,
  WarningCircle,
} from "@phosphor-icons/react";
import { useEffect, useMemo, useState } from "react";

import {
  fetchPrematchMatchPredictions,
  fetchPrematchPredictionModels,
  fetchPrematchPredictions,
} from "../api";
import type {
  PrematchAvailabilityMode,
  PrematchModelSummary,
  PrematchPrediction,
  PrematchPredictionPage,
  PrematchRoshMetrics,
} from "../types";
import "./PrematchPredictionView.css";

const PAGE_SIZE = 20;

const CALIBRATION_LABELS: Record<string, string> = {
  unsupported: "不支持",
  failed: "校准未通过",
  provisional: "样本暂定",
  reconstructed_only: "仅历史重建",
  shadow_collecting: "前瞻积累中",
  passed: "校准通过",
};

const MODEL_LABELS: Record<string, string> = {
  team_only: "球队基础",
  team_plus_draft: "球队 + Draft",
  team_plus_rosh: "球队 + R.O.S.H.",
  team_plus_draft_rosh: "球队 + Draft + R.O.S.H.",
};

const PREDICTION_STATUS_LABELS: Record<string, string> = {
  predicted: "已生成",
  settled: "已结算",
  insufficient_evidence: "证据不足",
  unavailable: "不可用",
  failed: "失败",
};

const AVAILABILITY_LABELS: Record<PrematchAvailabilityMode, string> = {
  reconstructed_walk_forward: "历史重建",
  prospective: "前瞻采集",
};

interface GateResult {
  /** Existing, current-lineage numbers may be shown as evidence. */
  evidenceAllowed: boolean;
  /** Only a prospective, explicitly authorized deployment may be applied. */
  runtimeAuthorized: boolean;
  label: string;
}

export function PrematchPredictionView() {
  const [modelKind, setModelKind] = useState("");
  const [availabilityMode, setAvailabilityMode] = useState<PrematchAvailabilityMode | "">("");
  const [status, setStatus] = useState("");
  const [pageNumber, setPageNumber] = useState(1);
  const [models, setModels] = useState<PrematchModelSummary[]>([]);
  const [page, setPage] = useState<PrematchPredictionPage | null>(null);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [selectedDetail, setSelectedDetail] = useState<PrematchPrediction | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    setSelectedDetail(null);
    const options = {
      page: pageNumber,
      pageSize: PAGE_SIZE,
      modelKind: modelKind || undefined,
      availabilityMode: availabilityMode || undefined,
      status: status || undefined,
    };
    Promise.all([
      fetchPrematchPredictionModels({
        page: 1,
        pageSize: 100,
        modelKind: modelKind || undefined,
        availabilityMode: availabilityMode || undefined,
      }, controller.signal),
      fetchPrematchPredictions(options, controller.signal),
    ])
      .then(([modelPage, predictionPage]) => {
        if (controller.signal.aborted) return;
        setModels(modelPage.data || []);
        setPage(predictionPage);
        setSelectedKey((current) => {
          if (current && predictionPage.data.some((row) => predictionKey(row) === current)) {
            return current;
          }
          return predictionPage.data[0] ? predictionKey(predictionPage.data[0]) : null;
        });
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setError(errorMessage(reason, "无法读取冻结赛前预测"));
          setPage(null);
          setModels([]);
          setSelectedKey(null);
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [availabilityMode, modelKind, pageNumber, refresh, status]);

  const modelKinds = useMemo(
    () => Array.from(new Set(models.map((model) => model.model_kind))).sort(),
    [models],
  );

  const selectedRow = page?.data.find((row) => predictionKey(row) === selectedKey) || null;
  const selected = selectedDetail || selectedRow;
  const selectedModel = selected ? findModel(models, selected) : null;
  const selectedGate = selected ? predictionGate(selected, selectedModel) : null;

  const selectPrediction = (prediction: PrematchPrediction) => {
    setSelectedKey(predictionKey(prediction));
    setSelectedDetail(null);
    setDetailError(null);
    setDetailLoading(true);
    fetchPrematchMatchPredictions(prediction.match_id)
      .then((value) => {
        const exact = value.predictions.find((row) => predictionKey(row) === predictionKey(prediction));
        setSelectedDetail(exact || value.predictions[0] || prediction);
      })
      .catch((reason: unknown) => {
        // The list row is already lineage-filtered; keep it visible if the
        // optional detail endpoint is temporarily unavailable.
        setSelectedDetail(prediction);
        setDetailError(errorMessage(reason, "无法读取该比赛的完整预测证据"));
      })
      .finally(() => setDetailLoading(false));
  };

  return (
    <section className="prematch-prediction-view" aria-label="冻结赛前预测">
      <header className="prematch-prediction-heading">
        <div>
          <span className="prematch-prediction-eyebrow">FROZEN PREMATCH DEPLOYMENT</span>
          <h2><ChartLineUp size={20} aria-hidden="true" />赛前预测</h2>
          <p>当前冻结部署的赛前模型状态与证据。</p>
        </div>
        <Button
          appearance="subtle"
          aria-label="刷新赛前预测"
          icon={<ArrowClockwise size={17} />}
          onClick={() => setRefresh((value) => value + 1)}
        />
      </header>

      <div className="prematch-prediction-filters" aria-label="赛前预测筛选">
        <label>
          <span>模型</span>
          <Select aria-label="赛前预测模型" value={modelKind} onChange={(_, data) => {
            setModelKind(data.value);
            setPageNumber(1);
          }}>
            <option value="">全部模型</option>
            {modelKinds.map((kind) => <option key={kind} value={kind}>{modelLabel(kind)}</option>)}
          </Select>
        </label>
        <label>
          <span>数据模式</span>
          <Select
            aria-label="赛前预测数据模式"
            value={availabilityMode}
            onChange={(_, data) => {
              setAvailabilityMode(data.value as PrematchAvailabilityMode | "");
              setPageNumber(1);
            }}
          >
            <option value="">全部模式</option>
            <option value="reconstructed_walk_forward">历史重建</option>
            <option value="prospective">前瞻采集</option>
          </Select>
        </label>
        <label>
          <span>预测状态</span>
          <Select aria-label="赛前预测状态" value={status} onChange={(_, data) => {
            setStatus(data.value);
            setPageNumber(1);
          }}>
            <option value="">全部状态</option>
            {Object.entries(PREDICTION_STATUS_LABELS).map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </Select>
        </label>
      </div>

      {error && (
        <div className="prematch-prediction-notice error" role="alert">
          <WarningCircle size={18} aria-hidden="true" />
          <div><strong>赛前预测暂不可用</strong><span>{error}</span></div>
        </div>
      )}
      {loading && !page && <div className="prematch-prediction-loading" role="status"><Spinner label="正在读取赛前预测" /></div>}
      {!loading && page && page.data.length === 0 && (
        <div className="prematch-prediction-empty">
          <Database size={21} aria-hidden="true" />
          <strong>没有符合当前筛选的预测</strong>
          <span>当前筛选无有效血缘记录。</span>
        </div>
      )}

      {page && page.data.length > 0 && (
        <div className="prematch-prediction-layout">
          <div className="prematch-prediction-table-wrap">
            <div className="prematch-prediction-table" role="table" aria-label="赛前预测列表">
              <div className="prematch-prediction-table-head" role="row">
                <span>比赛 / 截止</span>
                <span>模型</span>
                <span>球队基础</span>
                <span>原始 / 校准</span>
                <span>状态</span>
              </div>
              {page.data.map((prediction) => {
                const rowModel = findModel(models, prediction);
                const gate = predictionGate(prediction, rowModel);
                const selectedRowKey = predictionKey(prediction);
                return (
                  <button
                    className={`prematch-prediction-row${selectedRowKey === selectedKey ? " selected" : ""}`}
                    key={selectedRowKey}
                    onClick={() => selectPrediction(prediction)}
                    role="row"
                    type="button"
                  >
                    <span role="cell"><strong>#{prediction.match_id}</strong><small>{shortDate(prediction.prediction_cutoff)}</small><small>{prediction.settled_at ? "已结算" : "未结算"}</small></span>
                    <span role="cell"><strong>{modelLabel(prediction.model_kind)}</strong><small>{availabilityLabel(prediction.availability_mode)}</small></span>
                    <span role="cell" className="prematch-prediction-number">{formatProbability(prediction.team_base_probability, gate.evidenceAllowed)}</span>
                    <span role="cell" className="prematch-prediction-number">{formatProbability(prediction.raw_probability, gate.evidenceAllowed)} <small>/ {formatProbability(prediction.calibrated_probability, gate.evidenceAllowed)}</small></span>
                    <span role="cell"><span className={`prematch-prediction-status ${gate.runtimeAuthorized ? "allowed" : "blocked"}`}>{gate.label}</span></span>
                  </button>
                );
              })}
            </div>
            <div className="prematch-prediction-pagination">
              <Button
                appearance="subtle"
                aria-label="上一页赛前预测"
                disabled={page.pagination.page <= 1 || loading}
                icon={<CaretLeft size={15} />}
                onClick={() => setPageNumber((value) => Math.max(1, value - 1))}
                title="上一页"
              />
              <span>共 {page.pagination.total} 条 · 第 {page.pagination.page}/{page.pagination.total_pages} 页</span>
              <Button
                appearance="subtle"
                aria-label="下一页赛前预测"
                disabled={page.pagination.page >= page.pagination.total_pages || loading}
                icon={<CaretRight size={15} />}
                onClick={() => setPageNumber((value) => value + 1)}
                title="下一页"
              />
            </div>
          </div>
          {selected && (
            <PredictionEvidence
              detailError={detailError}
              loading={detailLoading}
              model={selectedModel}
              prediction={selected}
              gate={selectedGate || predictionGate(selected, selectedModel)}
            />
          )}
        </div>
      )}
    </section>
  );
}

function PredictionEvidence({
  detailError,
  loading,
  model,
  prediction,
  gate,
}: {
  detailError: string | null;
  loading: boolean;
  model: PrematchModelSummary | null;
  prediction: PrematchPrediction;
  gate: GateResult;
}) {
  const rosh = prediction.rosh_metrics || prediction.rosh_features || null;
  return (
    <aside className="prematch-prediction-evidence" aria-label={`比赛 ${prediction.match_id} 预测证据`}>
      <header>
        <div><span>比赛 #{prediction.match_id}</span><h3>{modelLabel(prediction.model_kind)}</h3></div>
        {loading && <Spinner size="tiny" label="正在读取详情" />}
      </header>
      {detailError && <p className="prematch-prediction-detail-note">{detailError}</p>}
      <div className={`prematch-prediction-gate ${gate.runtimeAuthorized ? "allowed" : "blocked"}`}>
        <strong>{gate.label}</strong>
        <span>{gate.evidenceAllowed
          ? gate.runtimeAuthorized
            ? "当前记录满足展示条件"
            : "历史证据可查看，但当前运行未获授权"
          : "当前状态禁止运行授权"}</span>
      </div>
      <dl className="prematch-prediction-meta">
        <Metric label="模型状态" value={modelStatusLabel(prediction.model_status)} />
        <Metric label="availability mode" value={availabilityLabel(prediction.availability_mode)} />
        <Metric label="校准状态" value={calibrationLabel(model?.calibration?.status || prediction.calibration_status)} />
        <Metric label="prediction cutoff" value={shortDate(prediction.prediction_cutoff)} />
        <Metric label="settled" value={prediction.settled_at ? "已结算" : "未结算"} />
      </dl>
      <div className="prematch-prediction-components">
        <ComponentMetric label="球队基础概率" value={formatProbability(prediction.team_base_probability, gate.evidenceAllowed)} />
        <ComponentMetric label="Draft residual 修正" value={formatLogit(prediction.draft_logit_delta, gate.evidenceAllowed)} />
        <ComponentMetric label="官方 R.O.S.H. 指标" value={formatRoshMetric(rosh, gate.evidenceAllowed)} />
        <ComponentMetric label="R.O.S.H. logit 修正" value={formatLogit(prediction.rosh_logit_delta, gate.evidenceAllowed)} />
        <ComponentMetric label="原始概率" value={formatProbability(prediction.raw_probability, gate.evidenceAllowed)} />
        <ComponentMetric label="校准概率" value={formatProbability(prediction.calibrated_probability, gate.evidenceAllowed)} />
        <ComponentMetric label="parameter uncertainty" value={formatUncertainty(prediction.parameter_uncertainty, gate.evidenceAllowed)} />
        <ComponentMetric label="coverage / support" value={formatCoverageSupport(prediction.coverage, prediction.support)} />
      </div>
      <footer className="prematch-prediction-evidence-footer">
        <span>截断来源：{prediction.cutoff_source}</span>
        <span>血缘修订：{prediction.dependency_revision}</span>
        {prediction.reason && <code>{prediction.reason}</code>}
      </footer>
    </aside>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div><dt>{label}</dt><dd>{value}</dd></div>;
}

function ComponentMetric({ label, value }: { label: string; value: string }) {
  return <div><span>{label}</span><strong>{value}</strong></div>;
}

function predictionGate(
  prediction: PrematchPrediction,
  model: PrematchModelSummary | null,
): GateResult {
  const modelReady = Boolean(
    model
      && (model.status === "trained" || model.status === "passed")
      && prediction.model_status === "trained",
  );
  if (!modelReady) {
    return { evidenceAllowed: false, runtimeAuthorized: false, label: modelStatusLabel(prediction.model_status) };
  }
  if (prediction.status !== "predicted" && prediction.status !== "settled") {
    return { evidenceAllowed: false, runtimeAuthorized: false, label: predictionStatusLabel(prediction.status) };
  }
  if (!prediction.validation) {
    return { evidenceAllowed: false, runtimeAuthorized: false, label: "缺少验证记录" };
  }
  if (prediction.raw_probability == null || !Number.isFinite(prediction.raw_probability)) {
    return { evidenceAllowed: false, runtimeAuthorized: false, label: "概率不可用" };
  }

  const calibrationStatus = model?.calibration?.status || prediction.calibration_status;
  const calibrationGatePassed = model?.calibration?.gate_passed === true;
  if (prediction.availability_mode === "prospective" && model?.runtime_authorized !== true) {
    return { evidenceAllowed: false, runtimeAuthorized: false, label: "运行未授权" };
  }
  const runtimeAuthorized = prediction.availability_mode === "prospective"
    && model?.runtime_authorized === true
    && calibrationStatus === "passed"
    && calibrationGatePassed
    && prediction.calibrated_probability != null
    && Number.isFinite(prediction.calibrated_probability);
  if (runtimeAuthorized) {
    return { evidenceAllowed: true, runtimeAuthorized: true, label: "运行已授权" };
  }
  if (prediction.availability_mode === "reconstructed_walk_forward") {
    return { evidenceAllowed: true, runtimeAuthorized: false, label: "仅历史重建" };
  }
  if (!calibrationStatus) {
    return { evidenceAllowed: false, runtimeAuthorized: false, label: "缺少校准状态" };
  }
  if (calibrationStatus === "passed" && !calibrationGatePassed) {
    return { evidenceAllowed: false, runtimeAuthorized: false, label: "缺少校准门槛证明" };
  }
  return { evidenceAllowed: false, runtimeAuthorized: false, label: calibrationLabel(calibrationStatus) };
}

function findModel(models: PrematchModelSummary[], prediction: PrematchPrediction): PrematchModelSummary | null {
  return models.find((model) => model.model_hash === prediction.model_hash) || null;
}

function predictionKey(prediction: PrematchPrediction): string {
  return `${prediction.match_id}:${prediction.model_hash}:${prediction.availability_mode}`;
}

function modelLabel(kind: string): string {
  return MODEL_LABELS[kind] || kind;
}

function availabilityLabel(mode: PrematchAvailabilityMode): string {
  return AVAILABILITY_LABELS[mode] || mode;
}

function calibrationLabel(status: string | null | undefined): string {
  return status ? (CALIBRATION_LABELS[status] || status) : "未提供";
}

function modelStatusLabel(status: string): string {
  if (status === "trained") return "已训练";
  if (status === "insufficient_evidence") return "证据不足";
  return status || "未知";
}

function predictionStatusLabel(status: string): string {
  if (status === "predicted") return "已生成";
  if (status === "settled") return "已结算";
  if (status === "insufficient_evidence") return "证据不足";
  if (status === "unavailable") return "不可用";
  if (status === "failed") return "失败";
  return status || "未知";
}

function formatProbability(value: number | null, allowed: boolean): string {
  if (!allowed || value == null || !Number.isFinite(value)) return "不可用";
  return `${(value * 100).toFixed(1)}%`;
}

function formatLogit(value: number | null, allowed: boolean): string {
  if (!allowed || value == null || !Number.isFinite(value)) return "不可用";
  return `${value >= 0 ? "+" : ""}${value.toFixed(3)} logit`;
}

function formatUncertainty(value: number | null, allowed: boolean): string {
  if (!allowed || value == null || !Number.isFinite(value)) return "不可用";
  return `±${(value * 100).toFixed(2)} pp`;
}

function formatCoverageSupport(coverage: number | null, support: number | null): string {
  const coverageText = coverage == null || !Number.isFinite(coverage)
    ? "未提供"
    : `${(coverage * 100).toFixed(1)}%`;
  return `${coverageText} / ${support == null ? "未提供" : support}`;
}

function formatRoshMetric(metrics: PrematchRoshMetrics | null, allowed: boolean): string {
  if (!allowed) return "不可用";
  if (!metrics) return "未提供";
  const value = metrics.relative_advantage
    ?? metrics.score_40
    ?? metrics.score_30
    ?? metrics.score_20;
  if (value == null || !Number.isFinite(value)) return "不可用";
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)} 分`;
}

function shortDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

export const prematchPredictionViewInternals = {
  predictionGate,
  formatProbability,
  formatRoshMetric,
};
