import { Button, Skeleton, SkeletonItem } from "@fluentui/react-components";
import { CheckCircle, Database, WarningCircle } from "@phosphor-icons/react";
import type {
  IntelligenceDraftQualitySlice,
  IntelligenceOverview,
  IntelligenceStateLabel,
} from "../../types";
import {
  availabilityStatus,
  qualitySlices,
} from "../../utils/intelligenceUtils";

const STATE_LABELS: Record<IntelligenceStateLabel, string> = {
  comeback: "翻盘局",
  throw: "被翻盘局",
  stomp: "碾压局",
  stomp_loss: "被碾压局",
  advantage: "优势局",
  disadvantage: "劣势局",
  even: "均势局",
  state_unscorable: "局势数据不足",
};

const COVERAGE_LABELS: Record<string, string> = {
  formal_maps: "正式地图",
  scored_matches: "含选手评分比赛",
  player_score_rows: "选手逐局评分",
  scored_players: "已识别选手",
  ranking_eligible_scores: "可排名评分",
  state_labeled_matches: "已分类比赛",
  team_state_rows: "队伍视角标签",
  profiled_teams: "球队画像",
  team_profiles: "球队画像版本行",
  draft_predicted_matches: "阵容预测比赛",
  draft_prediction_rows: "阵容预测切片",
};

function decimal(value: number | null | undefined, precision = 2): string {
  if (value == null) return "-";
  return value.toFixed(precision);
}

function integer(value: number | null | undefined): string {
  if (value == null) return "-";
  return Math.round(value).toLocaleString("zh-CN");
}

function versionLabel(name: string): string {
  return {
    player_score: "选手评分",
    team_state: "局势标签",
    team_profile: "球队画像",
    draft_score: "阵容输入",
    draft_model: "阵容模型",
    draft_backtest: "阵容回测",
    draft_features: "阵容特征",
  }[name] || name;
}

function modelKindLabel(kind: string): string {
  return kind === "pure_draft" ? "纯阵容" : kind === "context_adjusted" ? "上下文修正" : kind;
}

function availabilityLabel(mode: string): string {
  return mode === "prospective" ? "真实前瞻" : "历史重建";
}

function qualityStatusLabel(status: IntelligenceDraftQualitySlice["status"]): string {
  return {
    passed: "通过",
    failed: "未通过",
    provisional: "暂定",
    unsupported: "样本不足",
    missing: "无数据",
  }[status];
}

function gateFailureLabel(failure: string): string {
  return {
    support_below_100: "样本少于 100",
    "brier_not_below_0.25": "Brier 未低于 0.25",
    log_loss_not_below_ln2: "Log loss 未低于 ln2",
    "ece_above_0.10": "ECE 高于 0.10",
    "ece_upper_bound_above_0.15": "ECE 90% 上界高于 0.15",
    ece_upper_bound_missing: "ECE 90% 上界缺失",
    calibration_bins_not_valid_five_bin_ece: "ECE 无法形成五个有效分箱",
    prospective_data_missing: "前瞻数据尚未建立",
    reconstructed_data_missing: "历史重建数据缺失",
  }[failure] || failure;
}

export function OverviewPanel({
  error,
  loading,
  onRetry,
  overview,
}: {
  error: string | null;
  loading: boolean;
  onRetry: () => void;
  overview: IntelligenceOverview | null;
}) {
  if (loading && !overview) {
    return (
      <Skeleton className="intel-overview-skeleton" aria-label="正在加载历史情报总览">
        <SkeletonItem shape="rectangle" />
        <div><SkeletonItem shape="rectangle" /><SkeletonItem shape="rectangle" /></div>
        <SkeletonItem shape="rectangle" />
      </Skeleton>
    );
  }
  if (error && !overview) {
    return (
      <div className="intel-error-state" role="alert">
        <WarningCircle size={22} weight="fill" aria-hidden="true" />
        <div><strong>历史情报读取失败</strong><span>{error}</span></div>
        <Button appearance="secondary" onClick={onRetry}>重试</Button>
      </div>
    );
  }
  if (!overview) return null;

  const quality = qualitySlices(overview);
  const reconstructed = availabilityStatus(overview, quality, "reconstructed_walk_forward");
  const prospective = availabilityStatus(overview, quality, "prospective");
  const failures = quality.filter((item) => item.status === "failed");
  const provisional = quality.filter((item) => item.status === "provisional");
  const unsupported = quality.filter((item) => item.status === "unsupported");

  return (
    <section className="intel-overview" aria-label="评分与模型总览">
      {error && <div className="intel-stale-note">总览刷新失败，当前显示上一次成功结果</div>}
      <div className="intel-version-strip">
        <strong>当前版本</strong>
        {Object.entries(overview.versions).map(([name, version]) => (
          <span key={name}>
            {versionLabel(name)} <code>{version}</code>
          </span>
        ))}
      </div>

      <div className="intel-overview-grid">
        <section className="intel-metric-group">
          <h2>数据覆盖</h2>
          <div className="intel-metric-grid">
            {Object.entries(overview.coverage).map(([name, value]) => (
              <div key={name}>
                <strong>{integer(value)}</strong>
                <span>{COVERAGE_LABELS[name] || name}</span>
              </div>
            ))}
          </div>
        </section>

        <section className="intel-state-summary">
          <h2>局势分类</h2>
          <div>
            {(Object.keys(STATE_LABELS) as IntelligenceStateLabel[]).map((label) => (
              <span className={`intel-state-count state-${label}`} key={label}>
                <strong>{overview.team_state_distribution[label] || 0}</strong>
                {STATE_LABELS[label]}
              </span>
            ))}
          </div>
        </section>
      </div>

      <section className="intel-quality-section">
        <div className="intel-section-heading">
          <div>
            <h2>阵容模型校准</h2>
            <p>验收门槛：样本不少于 100，Brier 低于 0.25，Log loss 低于 ln2，ECE 不高于 0.10，ECE 90% bootstrap 上界不高于 0.15</p>
          </div>
          <div className="intel-availability" aria-label="预测数据可用性">
            <StatusTag ok={reconstructed}>历史重建 {reconstructed ? "有数据" : "无数据"}</StatusTag>
            <StatusTag ok={prospective}>前瞻验证 {prospective ? "有数据" : "尚未建立"}</StatusTag>
          </div>
        </div>

        {!prospective && reconstructed && (
          <div className="intel-quality-warning" role="status">
            <WarningCircle size={17} weight="fill" aria-hidden="true" />
            当前只有历史重建回测，不能视为真实前瞻表现。
          </div>
        )}
        {!prospective && !reconstructed && (
          <div className="intel-quality-warning" role="status">
            <WarningCircle size={17} weight="fill" aria-hidden="true" />
            历史重建和真实前瞻数据都尚未建立，当前没有可验收的阵容概率。
          </div>
        )}
        {failures.length > 0 && (
          <div className="intel-quality-warning critical" role="alert">
            <WarningCircle size={17} weight="fill" aria-hidden="true" />
            校准验收未通过：{failures.length} 个切片明确未通过门槛，阵容概率仅供研究复盘。
          </div>
        )}
        {provisional.length > 0 && (
          <div className="intel-quality-warning" role="status">
            <WarningCircle size={17} weight="fill" aria-hidden="true" />
            校准状态暂定：{provisional.length} 个切片的点估计通过，但完整 ECE bootstrap 验收尚未完成，不能标为通过。
          </div>
        )}
        {unsupported.length > 0 && (
          <div className="intel-quality-warning" role="status">
            <WarningCircle size={17} weight="fill" aria-hidden="true" />
            校准暂不支持：{unsupported.length} 个切片样本不足或没有可结算点，尚不能验收。
          </div>
        )}

        {quality.length === 0 ? (
          <div className="intel-empty-state compact">
            <Database size={18} aria-hidden="true" />
            <span>暂无可计算的阵容模型切片</span>
          </div>
        ) : (
          <div className="intel-table-scroll">
            <table className="intel-table intel-quality-table">
            <thead>
              <tr>
                <th>模型</th>
                <th>时点</th>
                <th>数据模式</th>
                <th>样本</th>
                <th>Brier</th>
                <th>Log loss</th>
                <th>ECE</th>
                <th>ECE 90% 上界</th>
                <th>验收</th>
              </tr>
            </thead>
            <tbody>
              {quality.map((slice) => (
                <tr key={`${slice.model_kind}-${slice.horizon_minutes}-${slice.availability_mode}`}>
                  <td>{modelKindLabel(slice.model_kind)}</td>
                  <td className="intel-number">{slice.horizon_minutes} 分钟</td>
                  <td>{availabilityLabel(slice.availability_mode)}</td>
                  <td className="intel-number">{slice.support}</td>
                  <td className="intel-number">{decimal(slice.brier_score, 3)}</td>
                  <td className="intel-number">{decimal(slice.log_loss, 3)}</td>
                  <td className="intel-number">{decimal(slice.ece_5_bin, 3)}</td>
                  <td className="intel-number">{decimal(slice.ece_90_upper, 3)}</td>
                  <td>
                    <span
                      className={`intel-quality-status ${slice.status}`}
                      title={slice.gate_failures.map(gateFailureLabel).join("、")}
                    >
                      {qualityStatusLabel(slice.status)}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
            </table>
          </div>
        )}
      </section>
    </section>
  );
}

function StatusTag({ children, ok }: { children: React.ReactNode; ok: boolean }) {
  return (
    <span className={ok ? "intel-status-tag ok" : "intel-status-tag warning"}>
      {ok ? <CheckCircle size={14} weight="fill" /> : <WarningCircle size={14} weight="fill" />}
      {children}
    </span>
  );
}
