import {
  Button,
  Dialog,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  Skeleton,
  SkeletonItem,
} from "@fluentui/react-components";
import {
  ArrowsOutSimple,
  ArrowSquareOut,
  ChartLineUp,
  CheckCircle,
  Clock,
  ClockCounterClockwise,
  Database,
  Eye,
  TerminalWindow,
  WarningCircle,
  X,
  XCircle,
} from "@phosphor-icons/react";
import { lazy, Suspense, useMemo, useState } from "react";

import {
  formatAge,
  formatClock,
  formatDateTime,
  formatOdds,
  formatPercent,
} from "../format";
import { getTrustedVision } from "../matchPresentation";
import { comparePeriods, mapNumberForPeriod, resolvePeriod } from "../probability-period";
import type {
  AnalysisSection,
  AnalysisSectionStatus,
  LineupAnalysisData,
  LineupCurveData,
  LineupCurvePoint,
  LineupSide,
  LivePlayerIdentityData,
  MatchDetail,
  MonitorMatch,
  OddsAnalysisData,
  RoshLineupScoresData,
  StrategyAnalysisData,
  StrategyComebackEntryInput,
  StrategyComebackStateInput,
  StrategyDecision,
  StrategyEntryWindowInput,
  StrategyRoshInput,
  VisionAnalysisData,
} from "../types";
import { LifecycleBadge } from "./StatusBadge";
import { PostmatchIntelligencePanel } from "./PostmatchIntelligencePanel";
import { LiveScoreboard } from "./live/LiveScoreboard";

const RAYBET_PAGE_HOSTS = new Set(["ray086.com", "www.ray086.com"]);
const RAYBET_PAGE_PREFIXES = ["/sports/esports", "/esports", "/dota2"];
const PUBLIC_STREAM_HOSTS = new Set([
  "play.ehome.gg",
  "play.xmshlb.com",
  "qplay.ehome.gg",
  "qplay.shyxswl.com",
]);
const SHA256_RE = /^[0-9a-f]{64}$/;
const VISION_FRAME_REF_RE = /^vision-frame:sha256:[0-9a-f]{64}$/;
const PROSPECTIVE_DRAFT_REF_RE = /^prospective-draft:[0-9a-f]{64}$/;
const OPAQUE_REF_RE = /^[A-Za-z0-9][A-Za-z0-9._:+-]{0,255}$/;
const UNSAFE_OPAQUE_REF_RE = /:\/\/|[?#]|[\u0000-\u001f\u007f]/;
const DECISION_KEY_RE = /^[0-9a-f]{32}$/;
const INPUT_REF_RE = /^[0-9a-f]{24}$/;
const STRATEGY_MATH_TOLERANCE = 1e-9;
const PROBABILITY_EPSILON = 1e-6;
const MAX_LEGACY_CONTRIBUTIONS_JSON_BYTES = 64 * 1024;
const COMEBACK_STRATEGY_V4 = "comeback-shadow-v4-controlled-entry";
const STRATEGY_CONTRIBUTION_KEYS = [
  "team_style",
  "player_form",
  "draft_curve",
  "lineup_rosh",
  "late_game_style",
  "market_movement",
] as const;
const LEGACY_STRATEGY_CONTRIBUTION_KEYS = STRATEGY_CONTRIBUTION_KEYS.filter(
  (key) => key !== "lineup_rosh",
);
const STRATEGY_CONTRIBUTION_KEY_SET = new Set<string>(STRATEGY_CONTRIBUTION_KEYS);

const ProbabilityChart = lazy(() =>
  import("./ProbabilityChart").then((module) => ({ default: module.ProbabilityChart })),
);

interface MatchWorkspaceProps {
  match: MonitorMatch | null;
  detail: MatchDetail | null;
  loading: boolean;
  error: string | null;
  now: number;
  replay: boolean;
}

export function MatchWorkspace({
  match,
  detail,
  loading,
  error,
  now,
  replay,
}: MatchWorkspaceProps) {
  const [periodSelection, setPeriodSelection] = useState<{
    matchId: string;
    period: string;
  } | null>(null);
  const selectedPeriod = periodSelection && periodSelection.matchId === match?.raybet_match_id
    ? periodSelection.period
    : null;
  const periods = useMemo(
    () => Array.from(new Set((detail?.winner_timeline || []).map((point) => point.period)))
      .sort(comparePeriods),
    [detail?.winner_timeline],
  );
  const activePeriod = resolvePeriod(
    periods,
    selectedPeriod,
    detail?.winner?.period || match?.winner?.period || null,
    replay,
  );

  if (!match) {
    return (
      <main className="workspace workspace-empty">
        <ChartLineUp size={32} aria-hidden="true" />
        <h2>没有可显示的赛事</h2>
        <p>启动赔率采集后，侦测到的 Dota 2 赛事会出现在这里。</p>
      </main>
    );
  }

  const liveWinner = detail?.winner || match.winner;
  const prematchWinner = !liveWinner && match.lifecycle === "upcoming"
    ? detail?.prematch_winner || null
    : null;
  const winner = liveWinner || prematchWinner;
  const showingPrematch = prematchWinner != null;
  const observedAge = winner?.observed_at
    ? Math.max(0, (now - new Date(winner.observed_at).getTime()) / 1000)
    : null;
  const trustedVision = detail ? getTrustedVision(detail) : null;
  const watchLink = safeWatchLink(match.watch_link);
  const chartTimeline = detail?.winner_timeline || [];
  const chartDecisions = detail?.decisions || [];
  const hasChartTimeline = chartTimeline.length > 0;
  const hasModelDecisions = chartDecisions.length > 0;

  return (
    <main className="workspace">
      <section className="match-decision-hero" aria-label="比赛与策略结论">
        <LiveScoreboard
          match={match}
          oddsLabel={showingPrematch
            ? `赛前快照 ${formatDateTime(prematchWinner.observed_at)}`
            : `赔率 ${formatAge(observedAge)}`}
          oddsStale={!showingPrematch && observedAge != null && observedAge > 60}
          trustedVision={trustedVision}
          watchLink={watchLink}
        />

        <section className="quote-strip" aria-label="最新胜负盘">
          <QuoteCell
            label={match.team_one || "队伍一"}
            odds={winner?.prices?.team_one}
            probability={winner?.probabilities?.team_one}
            tone="one"
          />
          <div className="quote-context">
            <span>{replay ? "历史回放" : showingPrematch ? "赛前快照" : "实时胜负盘"}</span>
            <strong>{trustedVision?.map_number ? `第 ${trustedVision.map_number} 局` : winner?.period || "局数待确认"}</strong>
            <small>
              {showingPrematch
                ? `采集于 ${formatDateTime(prematchWinner.observed_at)} · 不进入实时策略`
                : trustedVision?.game_clock_seconds != null
                ? `可信时钟 ${formatClock(trustedVision.game_clock_seconds)}`
                : "暂无可信比赛时钟"}
            </small>
          </div>
          <QuoteCell
            label={match.team_two || "队伍二"}
            odds={winner?.prices?.team_two}
            probability={winner?.probabilities?.team_two}
            tone="two"
          />
        </section>

        {error && (
          <div className="inline-error" role="alert">
            <WarningCircle size={18} aria-hidden="true" />
            <span>{error}</span>
          </div>
        )}

        <CurrentStrategyOverview detail={detail} error={error} match={match} />
      </section>

      {loading && !detail ? (
        <WorkspaceSkeleton />
      ) : (
        <>
          <section className={`workspace-section chart-section${hasChartTimeline ? "" : " is-empty"}`}>
            <div className="section-heading">
              <div>
                <h2>{hasModelDecisions
                  ? "市场概率与模型判断"
                  : hasChartTimeline ? "市场概率走势" : "市场概率记录"}</h2>
                <p>{hasChartTimeline
                  ? "横轴为真实采集时间。超过 60 秒的数据空档会断开曲线。"
                  : "等待同一采集时刻的完整双方报价。"}</p>
              </div>
              <span className="method-note" title="双方概率已按完整胜负盘去除水位">
                去水概率
              </span>
            </div>
            <Suspense fallback={<div className="chart-empty"><span>正在加载概率图</span></div>}>
              <ProbabilityChart
                key={match.raybet_match_id}
                timeline={chartTimeline}
                decisions={chartDecisions}
                teamOne={match.team_one}
                teamTwo={match.team_two}
                preferLatestPeriod={replay}
                preferredPeriod={winner?.period || null}
                selectedPeriod={activePeriod}
                onPeriodChange={(period) => setPeriodSelection({
                  matchId: match.raybet_match_id,
                  period,
                })}
              />
            </Suspense>
          </section>

          {replay && (
            <PostmatchIntelligencePanel
              mapNumber={mapNumberForPeriod(activePeriod)}
              raybetMatchId={match.raybet_match_id}
              teamOne={match.team_one}
              teamTwo={match.team_two}
            />
          )}

          <details className="advanced-analysis">
            <summary>
              <span>查看阵容、策略记录与原始证据</span>
              <small>用于复核当前结论</small>
            </summary>
            <div className="advanced-analysis-content">
              <LineupAnalysis
                detail={detail}
                error={error}
                match={match}
              />

              <section className="workspace-lower-grid">
                <DecisionTimeline detail={detail} error={error} match={match} />
                <EvidenceSummary detail={detail} error={error} match={match} />
              </section>

              <MarketDrawer detail={detail} />
            </div>
          </details>
        </>
      )}
    </main>
  );
}

type FunnelTone = "pass" | "blocked" | "waiting" | "invalid";

function CurrentStrategyOverview({
  detail,
  error,
  match,
}: {
  detail: MatchDetail | null;
  error: string | null;
  match: MonitorMatch;
}) {
  const strategy = normalizeStrategySection(
    sectionOrFallback(detail?.analysis?.strategy, error),
  );
  const decisions = strategy.status === "available" ? strategy.data?.decisions || [] : [];
  const latest = decisions[decisions.length - 1] || null;
  const evidence = latest ? parseDecisionEvidence(latest) : null;
  const inputs = evidence && !evidence.invalidReason ? evidence.inputs : {};
  const visionInput = recordValue(inputs.vision);
  const comeback = parseComebackState(
    inputs.comeback_state,
    visionInput.radiant_team_side,
  );
  const entryWindow = parseEntryWindow(inputs.entry_window);
  const entry = parseComebackEntry(inputs.comeback_entry, entryWindow);
  const v4 = latest?.strategy_version === COMEBACK_STRATEGY_V4;
  const malformedV4 = Boolean(v4 && (!comeback || !entryWindow || !entry));
  const invalid = strategy.status === "review" || malformedV4 || Boolean(evidence?.invalidReason);
  const entryCandidate = entry?.eligible === true;
  const waitingForStart = !invalid && !latest && match.lifecycle === "upcoming";

  const verdictTone: FunnelTone = invalid
    ? "invalid"
    : latest?.eligible === 1
      ? "pass"
      : latest ? "blocked" : "waiting";
  const verdictTitle = invalid
    ? "策略证据无效"
    : latest?.eligible === 1
      ? "最终策略合格"
      : latest && entryCandidate
        ? "候选通过，最终策略拒绝"
        : latest ? "当前策略拒绝" : waitingForStart ? "等待开赛" : "当前策略不可判定";
  const primaryReason = latest?.reason || strategy.reason;
  const reasonText = waitingForStart
    ? "等待比赛开始并采集可信 HUD。"
    : strategyReasonText(primaryReason, strategy.status);
  const readiness = detail?.readiness || match.readiness;
  const dataTone = combineReadinessTone(
    readinessTone(readiness.odds.status),
    readinessTone(readiness.mapping.status),
  );
  const hudTone: FunnelTone = malformedV4
    ? "invalid"
    : comeback?.source_status === "available" ? "pass" : "waiting";
  const entryTone: FunnelTone = malformedV4
    ? "invalid"
    : entry ? (entry.eligible ? "pass" : "blocked") : "waiting";
  const roshProbability = entry?.rosh_underdog_probability ?? null;
  const roshTone: FunnelTone = malformedV4
    ? "invalid"
    : roshProbability == null ? "waiting" : roshProbability > 0.5 ? "pass" : "blocked";
  const scanText = strategy.data
    ? `扫描 ${strategy.data.scanned_count} 条 · 显示 ${strategy.data.displayed_count} 条 · 唯一排除 ${strategy.data.excluded_decision_count} 条`
    : "尚无可信策略输出";
  const prematchWinner = match.lifecycle === "upcoming"
    ? detail?.prematch_winner || null
    : null;
  const latestDataAt = detail?.winner?.observed_at
    || match.winner?.observed_at
    || prematchWinner?.observed_at
    || match.updated_at;
  const nextStep = waitingForStart
    ? "等待比赛开始并采集可信 HUD"
    : invalid ? "复核并修复无效策略证据"
      : latest?.eligible === 1 ? "保持纸面监控并等待后续结算"
        : latest && entryCandidate ? "检查最终拒绝原因并保留候选证据"
          : latest ? "等待下一次可信输入" : "等待可信 HUD 与策略判断";

  return (
    <section className={`strategy-overview tone-${verdictTone}`} aria-label="当前策略结论与入场链路">
      <div className="strategy-verdict">
        <div className="strategy-verdict-status">
          <VerdictIcon tone={verdictTone} />
          <span>{invalid ? "证据无效" : latest ? "证据有效" : waitingForStart ? "尚未开赛" : "证据不可用"}</span>
          {entryCandidate && latest?.eligible === 0 && <span>入场候选通过</span>}
        </div>
        <h2>{verdictTitle}</h2>
        <p>{reasonText}</p>
        <div className="strategy-verdict-meta">
          <span>{latest
            ? `结论更新 ${formatDateTime(latest.decided_at)}`
            : `最近数据 ${formatDateTime(latestDataAt)}`}</span>
          <span>下一步 {nextStep}</span>
          {latest && <span>{scanText}</span>}
        </div>
        {primaryReason && (
          <details className="strategy-reason-code">
            <summary>诊断代码</summary>
            <code>{primaryReason}</code>
          </details>
        )}
        {v4 && (
          <p className="strategy-provisional">v4 仅为纸面影子信号，不代表策略表现已验证。</p>
        )}
      </div>

      <div className="strategy-funnel" aria-label="当前比赛关键链路">
        <FunnelStep
          detail={`${prematchWinner?.complete
            ? "赛前快照已保存"
            : `赔率 ${readinessStatusText(readiness.odds.status)}`} · 映射 ${readinessStatusText(readiness.mapping.status)}`}
          label="数据可信"
          tone={dataTone}
        />
        <FunnelStep
          detail={comeback
            ? `${comeback.source_status === "available" ? "HUD 已验证" : "HUD 不可用"} · 置信 ${formatPercent(comeback.confidence)}`
            : "等待 v4 HUD 证据"}
          label="实时 HUD"
          tone={hudTone}
        />
        <FunnelStep
          detail={entry
            ? `${entry.eligible ? "入场门槛通过" : "入场门槛阻止"} · ${comebackStateSummary(comeback)}`
            : "入场门槛不可判"}
          label="受控劣势"
          tone={entryTone}
        />
        <FunnelStep
          detail={roshProbability == null
            ? "方向不可判"
            : `${formatPercent(roshProbability)} ${roshProbability > 0.5 ? "支持弱势方" : "不支持弱势方"}`}
          label="Rosh 方向"
          tone={roshTone}
        />
        <FunnelStep
          detail={invalid
            ? "最终证据无效"
            : latest ? (latest.eligible === 1 ? "最终合格" : "最终拒绝") : "最终不可判"}
          label="最终资格"
          tone={verdictTone}
        />
      </div>
    </section>
  );
}

function FunnelStep({
  detail,
  label,
  tone,
}: {
  detail: string;
  label: string;
  tone: FunnelTone;
}) {
  const stateText = tone === "pass"
    ? "通过"
    : tone === "blocked" ? "阻止" : tone === "invalid" ? "无效" : "等待";
  return (
    <div className={`funnel-step ${tone}`}>
      <div>
        <VerdictIcon tone={tone} />
        <strong>{label}</strong>
        <span>{stateText}</span>
      </div>
      <p>{detail}</p>
    </div>
  );
}

function VerdictIcon({ tone }: { tone: FunnelTone }) {
  if (tone === "pass") return <CheckCircle size={17} weight="fill" aria-hidden="true" />;
  if (tone === "blocked") return <XCircle size={17} weight="fill" aria-hidden="true" />;
  if (tone === "invalid") return <WarningCircle size={17} weight="fill" aria-hidden="true" />;
  return <Clock size={17} aria-hidden="true" />;
}

function readinessTone(status: MonitorMatch["readiness"]["odds"]["status"]): FunnelTone {
  if (status === "ready" || status === "delayed") return "pass";
  if (status === "invalid") return "invalid";
  if (["unhealthy", "stopped", "degraded"].includes(status)) return "blocked";
  return "waiting";
}

function combineReadinessTone(left: FunnelTone, right: FunnelTone): FunnelTone {
  if (left === "invalid" || right === "invalid") return "invalid";
  if (left === "blocked" || right === "blocked") return "blocked";
  return left === "pass" && right === "pass" ? "pass" : "waiting";
}

function readinessStatusText(status: MonitorMatch["readiness"]["odds"]["status"]): string {
  return {
    ready: "就绪",
    delayed: "延迟",
    stale: "过期",
    missing: "缺失",
    invalid: "无效",
    unconfirmed: "未确认",
    degraded: "降级",
    unhealthy: "异常",
    stopped: "停止",
  }[status];
}

function strategyReasonText(reason: string, status: AnalysisSectionStatus): string {
  const descriptions: Record<string, string> = {
    eligible: "全部策略门槛已通过。",
    controlled_deficit: "弱势方处于策略允许的受控劣势区间。",
    vision_net_worth_evidence_missing: "HUD 缺少可用经济区间，Entry 已阻止。",
    vision_situation_collapsed: "弱势方劣势超出策略上限，Entry 已阻止。",
    underdog_deficit_not_material: "当前劣势未达到入场下限，Entry 已阻止。",
    comeback_entry_outside_time_window: "比赛时钟不在受控入场窗口内。",
    rosh_direction_unavailable: "Rosh 对弱势方的方向证据不可用。",
    rosh_direction_opposes_underdog: "Rosh 方向不支持当前弱势方。",
    edge_below_threshold: "模型 Edge 未达到最终策略阈值。",
    conservative_probability_not_above_market: "保守模型概率未高于市场概率。",
    insufficient_data_quality: "数据质量未达到最终策略门槛。",
    no_independent_positive_contribution: "没有独立的正向模型贡献。",
    strategy_evidence_invalid: "策略证据未通过前端契约校验。",
    waiting_for_strategy_inputs: "等待可信策略输入。",
  };
  if (descriptions[reason]) return descriptions[reason];
  if (status === "waiting") return "等待形成可信的策略判断。";
  if (status === "review") return "策略证据需要人工复核，不能用于入场。";
  if (status === "unavailable") return "当前没有可用于判断的策略证据。";
  return "当前判断已保留原因码，详细证据见下方策略记录。";
}

function comebackStateSummary(state: StrategyComebackStateInput | null): string {
  if (!state) return "局势不可判";
  const kill = deficitDescription(state.kill_deficit, "击杀");
  const economy = economyDeficitDescription(state);
  return `${kill.replace("弱势方", "")} · ${economy.replace("弱势方", "")}`;
}

function safeWatchLink(link: MonitorMatch["watch_link"]): {
  kind: "public_stream" | "match_page";
  url: string;
} | null {
  if (
    link?.availability !== "available"
    || (link.kind !== "public_stream" && link.kind !== "match_page")
    || typeof link.url !== "string"
  ) {
    return null;
  }
  try {
    const parsed = new URL(link.url);
    if (
      parsed.protocol !== "https:"
      || parsed.username
      || parsed.password
      || parsed.port
      || parsed.search
      || parsed.hash
    ) {
      return null;
    }
    if (link.kind === "public_stream") {
      return PUBLIC_STREAM_HOSTS.has(parsed.hostname)
        && parsed.pathname.toLowerCase().endsWith(".m3u8")
        ? { kind: link.kind, url: parsed.href }
        : null;
    }
    const allowedPath = RAYBET_PAGE_PREFIXES.some(
      (prefix) => parsed.pathname === prefix || parsed.pathname.startsWith(`${prefix}/`),
    );
    return RAYBET_PAGE_HOSTS.has(parsed.hostname) && allowedPath
      ? { kind: link.kind, url: parsed.href }
      : null;
  } catch {
    return null;
  }
}

function QuoteCell({
  label,
  odds,
  probability,
  tone,
}: {
  label: string;
  odds?: number;
  probability?: number;
  tone: "one" | "two";
}) {
  return (
    <div className={`quote-cell ${tone}`}>
      <span>{label}</span>
      <strong>{formatOdds(odds)}</strong>
      <small>{formatPercent(probability)} 市场概率</small>
    </div>
  );
}

const analysisStatusLabel: Record<AnalysisSectionStatus, string> = {
  available: "有证据",
  waiting: "等待",
  unavailable: "不可用",
  review: "需复核",
};

const reasonDescription: Record<string, string> = {
  analysis_contract_unavailable: "当前后端未提供分析契约，不能推断策略或阵容已经就绪。",
  analysis_section_invalid: "分析区块契约无效，不能把该结果显示为可用。",
  detail_request_failed: "赛事详情请求失败，无法确认分析证据。",
  live_player_identity_unavailable: "实时来源没有可信选手身份，因此不展示或猜测选手。",
  strategy_evidence_invalid: "策略证据结构无效，需要复核持久化记录。",
  odds_payload_invalid: "赔率分析摘要结构无效，已忽略该区块数据。",
  vision_payload_invalid: "视觉分析证据结构无效，已忽略该区块数据。",
  lineup_payload_invalid: "阵容不是可信的两边各五个唯一英雄。",
  lineup_curve_payload_invalid: "阵容曲线结构无效，不能显示为可用预测。",
  lineup_curve_clock_unavailable: "缺少可信比赛时钟，无法验证阵容曲线的当前检查点。",
  rosh_lineup_score_payload_invalid: "Rosh 阵容评分证据结构无效，不能显示或参与判断。",
  rosh_player_identity_available: "Rosh 证据已解析全部十个选手位置。",
  rosh_player_identity_partial: "Rosh 证据只解析出部分选手位置，实际评分回退纯阵容。",
  rosh_player_identity_evidence_invalid: "Rosh 选手身份结构无效，已隐藏该部分证据。",
  players_payload_invalid: "选手证据结构无效，不能显示。",
};

function unavailableSection<T>(reason = "analysis_contract_unavailable"): AnalysisSection<T> {
  return { status: "unavailable", reason, data: null };
}

function reviewSection<T>(reason: string): AnalysisSection<T> {
  return { status: "review", reason, data: null };
}

function sectionOrFallback<T>(
  section: AnalysisSection<T> | undefined,
  error: string | null,
): AnalysisSection<T> {
  if (section && validAnalysisSection(section)) return section;
  if (section) return reviewSection("analysis_section_invalid");
  return unavailableSection(error ? "detail_request_failed" : "analysis_contract_unavailable");
}

function withoutUnavailableData<T>(section: AnalysisSection<T>): AnalysisSection<T> {
  return section.status === "available"
    ? section
    : { status: section.status, reason: section.reason, data: null };
}

function normalizeOddsSection(
  section: AnalysisSection<OddsAnalysisData>,
): AnalysisSection<OddsAnalysisData> {
  const normalized = withoutUnavailableData(section);
  return normalized.status === "available" && !validOddsData(normalized.data)
    ? reviewSection("odds_payload_invalid")
    : normalized;
}

function normalizeVisionSection(
  section: AnalysisSection<VisionAnalysisData>,
): AnalysisSection<VisionAnalysisData> {
  const normalized = withoutUnavailableData(section);
  return normalized.status === "available" && !validVisionData(normalized.data)
    ? reviewSection("vision_payload_invalid")
    : normalized;
}

function normalizeStrategySection(
  section: AnalysisSection<StrategyAnalysisData>,
): AnalysisSection<StrategyAnalysisData> {
  const normalized = withoutUnavailableData(section);
  return normalized.status === "available" && !validStrategyData(normalized.data)
    ? reviewSection("strategy_evidence_invalid")
    : normalized;
}

function normalizeLineupSection(
  section: AnalysisSection<LineupAnalysisData>,
): AnalysisSection<LineupAnalysisData> {
  const normalized = withoutUnavailableData(section);
  if (normalized.status !== "available") return normalized;
  if (!validLineupData(normalized.data)) return reviewSection("lineup_payload_invalid");
  return {
    ...normalized,
    data: {
      ...normalized.data,
      scores: normalizeRoshScoresSection(
        sectionOrFallback(normalized.data.scores, null),
      ),
      active_curve: withoutUnavailableData(normalized.data.active_curve),
      players: normalizePlayersSection(normalized.data.players, normalized.data),
    },
  };
}

function normalizeRoshScoresSection(
  section: AnalysisSection<RoshLineupScoresData>,
): AnalysisSection<RoshLineupScoresData> {
  const normalized = withoutUnavailableData(section);
  return normalized.status === "available" && !validRoshScoresData(normalized.data)
    ? reviewSection("rosh_lineup_score_payload_invalid")
    : normalized;
}

function normalizeCurveSection(
  section: AnalysisSection<LineupCurveData>,
  gameClockSeconds: number | null,
): AnalysisSection<LineupCurveData> {
  const normalized = withoutUnavailableData(section);
  if (normalized.status !== "available") return normalized;
  if (!validGameClock(gameClockSeconds)) {
    return reviewSection("lineup_curve_clock_unavailable");
  }
  return !validCurveData(normalized.data, gameClockSeconds)
    ? reviewSection("lineup_curve_payload_invalid")
    : normalized;
}

function normalizePlayersSection(
  section: AnalysisSection<LivePlayerIdentityData>,
  lineup: LineupAnalysisData,
): AnalysisSection<LivePlayerIdentityData> {
  const normalized = withoutUnavailableData(section);
  return normalized.status === "available" && !validPlayersData(normalized.data, lineup)
    ? reviewSection("players_payload_invalid")
    : normalized;
}

function AnalysisState({
  section,
  lifecycle,
  label,
}: {
  section: AnalysisSection<unknown>;
  lifecycle: MonitorMatch["lifecycle"];
  label?: string;
}) {
  const waitingForStart = section.status === "waiting" && lifecycle === "upcoming";
  return (
    <span className={`analysis-state ${section.status}`}>
      {label || (waitingForStart ? "等待开赛" : analysisStatusLabel[section.status])}
    </span>
  );
}

function AnalysisEmpty({
  section,
  lifecycle,
  subject,
}: {
  section: AnalysisSection<unknown>;
  lifecycle: MonitorMatch["lifecycle"];
  subject: string;
}) {
  const waitingForStart = section.status === "waiting" && lifecycle === "upcoming";
  const title = waitingForStart
    ? "比赛尚未开始，等待开赛"
    : section.status === "waiting"
      ? `${subject}正在等待必要证据`
      : section.status === "review"
        ? `${subject}需要人工复核`
        : `${subject}不可用`;
  return (
    <div className="analysis-empty" role="status">
      <div>
        <AnalysisState lifecycle={lifecycle} section={section} />
        <strong>{title}</strong>
      </div>
      <span>{reasonDescription[section.reason] || "系统保留了明确的阻塞原因，没有用缺失数据补算。"}</span>
      <details className="analysis-reason-code">
        <summary>诊断详情</summary>
        <code>{section.reason || "reason_not_provided"}</code>
      </details>
    </div>
  );
}

function DecisionTimeline({
  detail,
  error,
  match,
}: {
  detail: MatchDetail | null;
  error: string | null;
  match: MonitorMatch;
}) {
  const section = normalizeStrategySection(
    sectionOrFallback(detail?.analysis?.strategy, error),
  );
  const structuredDecisions = section.status === "available"
    ? section.data?.decisions
    : null;
  const strategyData = section.status === "available" ? section.data : null;
  const legacy = !detail?.analysis && (detail?.decisions.length || 0) > 0;
  const decisions = structuredDecisions || (legacy ? detail?.decisions : []) || [];
  const displayed = legacy ? decisions.slice(-12) : decisions;
  const latest = [...displayed].reverse();

  return (
    <section className="workspace-section decision-section">
      <div className="section-heading compact">
        <div>
          <h2>策略判断</h2>
          <p>
            <span>显示最近 {latest.length} 条</span>
            {strategyData && (
              <>
                <span aria-hidden="true"> · </span>
                <span>{strategyScanScopeLabel(strategyData)}</span>
              </>
            )}
          </p>
        </div>
        <div className="section-status">
          <AnalysisState lifecycle={match.lifecycle} section={section} />
          <ClockCounterClockwise size={19} aria-hidden="true" />
        </div>
      </div>
      {legacy && (
        <div className="analysis-contract-note" role="status">
          仅显示旧契约记录；分析状态仍为不可用，不能据此宣称策略就绪。
        </div>
      )}
      {(strategyData?.has_more || strategyData?.truncated) && (
        <div className="analysis-contract-note" role="status">
          {strategyData.has_more && <span>还有更早的策略记录未显示。</span>}
          {strategyData.truncated && <span>后端结果已截断。</span>}
        </div>
      )}
      {latest.length ? (
        <div className="decision-list">
          {latest.map((decision) => (
            <DecisionRow
              decision={decision}
              key={decision.decision_key || `${decision.decided_at}-${decision.reason}`}
              match={match}
            />
          ))}
        </div>
      ) : (
        <AnalysisEmpty lifecycle={match.lifecycle} section={section} subject="策略分析" />
      )}
    </section>
  );
}

function strategyScanScopeLabel(data: StrategyAnalysisData): string {
  return `扫描范围：最近 ${data.scanned_count} 条候选记录`;
}

const contributionLabel: Record<string, string> = {
  team_style: "队伍风格",
  player_form: "选手状态",
  draft_curve: "阵容曲线",
  lineup_rosh: "Rosh 阵容评分",
  late_game_style: "后期能力",
  market_movement: "市场变化",
};

interface ParsedDecisionEvidence {
  contributions: Array<[string, number]>;
  conservative: Array<[string, number]>;
  inputs: Record<string, unknown>;
  invalidReason: string | null;
}

function DecisionRow({
  decision,
  match,
}: {
  decision: StrategyDecision;
  match: MonitorMatch;
}) {
  const evidence = parseDecisionEvidence(decision);
  const visionInput = recordValue(evidence.inputs.vision);
  const visionAuthority = recordValue(decision.vision_authority);
  const draftLandmarkInput = recordValue(evidence.inputs.draft_landmark);
  const draftInput = Object.keys(draftLandmarkInput).length
    ? draftLandmarkInput
    : recordValue(evidence.inputs.draft_authority);
  const draftRef = firstDraftReference(
    decision.draft_authority,
    ["source_ref", "landmark_key", "curve_key", "draft_hash"],
  );
  const visionRef = visionFrameReference(visionInput.source_frame_ref)
    || firstVisionFrameReference(
    decision.vision_authority,
    ["source_frame_ref", "frame_ref", "observation_key"],
  );
  const inputCapturedAt = stringValue(visionInput.captured_at);
  const authorityCapturedAt = stringValue(visionAuthority.captured_at);
  const capturedAt = inputCapturedAt && validTimestamp(inputCapturedAt)
    ? inputCapturedAt
    : authorityCapturedAt && validTimestamp(authorityCapturedAt)
      ? authorityCapturedAt
      : null;
  const inputGameClock = numberValue(visionInput.game_clock_seconds);
  const authorityGameClock = numberValue(visionAuthority.aligned_game_clock_seconds);
  const gameClock = validGameClock(inputGameClock)
    ? inputGameClock
    : validGameClock(authorityGameClock)
      ? authorityGameClock
      : null;
  const draftVersion = safeCode(draftInput.model_version);
  const roshInput = recordValue(evidence.inputs.rosh_lineup_score);
  const usesRosh = evidence.contributions.some(([name]) => name === "lineup_rosh");
  const persistedRosh = usesRosh
    && validRoshStrategyInput(roshInput, decision.eligible === 1, inputGameClock)
    ? roshInput as unknown as StrategyRoshInput
    : null;
  const actualStake = numberValue(roshInput.actual_stake_multiplier);
  const stakeCap = numberValue(roshInput.stake_cap);
  const strategyVersion = safeCode(decision.strategy_version);
  const inputRef = inputReference(decision.input_ref);
  const decisionKey = decisionReference(decision.decision_key);

  return (
    <article className="decision-row">
      <div className="decision-summary">
        <div className="decision-primary">
          <span>{formatDateTime(decision.decided_at)} · 第 {decision.map_number} 局</span>
          {decision.eligible === 0 && (
            <strong className="decision-blocked-label">未满足策略门槛</strong>
          )}
          <details className="decision-reason-code">
            <summary>诊断代码</summary>
            <code>{decision.reason}</code>
          </details>
        </div>
        <div className="decision-values">
          <span>模型 {formatPercent(decision.model_probability)}</span>
          <span>市场 {formatPercent(decision.market_probability)}</span>
          <strong className={decision.edge > 0 ? "positive" : ""}>
            Edge {decision.edge > 0 ? "+" : ""}{formatPercent(decision.edge)}
          </strong>
          <span>质量 {formatPercent(decision.data_quality)}</span>
        </div>
      </div>
      <dl className="decision-identity">
        <div><dt>策略版本</dt><dd><code>{strategyVersion || "未提供"}</code></dd></div>
        <div><dt>Input ref</dt><dd><code>{inputRef || "未提供"}</code></dd></div>
        <div><dt>Decision key</dt><dd><code>{decisionKey || "未提供"}</code></dd></div>
      </dl>
      {evidence.invalidReason ? (
        <div className="decision-evidence-error" role="alert">
          贡献证据无效 <code>{evidence.invalidReason}</code>
        </div>
      ) : (
        <div className="contribution-list" aria-label="策略贡献">
          {evidence.contributions.length ? evidence.contributions.map(([name, value]) => (
            <div className="contribution-row" key={name}>
              <span>{contributionLabel[name] || name}</span>
              <code>Δlogit {formatSigned(value)}</code>
              {conservativeValue(evidence.conservative, name) != null && (
                <small>保守 {formatSigned(conservativeValue(evidence.conservative, name)!)}</small>
              )}
            </div>
          )) : <span className="evidence-missing">没有持久化贡献项</span>}
        </div>
      )}
      {persistedRosh && (
        <DecisionRoshEvidence
          decision={decision}
          input={persistedRosh}
          match={match}
          radiantTeamSide={stringValue(visionInput.radiant_team_side)}
        />
      )}
      <DecisionComebackEvidence inputs={evidence.inputs} />
      <div className="decision-evidence" aria-label="持久化决策证据">
        <span>视觉 {capturedAt ? formatDateTime(capturedAt) : "未提供"}</span>
        <span>时钟 {gameClock == null ? "未提供" : formatClock(gameClock)}</span>
        <span>画面引用 <code>{visionRef || "未提供"}</code></span>
        <span>阵容模型 <code>{draftVersion || draftRef || "未提供"}</code></span>
        {actualStake != null && <span>实际仓位 {formatPercent(actualStake)}</span>}
        {stakeCap != null && <span>仓位上限 {formatPercent(stakeCap)}</span>}
      </div>
    </article>
  );
}

function DecisionRoshEvidence({
  decision,
  input,
  match,
  radiantTeamSide,
}: {
  decision: StrategyDecision;
  input: StrategyRoshInput;
  match: MonitorMatch;
  radiantTeamSide: string | null;
}) {
  const selectedScore = numberValue(input.selected_score);
  const selectedMinute = numberValue(input.selected_minute);
  const coverage = numberValue(input.player_coverage);
  const coverageCount = numberValue(input.player_coverage_count);
  const formulaVersion = safeCode(input.formula_version);
  const mode = input.mode === "player_adjusted"
    ? "选手修正"
    : input.mode === "pure"
      ? "纯阵容回退"
      : "评分不可用";
  const situation = selectedScore == null
    ? null
    : decisionRoshSituation(selectedScore, radiantTeamSide, match);
  return (
    <div className="decision-evidence" aria-label="决策时 Rosh 证据">
      <span>
        当前分钟分 {selectedScore == null
          ? "不可用"
          : `${formatRoshScore(selectedScore)} · ${selectedMinute} 分钟桶`}
      </span>
      <span>决策时阵容局势 {situation || "不可判定"}</span>
      <span>评分模式 {mode}</span>
      <span>
        选手覆盖 {coverage == null
          ? "未提供"
          : `${formatPercent(coverage)}${coverageCount == null ? "" : ` (${coverageCount}/10)`}`}
      </span>
      <span>公式 <code>{formulaVersion || "未提供"}</code></span>
      <span>Rosh 数据时间 {validTimestamp(input.source_as_of) ? formatDateTime(input.source_as_of) : "未提供"}</span>
      {selectedScore == null && (
        <span>
          缺失原因 <code>{decisionRoshMissingReason(decision.reason, input)}</code>
        </span>
      )}
    </div>
  );
}

function decisionRoshSituation(
  score: number,
  radiantTeamSide: string | null,
  match: MonitorMatch,
): string {
  if (Math.abs(score) < 0.05) return `阵容均衡 ${formatRoshScore(score)}`;
  const radiantSide = radiantTeamSide === "team_one" || radiantTeamSide === "team_two"
    ? radiantTeamSide
    : null;
  const advantagedSide = score > 0
    ? radiantSide
    : radiantSide === "team_one"
      ? "team_two"
      : radiantSide === "team_two"
        ? "team_one"
        : null;
  const team = advantagedSide === "team_one"
    ? match.team_one || "队伍一"
    : advantagedSide === "team_two"
      ? match.team_two || "队伍二"
      : score > 0
        ? "Radiant"
        : "Dire";
  return `${team} 阵容占优 ${formatRoshScore(score)}`;
}

function decisionRoshMissingReason(
  reason: string,
  input: StrategyRoshInput,
): string {
  const descriptions: Record<string, string> = {
    rosh_lineup_score_unavailable: "没有持久化 Rosh 阵容评分",
    rosh_lineup_draft_mismatch: "Rosh 评分阵容与决策时可信阵容不一致",
    rosh_minute_score_unavailable: "决策时刻没有可用的 Rosh 分钟桶",
    rosh_direction_unavailable: "无法从当前分钟 Rosh 分确定劣势方方向",
  };
  if (descriptions[reason]) return `${descriptions[reason]} (${reason})`;
  if (input.status === "unavailable") {
    return `没有持久化 Rosh 阵容评分 (${reason})`;
  }
  if (input.draft_matches_observation === false) {
    return `Rosh 评分阵容与决策时可信阵容不一致 (${reason})`;
  }
  return `当前分钟 Rosh 分不可用 (${reason})`;
}

function DecisionComebackEvidence({ inputs }: { inputs: Record<string, unknown> }) {
  const vision = recordValue(inputs.vision);
  const state = parseComebackState(
    inputs.comeback_state,
    vision.radiant_team_side,
  );
  const window = parseEntryWindow(inputs.entry_window);
  const entry = parseComebackEntry(inputs.comeback_entry, window);
  const frameRef = visionFrameReference(vision.source_frame_ref);
  if (!state && !window && !entry) return null;
  return (
    <div className="decision-evidence" aria-label="决策时实时局势证据">
      {state && (
        <>
          <span>实时局势 {state.controllable ? "可控劣势" : "不可用于入场"}</span>
          <span>局势原因 <code>{state.reason}</code></span>
          <span>{deficitDescription(state.kill_deficit, "击杀")}</span>
          <span>{economyDeficitDescription(state)}</span>
          <span>
            HUD {state.source || "来源不可用"} · 置信 {formatPercent(state.confidence)} · <code>{frameRef || "画面引用不可用"}</code>
          </span>
          {state.unavailable_reason && (
            <span>局势缺失原因 <code>{state.unavailable_reason}</code></span>
          )}
        </>
      )}
      {window && (
        <span>
          入场时间窗 {window.inside ? "命中" : "不在窗口"} · {formatClock(window.minimum_clock_seconds)}-{formatClock(window.maximum_clock_seconds)}
        </span>
      )}
      {entry && (
        <>
          <span>
            入场判定 {entry.eligible ? "允许" : "阻止"} · <code>{entry.reason}</code>
          </span>
          <span>
            可控区间：击杀落后 {entry.policy.minimum_kill_deficit}-{entry.policy.maximum_kill_deficit} · 经济落后 {entry.policy.minimum_net_worth_deficit.toLocaleString("zh-CN")}-{entry.policy.maximum_net_worth_deficit.toLocaleString("zh-CN")} · {formatClock(entry.policy.minimum_clock_seconds)}-{formatClock(entry.policy.maximum_clock_seconds)} · HUD 置信至少 {formatPercent(entry.policy.minimum_vision_confidence)}
          </span>
        </>
      )}
    </div>
  );
}

function parseComebackState(
  value: unknown,
  radiantTeamSide: unknown,
): StrategyComebackStateInput | null {
  const data = recordValue(value);
  const economyFields = [
    "underdog_net_worth",
    "opponent_net_worth",
    "net_worth_deficit",
    "net_worth_advantage_side",
    "net_worth_deficit_min",
    "net_worth_deficit_max",
  ];
  if (
    typeof data.controllable !== "boolean"
    || safeCode(data.reason) === null
    || safeCode(data.source_status) === null
    || !finiteUnit(data.confidence)
    || (data.underdog_side !== "team_one" && data.underdog_side !== "team_two")
    || economyFields.some((key) => !Object.prototype.hasOwnProperty.call(data, key))
  ) return null;
  const integerFields = [
    "underdog_kills",
    "opponent_kills",
    "kill_deficit",
  ] as const;
  if (integerFields.some((key) => data[key] != null && !Number.isInteger(data[key]))) {
    return null;
  }
  if (
    data.underdog_net_worth != null
    || data.opponent_net_worth != null
    || data.net_worth_deficit != null
  ) return null;
  const economyMinimum = data.net_worth_deficit_min;
  const economyMaximum = data.net_worth_deficit_max;
  const hasEconomyRange = Number.isInteger(economyMinimum) && Number.isInteger(economyMaximum);
  if (
    (economyMinimum == null) !== (economyMaximum == null)
    || (hasEconomyRange && data.net_worth_advantage_side !== "radiant" && data.net_worth_advantage_side !== "dire")
    || (!hasEconomyRange && data.net_worth_advantage_side != null)
  ) return null;
  if (hasEconomyRange) {
    if (radiantTeamSide !== "team_one" && radiantTeamSide !== "team_two") return null;
    const minimum = Number(economyMinimum);
    const maximum = Number(economyMaximum);
    const leaderIsUnderdog = (data.net_worth_advantage_side === "radiant")
      === (data.underdog_side === radiantTeamSide);
    const rawMinimum = leaderIsUnderdog ? -maximum : minimum;
    const rawMaximum = leaderIsUnderdog ? -minimum : maximum;
    if (
      !canonicalEconomyBucket(rawMinimum, rawMaximum)
      || (leaderIsUnderdog ? maximum > 0 : minimum < 0)
    ) return null;
  }
  if (data.source != null && safeCode(data.source) === null) return null;
  if (data.unavailable_reason != null && safeCode(data.unavailable_reason) === null) {
    return null;
  }
  return data as unknown as StrategyComebackStateInput;
}

function parseEntryWindow(value: unknown): StrategyEntryWindowInput | null {
  const data = recordValue(value);
  return Number.isInteger(data.minimum_clock_seconds)
    && Number(data.minimum_clock_seconds) >= 0
    && Number.isInteger(data.maximum_clock_seconds)
    && Number(data.maximum_clock_seconds) >= Number(data.minimum_clock_seconds)
    && Number.isInteger(data.game_clock_seconds)
    && Number(data.game_clock_seconds) >= 0
    && typeof data.inside === "boolean"
    ? data as unknown as StrategyEntryWindowInput
    : null;
}

function parseComebackEntry(
  value: unknown,
  window: StrategyEntryWindowInput | null,
): StrategyComebackEntryInput | null {
  const data = recordValue(value);
  const policy = recordValue(data.policy);
  const numeric = (key: string): number | null => numberValue(policy[key]);
  const minimumClock = numeric("minimum_clock_seconds");
  const maximumClock = numeric("maximum_clock_seconds");
  const minimumKills = numeric("minimum_kill_deficit");
  const maximumKills = numeric("maximum_kill_deficit");
  const minimumNetWorth = numeric("minimum_net_worth_deficit");
  const maximumNetWorth = numeric("maximum_net_worth_deficit");
  const minimumConfidence = numeric("minimum_vision_confidence");
  const policyValid = [
    minimumClock,
    maximumClock,
    minimumKills,
    maximumKills,
    minimumNetWorth,
    maximumNetWorth,
  ].every((item) => item !== null && Number.isInteger(item) && item >= 0)
    && minimumClock! <= maximumClock!
    && minimumKills! <= maximumKills!
    && minimumNetWorth! <= maximumNetWorth!
    && minimumConfidence !== null
    && finiteUnit(minimumConfidence)
    && (window === null || (
      window.minimum_clock_seconds === minimumClock
      && window.maximum_clock_seconds === maximumClock
    ));
  return typeof data.eligible === "boolean"
    && safeCode(data.reason) !== null
    && (data.rosh_underdog_probability == null || finiteUnit(data.rosh_underdog_probability))
    && policyValid
    ? data as unknown as StrategyComebackEntryInput
    : null;
}

function deficitDescription(value: number | null, metric: string): string {
  if (value == null) return `弱势方${metric}差不可用`;
  const amount = Math.abs(Math.round(value)).toLocaleString("zh-CN");
  return `弱势方${metric}${value >= 0 ? "落后" : "领先"} ${amount}`;
}

function economyDeficitDescription(state: StrategyComebackStateInput): string {
  const minimum = state.net_worth_deficit_min;
  const maximum = state.net_worth_deficit_max;
  if (minimum == null || maximum == null) return "弱势方经济差不可用";
  const trailing = maximum <= 0 ? "领先" : "落后";
  const displayMinimum = maximum <= 0 ? Math.abs(maximum) : minimum;
  const displayMaximum = maximum <= 0 ? Math.abs(minimum) : maximum;
  return `弱势方经济${trailing} ${displayMinimum.toLocaleString("zh-CN")}-${displayMaximum.toLocaleString("zh-CN")}`;
}

function canonicalEconomyBucket(minimum: number, maximum: number): boolean {
  return minimum >= 0
    && minimum % 1_000 === 0
    && maximum === minimum + 999;
}

function parseDecisionEvidence(decision: StrategyDecision): ParsedDecisionEvidence {
  let rawContributions: unknown = decision.contributions;
  let rawConservative: unknown = decision.conservative_contributions;
  let rawInputs: unknown = decision.inputs;

  if (rawContributions == null && decision.contributions_json != null) {
    if (utf8ByteLength(decision.contributions_json) > MAX_LEGACY_CONTRIBUTIONS_JSON_BYTES) {
      return {
        contributions: [],
        conservative: [],
        inputs: {},
        invalidReason: "contributions_json_too_large",
      };
    }
    try {
      const parsed: unknown = JSON.parse(decision.contributions_json);
      if (!isRecord(parsed)) throw new Error("not_object");
      rawContributions = parsed;
      rawInputs = rawInputs ?? parsed.__inputs__;
      const parsedInputs = recordValue(parsed.__inputs__);
      rawConservative = rawConservative ?? parsedInputs.conservative_contributions;
    } catch {
      return {
        contributions: [],
        conservative: [],
        inputs: {},
        invalidReason: "invalid_contributions_json",
      };
    }
  }

  const contributionResult = finiteNumberEntries(rawContributions, new Set(["__inputs__"]));
  const conservativeResult = finiteNumberEntries(rawConservative);
  if (contributionResult.invalid || conservativeResult.invalid) {
    return {
      contributions: [],
      conservative: [],
      inputs: {},
      invalidReason: "invalid_contribution_value",
    };
  }
  return {
    contributions: contributionResult.entries,
    conservative: conservativeResult.entries,
    inputs: recordValue(rawInputs),
    invalidReason: null,
  };
}

function finiteNumberEntries(
  value: unknown,
  ignored = new Set<string>(),
): { entries: Array<[string, number]>; invalid: boolean } {
  if (value == null) return { entries: [], invalid: false };
  if (!isRecord(value)) return { entries: [], invalid: true };
  const entries: Array<[string, number]> = [];
  for (const [key, item] of Object.entries(value)) {
    if (ignored.has(key)) continue;
    if (typeof item !== "number" || !Number.isFinite(item)) {
      return { entries: [], invalid: true };
    }
    entries.push([key, item]);
  }
  return { entries, invalid: false };
}

function formatSigned(value: number): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(3)}`;
}

function conservativeValue(values: Array<[string, number]>, key: string): number | null {
  return values.find(([name]) => name === key)?.[1] ?? null;
}

function LineupAnalysis({
  detail,
  error,
  match,
}: {
  detail: MatchDetail | null;
  error: string | null;
  match: MonitorMatch;
}) {
  const section = normalizeLineupSection(
    sectionOrFallback(detail?.analysis?.lineup, error),
  );
  const vision = normalizeVisionSection(
    sectionOrFallback(detail?.analysis?.vision, error),
  );
  const data = section.status === "available" ? section.data : null;

  return (
    <section className="workspace-section lineup-section">
      <div className="section-heading compact">
        <div>
          <h2>阵容分析</h2>
          <p>只显示可信完整阵容与因果时点可用的条件胜率</p>
        </div>
        <AnalysisState lifecycle={match.lifecycle} section={section} />
      </div>
      {data ? (
        <LineupContent data={data} match={match} vision={vision} />
      ) : (
        <AnalysisEmpty lifecycle={match.lifecycle} section={section} subject="阵容分析" />
      )}
    </section>
  );
}

function LineupContent({
  data,
  match,
  vision,
}: {
  data: LineupAnalysisData;
  match: MonitorMatch;
  vision: AnalysisSection<VisionAnalysisData>;
}) {
  const teamOneIsRadiant = data.radiant_team_side === "team_one";
  const teamOne = teamOneIsRadiant ? data.radiant : data.dire;
  const teamTwo = teamOneIsRadiant ? data.dire : data.radiant;
  const gameClockSeconds = vision.status === "available"
    ? vision.data?.game_clock_seconds ?? null
    : null;
  const curveSection = normalizeCurveSection(data.active_curve, gameClockSeconds);
  const curve = curveSection.status === "available" ? curveSection.data : null;
  const scores = data.scores;
  const players = data.players;

  return (
    <div className="lineup-content">
      <div className="lineup-sides">
        <LineupTeam
          dotaSide={teamOneIsRadiant ? "Radiant" : "Dire"}
          name={match.team_one || "队伍一"}
          side={teamOne}
        />
        <LineupTeam
          dotaSide={teamOneIsRadiant ? "Dire" : "Radiant"}
          name={match.team_two || "队伍二"}
          side={teamTwo}
        />
      </div>
      <RoshLineupScores data={data} match={match} section={scores} />
      <dl className="lineup-evidence">
        <div><dt>局数</dt><dd>第 {data.map_number} 局</dd></div>
        <div><dt>阵容证据时间</dt><dd>{formatDateTime(data.evidence.anchored_at || data.as_of)}</dd></div>
        <div><dt>阵容置信度</dt><dd>{formatPercent(vision.data?.draft_confidence)}</dd></div>
        <div><dt>Strict mapping</dt><dd><code>{data.evidence.strict_mapping_id}</code></dd></div>
        <div><dt>画面引用</dt><dd><code>{visionFrameReference(data.evidence.anchor_source_frame_ref) || "未提供"}</code></dd></div>
      </dl>
      <div className="curve-heading">
        <div>
          <h3>阵容条件胜率曲线</h3>
          <p>每个点都以比赛达到该分钟为条件；未来点不是当前胜率。</p>
        </div>
        <AnalysisState lifecycle={match.lifecycle} section={curveSection} />
      </div>
      {curve ? (
        <CurvePoints curve={curve} data={data} match={match} />
      ) : (
        <AnalysisEmpty lifecycle={match.lifecycle} section={curveSection} subject="阵容曲线" />
      )}
      <div className="player-identity-row">
        <div>
          <strong>实时选手身份</strong>
          <code>{players.reason}</code>
        </div>
        <AnalysisState lifecycle={match.lifecycle} section={players} />
        <span>
          {players.status === "unavailable"
            ? "实时选手身份不可用；系统不会根据队名或历史阵容猜测选手。"
            : reasonDescription[players.reason] || "选手证据按来源状态显示。"}
        </span>
      </div>
      {players.status === "available" && players.data && (
        <div className="player-identity-grid" role="list" aria-label="Rosh 选手身份">
          {players.data.players.map((player) => (
            <div key={`${player.side}:${player.position}`} role="listitem">
              <strong>{player.side === "radiant" ? "Radiant" : "Dire"} P{player.position}</strong>
              <span>英雄 {player.hero_id}</span>
              <code>{player.steam_account_id == null ? "Steam ID 不可用" : `Steam ${player.steam_account_id}`}</code>
              <small>{playerIdentityStatusLabel(player.status)}</small>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function playerIdentityStatusLabel(
  status: LivePlayerIdentityData["players"][number]["status"],
): string {
  if (status === "resolved") return "已用于选手修正";
  if (status === "selected_unresolved") return "身份可信，修正数据不可用";
  return "身份不可用";
}

function RoshLineupScores({
  data,
  match,
  section,
}: {
  data: LineupAnalysisData;
  match: MonitorMatch;
  section: AnalysisSection<RoshLineupScoresData>;
}) {
  const scores = section.status === "available" ? section.data : null;
  const fallback = scores?.mode === "pure";
  return (
    <div className="rosh-score-block">
      <div className="curve-heading">
        <div>
          <h3>Rosh 阵容评分</h3>
          <p>正值代表 Radiant 优势，负值代表 Dire 优势。</p>
        </div>
        <AnalysisState lifecycle={match.lifecycle} section={section} />
      </div>
      {scores ? (
        <>
          <dl className="rosh-score-grid">
            <div>
              <dt>纯阵容评分</dt>
              <dd>{formatRoshScore(scores.pure_lineup_score)}</dd>
              <small>{roshAdvantageLabel(scores.pure_lineup_score, data, match)}</small>
            </div>
            <div>
              <dt>选手修正后实际阵容评分</dt>
              <dd>
                {scores.player_adjusted_lineup_score == null
                  ? "不可用"
                  : formatRoshScore(scores.player_adjusted_lineup_score)}
              </dd>
              <small>
                {fallback
                  ? `回退采用纯阵容评分 · 仓位上限 ${formatPercent(scores.stake_cap)}`
                  : `${roshAdvantageLabel(scores.effective_lineup_score, data, match)} · 选手覆盖 ${formatPercent(scores.player_coverage)}`}
              </small>
            </div>
          </dl>
          <dl className="lineup-evidence rosh-score-evidence">
            <div><dt>实际采用</dt><dd>{fallback ? "纯阵容回退" : "选手修正评分"}</dd></div>
            <div><dt>有效评分</dt><dd>{formatRoshScore(scores.effective_lineup_score)}</dd></div>
            <div><dt>选手覆盖</dt><dd>{formatPercent(scores.player_coverage)} ({scores.player_coverage_count}/10)</dd></div>
            <div><dt>公式版本</dt><dd><code>{scores.formula_version}</code></dd></div>
            <div><dt>数据时间</dt><dd>{formatDateTime(scores.source_as_of)}</dd></div>
            <div><dt>Score key</dt><dd><code>{scores.score_key}</code></dd></div>
            <div><dt>Player identity</dt><dd><code>{scores.player_identity_hash}</code></dd></div>
            <div><dt>Evidence hash</dt><dd><code>{scores.evidence_hash}</code></dd></div>
          </dl>
        </>
      ) : (
        <AnalysisEmpty lifecycle={match.lifecycle} section={section} subject="Rosh 阵容评分" />
      )}
    </div>
  );
}

function formatRoshScore(value: number): string {
  return `${value > 0 ? "+" : ""}${value.toFixed(1)} pp`;
}

function roshAdvantageLabel(
  score: number,
  data: LineupAnalysisData,
  match: MonitorMatch,
): string {
  if (Math.abs(score) < 0.05) return "阵容评分均衡";
  const radiantName = data.radiant_team_side === "team_one"
    ? match.team_one || "队伍一"
    : match.team_two || "队伍二";
  const direName = data.radiant_team_side === "team_one"
    ? match.team_two || "队伍二"
    : match.team_one || "队伍一";
  return `${score > 0 ? radiantName : direName} 阵容占优`;
}

function LineupTeam({
  dotaSide,
  name,
  side,
}: {
  dotaSide: "Radiant" | "Dire";
  name: string;
  side: LineupSide;
}) {
  return (
    <section className="lineup-team" aria-label={`${name} 阵容`}>
      <div className="lineup-team-heading">
        <strong>{name}</strong>
        <span>{dotaSide}</span>
      </div>
      <div className="hero-grid">
        {side.hero_ids.map((heroId) => (
          <div className="hero-slot" key={heroId}>
            <span>{heroLabel(side, heroId)}</span>
            <code>ID {heroId}</code>
          </div>
        ))}
      </div>
    </section>
  );
}

function heroLabel(side: LineupSide, heroId: number): string {
  const authoritative = Array.isArray(side.heroes) ? side.heroes.find((hero) => (
    hero.hero_id === heroId && typeof hero.hero_name === "string" && hero.hero_name.trim()
  )) : undefined;
  return authoritative?.hero_name?.trim() || `英雄 ${heroId}`;
}

function CurvePoints({
  curve,
  data,
  match,
}: {
  curve: LineupCurveData;
  data: LineupAnalysisData;
  match: MonitorMatch;
}) {
  const teamOneIsRadiant = data.radiant_team_side === "team_one";
  const points = [...curve.points].sort((left, right) => left.horizon_minutes - right.horizon_minutes);
  return (
    <div className="curve-points" role="list" aria-label="阵容条件胜率点">
      {points.map((point) => {
        const teamOneProbability = teamOneIsRadiant
          ? point.radiant_probability
          : 1 - point.radiant_probability;
        return (
          <div className={point.active ? "curve-point active" : "curve-point"} key={point.landmark_key} role="listitem">
            <div className="curve-point-heading">
              <strong>达到 {point.horizon_minutes} 分钟</strong>
              <span>{point.active ? "当前可用检查点" : point.conditional ? "未来条件点" : "已过检查点"}</span>
            </div>
            <div className="curve-probability">
              <span>{match.team_one || "队伍一"} {formatPercent(teamOneProbability)}</span>
              <span>{match.team_two || "队伍二"} {formatPercent(1 - teamOneProbability)}</span>
            </div>
            <small>条件胜率 · 质量 {formatPercent(point.quality)} · 样本 {point.support}</small>
            <small>不确定度 {formatPercent(point.uncertainty)} · {point.model_version}</small>
          </div>
        );
      })}
    </div>
  );
}

function validOddsData(data: OddsAnalysisData | null): data is OddsAnalysisData {
  if (!data || !isRecord(data) || !Array.isArray(data.periods)) return false;
  return Number.isInteger(data.point_count)
    && data.point_count > 0
    && data.periods.length > 0
    && data.periods.every((period) => safeCode(period) !== null)
    && new Set(data.periods).size === data.periods.length
    && validTimestamp(data.latest_observed_at);
}

function validVisionData(data: VisionAnalysisData | null): data is VisionAnalysisData {
  if (!data || !isRecord(data)) return false;
  return Number.isInteger(data.map_number)
    && data.map_number > 0
    && validTimestamp(data.captured_at)
    && validGameClock(data.game_clock_seconds)
    && finiteUnit(data.clock_confidence)
    && finiteUnit(data.draft_confidence)
    && visionFrameReference(data.source_frame_ref) !== null;
}

function validStrategyData(data: StrategyAnalysisData | null): data is StrategyAnalysisData {
  if (!data || !isRecord(data) || !Array.isArray(data.decisions) || !data.decisions.length) {
    return false;
  }
  if (!data.decisions.every(validStrategyDecision) || !isRecord(data.excluded)) {
    return false;
  }
  const rawExcludedCounts = [
    data.excluded.vision_invalidated,
    data.excluded.mapping_impacted,
    data.excluded.draft_conflicted,
    data.excluded.invalid_payload,
  ];
  if (
    !nonnegativeInteger(data.displayed_count)
    || !nonnegativeInteger(data.scanned_count)
    || !nonnegativeInteger(data.excluded_decision_count)
    || !rawExcludedCounts.every(nonnegativeInteger)
    || data.count_scope !== "recent_scanned_window"
    || typeof data.has_more !== "boolean"
    || typeof data.truncated !== "boolean"
  ) {
    return false;
  }
  const excludedCounts = rawExcludedCounts as number[];
  const excludedReasonCountSum = excludedCounts.reduce(
    (sum, value) => sum + value,
    0,
  );
  return data.displayed_count === data.decisions.length
    && data.scanned_count >= data.displayed_count + data.excluded_decision_count
    && data.has_more === data.truncated
    && excludedCounts.every((value) => value <= data.excluded_decision_count)
    && excludedReasonCountSum >= data.excluded_decision_count;
}

function nonnegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

function validStrategyDecision(value: unknown): value is StrategyDecision {
  if (!isRecord(value)) return false;
  const decidedAt = value.decided_at;
  const underdogSide = value.underdog_side;
  const marketProbability = value.market_probability;
  const modelProbability = value.model_probability;
  const edge = value.edge;
  const dataQuality = value.data_quality;
  const eligible = value.eligible;
  const reason = safeCode(value.reason);
  const contributions = value.contributions;
  const conservative = value.conservative_contributions;
  const inputs = value.inputs;
  const draftAuthority = value.draft_authority;
  const visionAuthority = value.vision_authority;
  if (!(decisionReference(value.decision_key) !== null
    && validTimestamp(decidedAt)
    && Number.isInteger(value.map_number)
    && Number(value.map_number) > 0
    && (underdogSide === "team_one" || underdogSide === "team_two")
    && typeof marketProbability === "number"
    && Number.isFinite(marketProbability)
    && marketProbability >= 0
    && marketProbability <= 1
    && typeof modelProbability === "number"
    && Number.isFinite(modelProbability)
    && modelProbability >= 0
    && modelProbability <= 1
    && typeof edge === "number"
    && Number.isFinite(edge)
    && typeof dataQuality === "number"
    && Number.isFinite(dataQuality)
    && dataQuality >= 0
    && dataQuality <= 1
    && (eligible === 0 || eligible === 1)
    && reason !== null
    && safeCode(value.strategy_version) !== null
    && inputReference(value.input_ref) !== null
    && validContributionRecord(contributions)
    && validContributionRecord(conservative)
    && isRecord(inputs)
    && isRecord(draftAuthority)
    && isRecord(visionAuthority)
    && validDecisionReferences(value))) {
    return false;
  }
  if (!validDecisionEdge(edge, marketProbability, modelProbability)) return false;
  if ((eligible === 1) !== (reason === "eligible")) return false;
  const hasContributions = Object.keys(contributions).length > 0;
  if (eligible === 0 && !hasContributions) {
    return validNoSignalStrategyDecision({
      conservative,
      dataQuality,
      decidedAt,
      draftAuthority,
      edge,
      inputs,
      marketProbability,
      modelProbability,
      underdogSide,
      visionAuthority,
    });
  }
  if (
    value.strategy_version === COMEBACK_STRATEGY_V4
    && !validV4ComebackInputs(inputs, eligible === 1)
  ) return false;
  if (
    !hasRequiredStrategyContributions(contributions)
    || !hasRequiredStrategyContributions(conservative)
    || (Object.prototype.hasOwnProperty.call(contributions, "lineup_rosh")
      && (contributions.draft_curve !== 0 || conservative.draft_curve !== 0))
    || !validConservativeContributions(contributions, conservative)
    || !validDecisionReferences(value, true)
    || (Object.prototype.hasOwnProperty.call(contributions, "lineup_rosh")
      && !validRoshStrategyInput(
        inputs.rosh_lineup_score,
        eligible === 1,
        recordValue(inputs.vision).game_clock_seconds,
      ))
  ) {
    return false;
  }
  const inputConservative = inputs.conservative_contributions;
  const expectedModelProbability = strategyProbability(marketProbability, contributions);
  const expectedConservativeProbability = strategyProbability(
    marketProbability,
    conservative,
  );
  const conservativeProbability = numberValue(inputs.conservative_probability);
  const independentPositive = contributions.team_style + contributions.late_game_style > 0
    || contributions.player_form > 0
    || (contributions.lineup_rosh ?? contributions.draft_curve) > 0;
  return expectedModelProbability !== null
    && expectedConservativeProbability !== null
    && conservativeProbability !== null
    && finiteUnit(conservativeProbability)
    && sameContributionRecord(inputConservative, conservative)
    && approximatelyEqual(modelProbability, expectedModelProbability)
    && approximatelyEqual(conservativeProbability, expectedConservativeProbability)
    && inputs.independent_positive === independentPositive
    && (eligible === 0 || (
      conservativeProbability > marketProbability
      && independentPositive
    ));
}

function validV4ComebackInputs(
  inputs: Record<string, unknown>,
  finalEligible: boolean,
): boolean {
  const stateValue = inputs.comeback_state;
  const windowValue = inputs.entry_window;
  const entryValue = inputs.comeback_entry;
  const visionValue = inputs.vision;
  const marketValue = inputs.market;
  const roshValue = inputs.rosh_lineup_score;
  if (
    !isRecord(stateValue)
    || !isRecord(windowValue)
    || !isRecord(entryValue)
    || !isRecord(visionValue)
    || !isRecord(marketValue)
    || !isRecord(roshValue)
    || !isRecord(entryValue.policy)
    || !hasExactKeys(stateValue, [
      "controllable", "reason", "source_status", "source", "confidence",
      "underdog_side", "underdog_kills", "opponent_kills", "kill_deficit",
      "underdog_net_worth", "opponent_net_worth", "net_worth_deficit",
      "net_worth_advantage_side", "net_worth_deficit_min",
      "net_worth_deficit_max", "unavailable_reason",
    ])
    || !hasExactKeys(windowValue, [
      "minimum_clock_seconds", "maximum_clock_seconds", "game_clock_seconds", "inside",
    ])
    || !hasExactKeys(entryValue, ["eligible", "reason", "rosh_underdog_probability", "policy"])
    || !hasExactKeys(entryValue.policy, [
      "minimum_clock_seconds", "maximum_clock_seconds", "minimum_kill_deficit",
      "maximum_kill_deficit", "minimum_net_worth_deficit",
      "maximum_net_worth_deficit", "minimum_vision_confidence",
    ])
  ) return false;

  const vision = visionValue;
  const state = parseComebackState(
    stateValue,
    vision.radiant_team_side,
  );
  const window = parseEntryWindow(windowValue);
  const entry = parseComebackEntry(entryValue, window);
  if (!state || !window || !entry) return false;

  const policy = entry.policy;
  if (
    policy.minimum_clock_seconds !== 1_200
    || policy.maximum_clock_seconds !== 2_700
    || policy.minimum_kill_deficit !== 2
    || policy.maximum_kill_deficit !== 10
    || policy.minimum_net_worth_deficit !== 1_000
    || policy.maximum_net_worth_deficit !== 10_000
    || !approximatelyEqual(policy.minimum_vision_confidence, 0.9)
  ) return false;

  const expectedInside = window.game_clock_seconds >= policy.minimum_clock_seconds
    && window.game_clock_seconds <= policy.maximum_clock_seconds;
  if (
    window.minimum_clock_seconds !== policy.minimum_clock_seconds
    || window.maximum_clock_seconds !== policy.maximum_clock_seconds
    || window.inside !== expectedInside
    || vision.game_clock_seconds !== window.game_clock_seconds
    || marketValue.underdog_side !== state.underdog_side
  ) return false;

  const killsAvailable = Number.isInteger(state.underdog_kills)
    && Number(state.underdog_kills) >= 0
    && Number.isInteger(state.opponent_kills)
    && Number(state.opponent_kills) >= 0
    && Number.isInteger(state.kill_deficit);
  if (killsAvailable) {
    if (state.kill_deficit !== Number(state.opponent_kills) - Number(state.underdog_kills)) {
      return false;
    }
  } else if (
    state.underdog_kills != null
    || state.opponent_kills != null
    || state.kill_deficit != null
  ) return false;

  const economyAvailable = state.net_worth_advantage_side !== null;
  if (killsAvailable) {
    if (
      state.source_status !== "available"
      || state.source !== "vision_hud"
      || state.unavailable_reason !== null
      || state.confidence < policy.minimum_vision_confidence
    ) return false;
    const collapsed = Number(state.kill_deficit) > policy.maximum_kill_deficit
      || (economyAvailable
        && Number(state.net_worth_deficit_max) > policy.maximum_net_worth_deficit);
    const notMaterial = Number(state.kill_deficit) < policy.minimum_kill_deficit
      || (economyAvailable
        && Number(state.net_worth_deficit_min) < policy.minimum_net_worth_deficit);
    const expectedStateReason = !economyAvailable
      ? "vision_net_worth_evidence_missing"
      : collapsed ? "vision_situation_collapsed"
        : notMaterial ? "underdog_deficit_not_material" : "controlled_deficit";
    if (
      state.reason !== expectedStateReason
      || state.controllable !== (expectedStateReason === "controlled_deficit")
    ) return false;
  } else if (
    economyAvailable
    || state.controllable
    || [
      "controlled_deficit",
      "vision_situation_collapsed",
      "underdog_deficit_not_material",
    ].includes(state.reason)
  ) return false;

  const selectedScore = numberValue(roshValue.selected_score);
  let expectedRoshProbability: number | null = null;
  if (roshValue.selected_score != null) {
    if (
      selectedScore === null
      || (vision.radiant_team_side !== "team_one" && vision.radiant_team_side !== "team_two")
    ) return false;
    const radiantProbability = Math.min(
      1 - PROBABILITY_EPSILON,
      Math.max(PROBABILITY_EPSILON, (50 + selectedScore) / 100),
    );
    expectedRoshProbability = state.underdog_side === vision.radiant_team_side
      ? radiantProbability
      : 1 - radiantProbability;
  }
  if (
    expectedRoshProbability === null
      ? entry.rosh_underdog_probability !== null
      : entry.rosh_underdog_probability === null
        || !approximatelyEqual(entry.rosh_underdog_probability, expectedRoshProbability)
  ) return false;

  let expectedEntryReason = state.reason;
  if (state.controllable) {
    expectedEntryReason = !expectedInside
      ? "comeback_entry_outside_time_window"
      : expectedRoshProbability === null ? "rosh_direction_unavailable"
        : expectedRoshProbability <= 0.5 ? "rosh_direction_opposes_underdog" : "eligible";
  }
  const expectedEntryEligible = expectedEntryReason === "eligible";
  return entry.eligible === expectedEntryEligible
    && entry.reason === expectedEntryReason
    && (!finalEligible || expectedEntryEligible);
}

function hasExactKeys(value: Record<string, unknown>, keys: string[]): boolean {
  const actual = Object.keys(value);
  return actual.length === keys.length && keys.every((key) => Object.prototype.hasOwnProperty.call(value, key));
}

function validRoshStrategyInput(
  value: unknown,
  eligible: boolean,
  gameClockSeconds: unknown,
): boolean {
  if (!isRecord(value)) return false;
  const actualStake = numberValue(value.actual_stake_multiplier);
  const compatibilityStake = numberValue(value.stake_multiplier);
  if (value.status === "unavailable") {
    return !eligible
      && value.draft_matches_observation === false
      && actualStake === 0
      && compatibilityStake === 0
      && value.selected_score == null
      && value.selected_minute == null
      && value.selected_table == null
      && value.match_percentage == null;
  }
  const stakeCap = numberValue(value.stake_cap);
  const coverage = numberValue(value.player_coverage);
  const coverageCount = numberValue(value.player_coverage_count);
  const pureScore = numberValue(value.pure_score);
  const effectiveScore = numberValue(value.effective_score);
  const adjustedScore = value.player_adjusted_score == null
    ? null
    : numberValue(value.player_adjusted_score);
  if (
    actualStake === null
    || compatibilityStake === null
    || stakeCap === null
    || coverage === null
    || coverageCount === null
    || !Number.isInteger(coverageCount)
    || coverageCount < 0
    || coverageCount > 10
    || !finiteUnit(coverage)
    || !approximatelyEqual(coverage, coverageCount / 10)
    || pureScore === null
    || effectiveScore === null
    || !approximatelyEqual(actualStake, compatibilityStake)
    || typeof value.draft_matches_observation !== "boolean"
    || safeCode(value.formula_version) === null
    || !validTimestamp(value.source_as_of)
  ) {
    return false;
  }
  const playerAdjusted = value.mode === "player_adjusted"
    && coverageCount === 10
    && adjustedScore !== null
    && approximatelyEqual(stakeCap, 1);
  const pure = value.mode === "pure"
    && coverageCount < 10
    && value.player_adjusted_score == null
    && approximatelyEqual(stakeCap, 0.5);
  if (!playerAdjusted && !pure) return false;

  const selectedScore = numberValue(value.selected_score);
  if (value.selected_score == null) {
    return !eligible
      && actualStake === 0
      && compatibilityStake === 0
      && value.selected_minute == null
      && value.selected_table == null
      && value.match_percentage == null;
  }
  const selectedMinute = numberValue(value.selected_minute);
  const matchPercentage = numberValue(value.match_percentage);
  if (
    value.draft_matches_observation !== true
    || selectedScore === null
    || selectedMinute === null
    || !Number.isInteger(selectedMinute)
    || selectedMinute < 20
    || selectedMinute > 60
    || !validGameClock(gameClockSeconds)
    || selectedMinute > Math.floor(gameClockSeconds / 60)
    || matchPercentage === null
    || matchPercentage < 0
    || matchPercentage > 100
  ) return false;
  if (playerAdjusted) {
    return value.selected_table === "minute_table"
      && approximatelyEqual(actualStake, 1);
  }
  return value.selected_table === "pure_minute_table"
    && actualStake >= 0.1
    && actualStake <= 0.5;
}

function hasRequiredStrategyContributions(value: Record<string, number>): boolean {
  return contributionKeys(value).every(
    (key) => Object.prototype.hasOwnProperty.call(value, key),
  );
}

function contributionKeys(value: Record<string, number>): readonly string[] {
  return Object.prototype.hasOwnProperty.call(value, "lineup_rosh")
    ? STRATEGY_CONTRIBUTION_KEYS
    : LEGACY_STRATEGY_CONTRIBUTION_KEYS;
}

function validContributionRecord(value: unknown): value is Record<string, number> {
  return isRecord(value)
    && Object.entries(value).every(([key, item]) => (
      STRATEGY_CONTRIBUTION_KEY_SET.has(key) && finiteNumber(item)
    ));
}

function sameContributionRecord(value: unknown, expected: Record<string, number>): boolean {
  return validContributionRecord(value)
    && Object.keys(value).length === Object.keys(expected).length
    && contributionKeys(expected).every((key) => (
      Object.prototype.hasOwnProperty.call(value, key) && value[key] === expected[key]
    ));
}

function validConservativeContributions(
  raw: Record<string, number>,
  conservative: Record<string, number>,
): boolean {
  if (!approximatelyEqual(conservative.market_movement, raw.market_movement)) {
    return false;
  }
  return contributionKeys(raw)
    .filter((key) => key !== "market_movement")
    .every((key) => {
      const rawValue = raw[key];
      const conservativeValue = conservative[key];
      return rawValue <= 0
        ? approximatelyEqual(conservativeValue, rawValue)
        : conservativeValue >= -STRATEGY_MATH_TOLERANCE
          && conservativeValue <= rawValue + STRATEGY_MATH_TOLERANCE;
    });
}

function strategyProbability(
  marketProbability: number,
  contributions: Record<string, number>,
): number | null {
  const bounded = Math.min(
    1 - PROBABILITY_EPSILON,
    Math.max(PROBABILITY_EPSILON, marketProbability),
  );
  const score = Math.log(bounded / (1 - bounded))
    + contributionKeys(contributions).reduce((sum, key) => sum + contributions[key], 0);
  if (!Number.isFinite(score)) return null;
  if (score >= 0) {
    const inverse = Math.exp(-score);
    return 1 / (1 + inverse);
  }
  const exponent = Math.exp(score);
  return exponent / (1 + exponent);
}

function approximatelyEqual(left: number, right: number): boolean {
  return Math.abs(left - right) <= Math.max(
    STRATEGY_MATH_TOLERANCE,
    STRATEGY_MATH_TOLERANCE * Math.max(Math.abs(left), Math.abs(right)),
  );
}

function validNoSignalStrategyDecision({
  conservative,
  dataQuality,
  decidedAt,
  draftAuthority,
  edge,
  inputs,
  marketProbability,
  modelProbability,
  underdogSide,
  visionAuthority,
}: {
  conservative: Record<string, number>;
  dataQuality: number;
  decidedAt: string;
  draftAuthority: Record<string, unknown>;
  edge: number;
  inputs: Record<string, unknown>;
  marketProbability: number;
  modelProbability: number;
  underdogSide: "team_one" | "team_two";
  visionAuthority: Record<string, unknown>;
}): boolean {
  const market = inputs.market;
  const vision = inputs.vision;
  if (!isRecord(market) || !isRecord(vision)) return false;
  const inputMarketProbability = numberValue(market.underdog_probability);
  const marketPrice = numberValue(market.underdog_price);
  const marketQuality = market.quality;
  const missingMarkets = market.missing_markets;
  const capturedAt = vision.captured_at;
  return Object.keys(conservative).length === 0
    && Object.keys(draftAuthority).length === 0
    && Object.keys(visionAuthority).length === 0
    && approximatelyEqual(modelProbability, marketProbability)
    && approximatelyEqual(edge, 0)
    && approximatelyEqual(dataQuality, 0)
    && market.underdog_side === underdogSide
    && inputMarketProbability !== null
    && finiteUnit(inputMarketProbability)
    && approximatelyEqual(inputMarketProbability, marketProbability)
    && marketPrice !== null
    && marketPrice > 1
    && finiteUnit(marketQuality)
    && Array.isArray(missingMarkets)
    && missingMarkets.every((item) => safeCode(item) !== null)
    && validTimestamp(capturedAt)
    && Date.parse(capturedAt) <= Date.parse(decidedAt)
    && visionFrameReference(vision.source_frame_ref) !== null
    && (vision.game_clock_seconds == null || validGameClock(vision.game_clock_seconds))
    && (vision.radiant_team_side == null
      || vision.radiant_team_side === "team_one"
      || vision.radiant_team_side === "team_two");
}

function validLineupData(data: LineupAnalysisData | null): data is LineupAnalysisData {
  if (!data || !isRecord(data)) return false;
  if (!validLineupSide(data.radiant) || !validLineupSide(data.dire)) return false;
  const combined = [...data.radiant.hero_ids, ...data.dire.hero_ids];
  return new Set(combined).size === 10
    && (data.radiant_team_side === "team_one" || data.radiant_team_side === "team_two")
    && Number.isInteger(data.map_number)
    && data.map_number > 0
    && validTimestamp(data.as_of)
    && isRecord(data.evidence)
    && typeof data.evidence.draft_hash === "string"
    && SHA256_RE.test(data.evidence.draft_hash)
    && visionFrameReference(data.evidence.anchor_source_frame_ref) !== null
    && validTimestamp(data.evidence.anchored_at)
    && Number.isInteger(data.evidence.strict_mapping_id)
    && data.evidence.strict_mapping_id > 0
    && (data.scores === undefined || validAnalysisSection(data.scores))
    && validAnalysisSection(data.active_curve)
    && validAnalysisSection(data.players);
}

function validRoshScoresData(
  data: RoshLineupScoresData | null,
): data is RoshLineupScoresData {
  if (!data || !isRecord(data)) return false;
  const pure = numberValue(data.pure_lineup_score);
  const adjusted = data.player_adjusted_lineup_score == null
    ? null
    : numberValue(data.player_adjusted_lineup_score);
  const effective = numberValue(data.effective_lineup_score);
  const multiplier = numberValue(data.stake_multiplier);
  if (
    pure === null
    || effective === null
    || multiplier === null
    || !finiteUnit(data.player_coverage)
    || !Number.isInteger(data.player_coverage_count)
    || Number(data.player_coverage_count) < 0
    || Number(data.player_coverage_count) > 10
    || !approximatelyEqual(data.player_coverage, data.player_coverage_count / 10)
    || safeCode(data.formula_version) === null
    || !validTimestamp(data.source_as_of)
    || typeof data.score_key !== "string"
    || !SHA256_RE.test(data.score_key)
    || typeof data.player_identity_hash !== "string"
    || !SHA256_RE.test(data.player_identity_hash)
    || typeof data.evidence_hash !== "string"
    || !SHA256_RE.test(data.evidence_hash)
    || !finiteUnit(data.stake_cap)
    || !approximatelyEqual(data.stake_multiplier, data.stake_cap)
  ) {
    return false;
  }
  if (data.mode === "pure") {
    return adjusted === null
      && data.player_coverage_count < 10
      && approximatelyEqual(effective, pure)
      && approximatelyEqual(multiplier, 0.5);
  }
  return data.mode === "player_adjusted"
    && adjusted !== null
    && data.player_coverage_count === 10
    && approximatelyEqual(effective, adjusted)
    && approximatelyEqual(multiplier, 1);
}

function validLineupSide(value: unknown): value is LineupSide {
  if (!isRecord(value) || !validHeroIds(value.hero_ids)) return false;
  if (!Object.prototype.hasOwnProperty.call(value, "heroes")) return true;
  if (!Array.isArray(value.heroes)) return false;
  const ids = new Set(value.hero_ids);
  const metadataIds = new Set<number>();
  for (const hero of value.heroes) {
    if (
      !isRecord(hero)
      || !Number.isInteger(hero.hero_id)
      || !ids.has(Number(hero.hero_id))
      || metadataIds.has(Number(hero.hero_id))
      || !validOptionalHeroName(hero.hero_name)
    ) {
      return false;
    }
    metadataIds.add(Number(hero.hero_id));
  }
  return true;
}

function validOptionalHeroName(value: unknown): boolean {
  return value == null || (typeof value === "string" && Boolean(value.trim()));
}

function validHeroIds(value: unknown): value is number[] {
  return Array.isArray(value)
    && value.length === 5
    && value.every((hero) => Number.isInteger(hero) && hero > 0)
    && new Set(value).size === 5;
}

function validCurveData(
  data: LineupCurveData | null,
  gameClockSeconds: number,
): data is LineupCurveData {
  if (!data || !isRecord(data) || !Array.isArray(data.points) || !data.points.length) {
    return false;
  }
  if (
    typeof data.curve_key !== "string"
    || !SHA256_RE.test(data.curve_key)
    || !validTimestamp(data.first_usable_at)
    || !Number.isInteger(data.active_horizon_minutes)
    || data.active_horizon_minutes <= 0
  ) {
    return false;
  }
  const points = data.points as unknown[];
  if (!points.every(validCurvePoint)) return false;
  const typedPoints = points as LineupCurvePoint[];
  if (!typedPoints.every((point) => (
    point.conditional === (point.horizon_minutes * 60 > gameClockSeconds)
  ))) {
    return false;
  }
  const active = typedPoints.filter((point) => point.active);
  const currentHorizons = typedPoints
    .filter((point) => !point.conditional)
    .map((point) => point.horizon_minutes);
  const maximumCurrentHorizon = currentHorizons.length ? Math.max(...currentHorizons) : null;
  return active.length === 1
    && !active[0].conditional
    && active[0].horizon_minutes === data.active_horizon_minutes
    && active[0].horizon_minutes === maximumCurrentHorizon
    && gameClockSeconds / 60 - active[0].horizon_minutes <= 10
    && new Set(typedPoints.map((point) => point.horizon_minutes)).size === points.length
    && new Set(typedPoints.map((point) => point.landmark_key)).size === points.length;
}

function validCurvePoint(value: unknown): value is LineupCurvePoint {
  if (!isRecord(value)) return false;
  return typeof value.landmark_key === "string"
    && SHA256_RE.test(value.landmark_key)
    && Number.isInteger(value.horizon_minutes)
    && Number(value.horizon_minutes) > 0
    && finiteUnit(value.radiant_probability)
    && finiteUnit(value.quality)
    && Number.isInteger(value.support)
    && Number(value.support) >= 0
    && (value.uncertainty == null || finiteUnit(value.uncertainty))
    && safeCode(value.model_version) !== null
    && safeCode(value.validation_status) !== null
    && typeof value.conditional === "boolean"
    && typeof value.active === "boolean"
    && !(value.conditional && value.active);
}

function validPlayersData(
  data: LivePlayerIdentityData | null,
  lineup: LineupAnalysisData,
): data is LivePlayerIdentityData {
  if (!data || !isRecord(data) || !Array.isArray(data.players) || data.players.length !== 10) {
    return false;
  }
  const steamIds = new Set<number>();
  return data.players.every((player) => {
    if (!isRecord(player)) return false;
    const side = player.side;
    const position = player.position;
    const expected = side === "radiant" ? lineup.radiant.hero_ids : lineup.dire.hero_ids;
    const steamId = player.steam_account_id;
    if (
      (side !== "radiant" && side !== "dire")
      || !Number.isInteger(position)
      || Number(position) < 1
      || Number(position) > 5
      || player.hero_id !== expected[Number(position) - 1]
      || !["resolved", "selected_unresolved", "unavailable"].includes(String(player.status))
      || (player.status === "unavailable" && steamId !== null)
      || (player.status !== "unavailable" && !Number.isInteger(steamId))
      || (typeof steamId === "number" && (steamId <= 0 || steamIds.has(steamId)))
    ) {
      return false;
    }
    if (typeof steamId === "number") steamIds.add(steamId);
    return true;
  });
}

function validDecisionEdge(
  edge: unknown,
  marketProbability: unknown,
  modelProbability: unknown,
): boolean {
  if (!finiteNumber(edge) || edge < -1 || edge > 1) return false;
  if (!finiteNumber(marketProbability) || !finiteNumber(modelProbability)) return true;
  return approximatelyEqual(edge, modelProbability - marketProbability);
}

function finiteUnit(value: unknown): boolean {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
}

function finiteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function EvidenceSummary({
  detail,
  error,
  match,
}: {
  detail: MatchDetail | null;
  error: string | null;
  match: MonitorMatch;
}) {
  const latestVision = detail?.latest_vision || null;
  const latestCapture = detail?.latest_capture || null;
  const trustedFrameUrl = safeVisionFrameUrl(detail, latestVision);
  const captureFrameUrl = safeCaptureFrameUrl(detail, latestCapture);
  const frameUrl = trustedFrameUrl || captureFrameUrl;
  const displayedVision = trustedFrameUrl ? latestVision : latestCapture;
  const untrustedCapture = !trustedFrameUrl && captureFrameUrl !== null;
  const odds = normalizeOddsSection(
    sectionOrFallback(detail?.analysis?.odds, error),
  );
  const vision = normalizeVisionSection(
    sectionOrFallback(detail?.analysis?.vision, error),
  );
  const lineup = normalizeLineupSection(
    sectionOrFallback(detail?.analysis?.lineup, error),
  );
  const strategy = normalizeStrategySection(
    sectionOrFallback(detail?.analysis?.strategy, error),
  );
  const curve = lineup.status === "available" && lineup.data
    ? normalizeCurveSection(
      lineup.data.active_curve,
      vision.status === "available" ? vision.data?.game_clock_seconds ?? null : null,
    )
    : { status: lineup.status, reason: lineup.reason, data: null };
  const scores: AnalysisSection<RoshLineupScoresData> = lineup.status === "available" && lineup.data
    ? lineup.data.scores
    : { status: lineup.status, reason: lineup.reason, data: null };
  const mapping = readinessSection(detail?.readiness.mapping || match.readiness.mapping, match.lifecycle, "mapping");
  const workerReadiness = detail?.readiness.strategy || match.readiness.strategy;
  const worker = readinessSection(workerReadiness, match.lifecycle, "shadow_worker");
  const workerPresentation = shadowWorkerPresentation(workerReadiness.status);
  const inputCount = strategy.data?.decisions.filter((decision) => (
    isRecord(decision.inputs) && Object.keys(decision.inputs).length > 0
  )).length || 0;
  const modelInputs: AnalysisSection<null> = strategy.status === "available"
    ? inputCount > 0
      ? { status: "available", reason: "persisted_strategy_inputs", data: null }
      : { status: "review", reason: "strategy_inputs_missing", data: null }
    : { status: strategy.status, reason: strategy.reason, data: null };
  const excluded = strategy.data?.excluded;
  const excludedCount = strategy.data?.excluded_decision_count || 0;
  const excludedDetail = excluded
    ? `；原因明细（可重叠）：视觉失效 ${excluded.vision_invalidated}、映射影响 ${excluded.mapping_impacted}、阵容冲突 ${excluded.draft_conflicted}、无效载荷 ${excluded.invalid_payload}`
    : "";
  const diagnostics = [
    ["赔率", odds.reason],
    ["比赛映射", mapping.reason],
    ["视觉时钟", vision.reason],
    ["完整阵容", lineup.reason],
    ["Rosh 阵容评分", scores.reason],
    ["阵容曲线", curve.reason],
    ["模型输入", modelInputs.reason],
    ["策略进程", worker.reason],
    ["策略输出", strategy.reason],
  ];

  return (
    <section className="workspace-section evidence-section">
      <div className="section-heading compact">
        <div>
          <h2>证据摘要</h2>
          <p>来源状态、持久化数量与阻塞原因</p>
        </div>
        <Database size={19} aria-hidden="true" />
      </div>
      <div className="source-status-list">
        <SourceStatusRow
          detail={`${odds.data?.point_count ?? detail?.winner_timeline.length ?? 0} 点 · ${formatDateTime(odds.data?.latest_observed_at)}`}
          label="赔率"
          lifecycle={match.lifecycle}
          section={odds}
        />
        <SourceStatusRow
          detail={`${detail?.readiness.mapping.count ?? 0}/${detail?.readiness.mapping.total_count ?? 0} 个有效映射`}
          label="比赛映射"
          lifecycle={match.lifecycle}
          section={mapping}
        />
        <SourceStatusRow
          detail={vision.data
            ? `${formatDateTime(vision.data.captured_at)} · 阵容置信 ${formatPercent(vision.data.draft_confidence)}`
            : `${detail?.vision.length || 0} 条观测`}
          label="视觉时钟"
          lifecycle={match.lifecycle}
          section={vision}
        />
        <SourceStatusRow
          detail={lineup.data ? `第 ${lineup.data.map_number} 局 · 10 个英雄` : "无可信完整阵容"}
          label="完整阵容"
          lifecycle={match.lifecycle}
          section={lineup}
        />
        <SourceStatusRow
          detail={scores.data
            ? `${formatRoshScore(scores.data.effective_lineup_score)} · ${scores.data.mode === "pure" ? "纯阵容半仓回退" : `选手覆盖 ${formatPercent(scores.data.player_coverage)}`}`
            : "无持久化 Rosh 阵容评分"}
          label="Rosh 阵容评分"
          lifecycle={match.lifecycle}
          section={scores}
        />
        <SourceStatusRow
          detail={curve.data ? `${curve.data.points.length} 个条件点 · 当前 ${curve.data.active_horizon_minutes} 分钟` : "无可用曲线"}
          label="阵容曲线"
          lifecycle={match.lifecycle}
          section={curve}
        />
        <SourceStatusRow
          detail={`当前显示决策中 ${inputCount} 条有持久化输入`}
          label="模型输入"
          lifecycle={match.lifecycle}
          section={modelInputs}
        />
        <SourceStatusRow
          detail={workerPresentation.detail}
          label="策略进程"
          lifecycle={match.lifecycle}
          section={worker}
          statusLabel={workerPresentation.label}
        />
        <SourceStatusRow
          detail={`${strategy.data?.displayed_count ?? detail?.decisions.length ?? 0} 条已显示输出 · ${excludedCount} 条唯一排除${excludedDetail}`}
          label="策略输出"
          lifecycle={match.lifecycle}
          section={strategy}
        />
      </div>
      <details className="evidence-diagnostics">
        <summary>
          <TerminalWindow size={15} aria-hidden="true" />
          <span>系统诊断</span>
          <small>{diagnostics.length} 项原始状态</small>
        </summary>
        <dl>
          {diagnostics.map(([label, reason]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd><code>{reason || "reason_not_provided"}</code></dd>
            </div>
          ))}
        </dl>
      </details>
      {frameUrl ? (
        <VisionFramePreview
          frameUrl={frameUrl}
          untrusted={untrustedCapture}
          vision={displayedVision}
        />
      ) : (
        <div className="vision-frame-unavailable" role="status">
          <Eye size={16} aria-hidden="true" />
          <span>暂无可用的已捕获画面</span>
        </div>
      )}
    </section>
  );
}

function SourceStatusRow({
  detail,
  label,
  lifecycle,
  section,
  statusLabel,
}: {
  detail: string;
  label: string;
  lifecycle: MonitorMatch["lifecycle"];
  section: AnalysisSection<unknown>;
  statusLabel?: string;
}) {
  return (
    <div className="source-status-row">
      <strong>{label}</strong>
      <AnalysisState
        label={statusLabel || sourceStatusLabel(label, section.status, lifecycle)}
        lifecycle={lifecycle}
        section={section}
      />
      <span>{detail}</span>
    </div>
  );
}

function sourceStatusLabel(
  label: string,
  status: AnalysisSectionStatus,
  lifecycle: MonitorMatch["lifecycle"],
): string {
  if (status === "waiting" && lifecycle === "upcoming") return "等待开赛";
  const subject = {
    "赔率": "赔率",
    "比赛映射": "映射",
    "视觉时钟": "HUD",
    "完整阵容": "阵容",
    "Rosh 阵容评分": "评分",
    "阵容曲线": "曲线",
    "模型输入": "输入",
    "策略输出": "策略",
  }[label] || label;
  if (status === "available") {
    return {
      "比赛映射": "映射就绪",
      "视觉时钟": "HUD 已确认",
      "完整阵容": "阵容已确认",
      "模型输入": "输入已记录",
      "策略输出": "策略已生成",
    }[label] || `${subject}可用`;
  }
  if (status === "waiting") {
    return {
      "视觉时钟": "HUD 识别中",
      "完整阵容": "阵容识别中",
      "策略输出": "策略生成中",
    }[label] || `${subject}等待中`;
  }
  if (status === "review") return `${subject}需复核`;
  return `${subject}不可用`;
}

function readinessSection(
  readiness: MonitorMatch["readiness"]["odds"],
  lifecycle: MonitorMatch["lifecycle"],
  prefix: string,
): AnalysisSection<null> {
  const reason = readiness.reasons?.join(",") || `${prefix}_${readiness.status}`;
  if (readiness.status === "ready") return { status: "available", reason, data: null };
  if (readiness.status === "invalid") return { status: "review", reason, data: null };
  if (["unhealthy", "stopped", "degraded"].includes(readiness.status)) {
    return { status: "unavailable", reason, data: null };
  }
  return {
    status: lifecycle === "ended" ? "unavailable" : "waiting",
    reason,
    data: null,
  };
}

type StrategyReadinessStatus = MonitorMatch["readiness"]["strategy"]["status"];

const shadowWorkerPresentations: Record<
  StrategyReadinessStatus,
  { label: string; detail: string }
> = {
  ready: { label: "进程运行", detail: "策略进程运行中" },
  delayed: { label: "延迟", detail: "策略进程心跳延迟" },
  stale: { label: "陈旧", detail: "策略进程心跳陈旧" },
  missing: { label: "缺失", detail: "策略进程心跳缺失" },
  invalid: { label: "数据无效", detail: "策略进程数据无效" },
  unconfirmed: { label: "等待确认", detail: "策略进程等待确认" },
  degraded: { label: "降级", detail: "策略进程降级运行" },
  unhealthy: { label: "故障", detail: "策略进程运行异常" },
  stopped: { label: "未运行", detail: "策略进程未运行" },
};

function shadowWorkerPresentation(status: unknown): { label: string; detail: string } {
  if (
    typeof status === "string"
    && Object.prototype.hasOwnProperty.call(shadowWorkerPresentations, status)
  ) {
    return shadowWorkerPresentations[status as StrategyReadinessStatus];
  }
  return { label: "状态未知", detail: "策略进程状态未知" };
}

function VisionFramePreview({
  frameUrl,
  untrusted,
  vision,
}: {
  frameUrl: string;
  untrusted: boolean;
  vision: MatchDetail["latest_vision"];
}) {
  const [failedFrameUrl, setFailedFrameUrl] = useState<string | null>(null);
  const [zoomed, setZoomed] = useState(false);
  const failed = failedFrameUrl === frameUrl;
  const hudClockRecognized = untrusted
    && vision?.screen_state === "game"
    && typeof vision.game_clock_seconds === "number"
    && typeof vision.clock_confidence === "number"
    && vision.clock_confidence >= 0.9;
  const captureStatus = hudClockRecognized
    ? "HUD 时钟已识别，完整阵容未确认"
    : "已捕获，HUD 未识别";
  if (failed) {
    return (
      <div className="vision-frame-unavailable" role="status">
        <WarningCircle size={16} aria-hidden="true" />
        <span>已捕获画面加载失败</span>
      </div>
    );
  }
  return (
    <div
      className="vision-frame-preview"
      aria-label={untrusted ? "最近捕获但未确认的画面" : "最近有效视觉观测"}
    >
      {untrusted ? (
        <div className="vision-frame-untrusted" role="status">
          <WarningCircle size={16} aria-hidden="true" />
          <span>{captureStatus}</span>
          <strong>未确认，不能进入策略</strong>
        </div>
      ) : null}
      <button
        aria-label={untrusted ? "放大最近捕获但未确认的画面" : "放大最近有效视觉观测画面"}
        className="vision-frame-stage vision-frame-button"
        onClick={() => setZoomed(true)}
        type="button"
      >
        <img
          alt={untrusted ? "最近捕获但未确认的画面" : "最近有效视觉观测画面"}
          onError={() => setFailedFrameUrl(frameUrl)}
          src={frameUrl}
        />
        <span className="vision-frame-zoom-hint"><ArrowsOutSimple size={14} />放大</span>
      </button>
      <dl className="vision-frame-meta">
        <div><dt>捕获时间</dt><dd>{vision ? formatDateTime(vision.captured_at) : "无"}</dd></div>
        <div><dt>确认状态</dt><dd>{untrusted ? "未确认，不能进入策略" : vision ? (vision.confirmed === 1 ? "已确认" : "未确认") : "无观测"}</dd></div>
        <div><dt>画面状态</dt><dd>{vision?.screen_state || "未识别"}</dd></div>
      </dl>
      {zoomed && (
        <Dialog modalType="modal" onOpenChange={(_, data) => setZoomed(data.open)} open>
          <DialogSurface className="vision-frame-dialog">
            <DialogBody>
              <DialogTitle
                action={(
                  <Button
                    appearance="subtle"
                    aria-label="关闭画面预览"
                    icon={<X size={18} />}
                    onClick={() => setZoomed(false)}
                  />
                )}
              >
                {untrusted ? "未确认画面" : "最近有效视觉观测"}
              </DialogTitle>
              <DialogContent>
                {untrusted && (
                  <div className="vision-frame-untrusted" role="status">
                    <WarningCircle size={16} aria-hidden="true" />
                    <span>{captureStatus}</span>
                    <strong>未确认，不能进入策略</strong>
                  </div>
                )}
                <div className="vision-frame-dialog-stage">
                  <img
                    alt={untrusted ? "放大的未确认画面" : "放大的最近有效视觉观测画面"}
                    onError={() => setFailedFrameUrl(frameUrl)}
                    src={frameUrl}
                  />
                </div>
              </DialogContent>
            </DialogBody>
          </DialogSurface>
        </Dialog>
      )}
    </div>
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function validAnalysisSection(value: unknown): value is AnalysisSection<unknown> {
  return isRecord(value)
    && ["available", "waiting", "unavailable", "review"].includes(String(value.status))
    && typeof value.reason === "string"
    && Object.prototype.hasOwnProperty.call(value, "data");
}

function recordValue(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function numberValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function validTimestamp(value: unknown): value is string {
  return typeof value === "string"
    && Boolean(value.trim())
    && Number.isFinite(Date.parse(value));
}

function validGameClock(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) >= 0;
}

function safeCode(value: unknown): string | null {
  return opaqueReference(value);
}

function visionFrameReference(value: unknown): string | null {
  return typeof value === "string" && VISION_FRAME_REF_RE.test(value) ? value : null;
}

function draftReference(value: unknown): string | null {
  return typeof value === "string"
    && (SHA256_RE.test(value) || PROSPECTIVE_DRAFT_REF_RE.test(value))
    ? value
    : null;
}

function inputReference(value: unknown): string | null {
  return typeof value === "string" && INPUT_REF_RE.test(value) ? value : null;
}

function decisionReference(value: unknown): string | null {
  return typeof value === "string" && DECISION_KEY_RE.test(value) ? value : null;
}

function opaqueReference(value: unknown): string | null {
  return typeof value === "string"
    && OPAQUE_REF_RE.test(value)
    && !UNSAFE_OPAQUE_REF_RE.test(value)
    ? value
    : null;
}

function firstAllowedReference(
  value: unknown,
  keys: string[],
  allow: (candidate: unknown) => string | null,
): string | null {
  const record = recordValue(value);
  for (const key of keys) {
    const result = allow(record[key]);
    if (result) return result;
  }
  return null;
}

function firstDraftReference(value: unknown, keys: string[]): string | null {
  return firstAllowedReference(value, keys, draftReference);
}

function firstVisionFrameReference(value: unknown, keys: string[]): string | null {
  return firstAllowedReference(value, keys, visionFrameReference);
}

function validDecisionReferences(
  value: Record<string, unknown>,
  requireAuthority = value.eligible === 1,
): boolean {
  const draftAuthority = recordValue(value.draft_authority);
  const visionAuthority = recordValue(value.vision_authority);
  const draftKeys = ["source_ref", "landmark_key", "curve_key", "draft_hash"];
  const visionKeys = ["source_frame_ref", "frame_ref", "observation_key"];
  const presentDraftReferences = draftKeys
    .filter((key) => Object.prototype.hasOwnProperty.call(draftAuthority, key))
    .map((key) => draftAuthority[key]);
  const presentVisionReferences = visionKeys
    .filter((key) => Object.prototype.hasOwnProperty.call(visionAuthority, key))
    .map((key) => visionAuthority[key]);
  const inputs = recordValue(value.inputs);
  const visionInput = recordValue(inputs.vision);
  const inputFrameReference = Object.prototype.hasOwnProperty.call(visionInput, "source_frame_ref")
    ? visionInput.source_frame_ref
    : null;
  const referencesAreValid = presentDraftReferences.every(
    (reference) => draftReference(reference) !== null,
  ) && presentVisionReferences.every(
    (reference) => visionFrameReference(reference) !== null,
  ) && (inputFrameReference === null || visionFrameReference(inputFrameReference) !== null);
  if (!referencesAreValid) return false;
  return !requireAuthority || (
    presentDraftReferences.length > 0
    && presentVisionReferences.length > 0
  );
}

function utf8ByteLength(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function safeVisionFrameUrl(
  detail: MatchDetail | null,
  vision: MatchDetail["latest_vision"],
): string | null {
  const digest = vision?.frame_digest;
  const url = vision?.frame_url;
  if (
    !detail
    || !vision
    || typeof digest !== "string"
    || !/^[0-9a-f]{64}$/.test(digest)
    || typeof url !== "string"
    || vision.source_frame_ref !== `vision-frame:sha256:${digest}`
  ) {
    return null;
  }
  const expected = `/api/monitor/matches/${encodeURIComponent(detail.raybet_match_id)}`
    + `/vision-frames/${digest}.jpg`;
  return url === expected ? url : null;
}

function safeCaptureFrameUrl(
  detail: MatchDetail | null,
  capture: MatchDetail["latest_capture"],
): string | null {
  const digest = capture?.frame_digest;
  const url = capture?.frame_url;
  if (
    !detail
    || !capture
    || capture.strategy_authority !== false
    || typeof digest !== "string"
    || !/^[0-9a-f]{64}$/.test(digest)
    || typeof url !== "string"
    || capture.source_frame_ref !== `vision-frame:sha256:${digest}`
  ) {
    return null;
  }
  const expected = `/api/monitor/matches/${encodeURIComponent(detail.raybet_match_id)}`
    + `/captures/${digest}.jpg`;
  return url === expected ? url : null;
}

function MarketDrawer({ detail }: { detail: MatchDetail | null }) {
  const markets = detail?.markets || [];
  const grouped = Object.entries(
    markets.reduce<Record<string, typeof markets>>((result, market) => {
      const key = `${market.period} / ${market.market_type}`;
      (result[key] ||= []).push(market);
      return result;
    }, {}),
  );
  return (
    <details className="market-drawer">
      <summary>
        <span>其他盘口</span>
        <small>{markets.length} 条最新报价</small>
      </summary>
      <div className="market-groups">
        {grouped.length ? grouped.map(([name, quotes]) => (
          <section key={name} className="market-group">
            <h3>{name}</h3>
            <div className="market-quotes">
              {quotes.map((quote) => (
                <div key={quote.odds_id} className="market-quote">
                  <span>{quote.outcome_key}</span>
                  <strong>{formatOdds(quote.price)}</strong>
                  <small>{formatDateTime(quote.received_at)}</small>
                </div>
              ))}
            </div>
          </section>
        )) : <div className="subtle-empty">暂无其他盘口</div>}
      </div>
    </details>
  );
}

function WorkspaceSkeleton() {
  return (
    <Skeleton className="workspace-skeleton" aria-label="正在加载赛事详情">
      <SkeletonItem shape="rectangle" size={128} />
      <SkeletonItem shape="rectangle" size={128} />
      <div className="skeleton-grid">
        <SkeletonItem shape="rectangle" size={96} />
        <SkeletonItem shape="rectangle" size={96} />
      </div>
    </Skeleton>
  );
}
