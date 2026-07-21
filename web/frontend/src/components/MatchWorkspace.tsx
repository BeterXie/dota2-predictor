import { Button, Skeleton, SkeletonItem } from "@fluentui/react-components";
import {
  ArrowSquareOut,
  ChartLineUp,
  Clock,
  ClockCounterClockwise,
  Database,
  Eye,
  WarningCircle,
} from "@phosphor-icons/react";
import { lazy, Suspense, useMemo, useState } from "react";

import {
  formatAge,
  formatClock,
  formatDateTime,
  formatOdds,
  formatPercent,
} from "../format";
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
  StrategyDecision,
  VisionAnalysisData,
} from "../types";
import { LifecycleBadge } from "./StatusBadge";
import { PostmatchIntelligencePanel } from "./PostmatchIntelligencePanel";

const RAYBET_PAGE_HOSTS = new Set(["ray086.com", "www.ray086.com"]);
const RAYBET_PAGE_PREFIXES = ["/sports/esports", "/esports", "/dota2"];
const PUBLIC_STREAM_HOSTS = new Set([
  "play.ehome.gg",
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

  const winner = detail?.winner || match.winner;
  const observedAge = winner?.observed_at
    ? Math.max(0, (now - new Date(winner.observed_at).getTime()) / 1000)
    : null;
  const vision = detail?.latest_vision;
  const visionStatus = (detail || match).readiness.vision.status;
  const trustedVision = vision?.confirmed === 1
    && (visionStatus === "ready" || visionStatus === "delayed")
    ? vision
    : null;
  const watchLink = safeWatchLink(match.watch_link);

  return (
    <main className="workspace" aria-live="polite">
      <header className="match-header">
        <div className="match-title-block">
          <div className="match-kicker">
            <span>{match.tournament || "未知赛事"}</span>
            <LifecycleBadge lifecycle={match.lifecycle} />
          </div>
          <h1>
            <span>{match.team_one || "队伍一"}</span>
            <small>VS</small>
            <span>{match.team_two || "队伍二"}</span>
          </h1>
          <div className="match-meta">
            <span>RayBet {match.raybet_match_id}</span>
            <span>BO{match.best_of || "?"}</span>
            <span>{formatDateTime(match.scheduled_at)}</span>
          </div>
        </div>
        <div className="match-header-actions">
          {watchLink && (
            <Button
              appearance="subtle"
              as="a"
              href={watchLink.url}
              icon={<ArrowSquareOut size={16} />}
              rel="noreferrer"
              target="_blank"
            >
              {watchLink.kind === "match_page" ? "打开比赛页" : "打开直播"}
            </Button>
          )}
          <span className={observedAge != null && observedAge > 60 ? "source-age stale" : "source-age"}>
            <Clock size={15} aria-hidden="true" />
            赔率 {formatAge(observedAge)}
          </span>
        </div>
      </header>

      <section className="quote-strip" aria-label="最新胜负盘">
        <QuoteCell
          label={match.team_one || "队伍一"}
          odds={winner?.prices?.team_one}
          probability={winner?.probabilities?.team_one}
          tone="one"
        />
        <div className="quote-context">
          <span>{replay ? "历史回放" : "实时胜负盘"}</span>
          <strong>{trustedVision?.map_number ? `第 ${trustedVision.map_number} 局` : winner?.period || "局数待确认"}</strong>
          <small>
            {trustedVision?.game_clock_seconds != null
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

      {loading && !detail ? (
        <WorkspaceSkeleton />
      ) : (
        <>
          <section className="workspace-section chart-section">
            <div className="section-heading">
              <div>
                <h2>市场概率与模型判断</h2>
                <p>横轴为真实采集时间。超过 60 秒的数据空档会断开曲线。</p>
              </div>
              <span className="method-note" title="双方概率已按完整胜负盘去除水位">
                去水概率
              </span>
            </div>
            <Suspense fallback={<div className="chart-empty"><span>正在加载概率图</span></div>}>
              <ProbabilityChart
                key={match.raybet_match_id}
                timeline={detail?.winner_timeline || []}
                decisions={detail?.decisions || []}
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
        </>
      )}
    </main>
  );
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
      <code>{section.reason || "reason_not_provided"}</code>
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

function DecisionRow({ decision }: { decision: StrategyDecision }) {
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
          <code>{decision.reason}</code>
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
    !hasRequiredStrategyContributions(contributions)
    || !hasRequiredStrategyContributions(conservative)
    || (Object.prototype.hasOwnProperty.call(contributions, "lineup_rosh")
      && (contributions.draft_curve !== 0 || conservative.draft_curve !== 0))
    || !validConservativeContributions(contributions, conservative)
    || !validDecisionReferences(value, true)
    || (Object.prototype.hasOwnProperty.call(contributions, "lineup_rosh")
      && !validRoshStrategyInput(inputs.rosh_lineup_score, eligible === 1))
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

function validRoshStrategyInput(value: unknown, eligible: boolean): boolean {
  if (!isRecord(value)) return false;
  const actualStake = numberValue(value.actual_stake_multiplier);
  const compatibilityStake = numberValue(value.stake_multiplier);
  if (value.status === "unavailable") {
    return !eligible
      && value.draft_matches_observation === false
      && actualStake === 0
      && compatibilityStake === 0
      && value.selected_score == null;
  }
  const stakeCap = numberValue(value.stake_cap);
  if (
    actualStake === null
    || compatibilityStake === null
    || stakeCap === null
    || !approximatelyEqual(actualStake, compatibilityStake)
    || value.draft_matches_observation !== true
    || safeCode(value.formula_version) === null
  ) {
    return false;
  }
  if (value.mode === "player_adjusted") {
    return approximatelyEqual(actualStake, 1) && approximatelyEqual(stakeCap, 1);
  }
  return value.mode === "pure"
    && approximatelyEqual(stakeCap, 0.5)
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
  const frameUrl = safeVisionFrameUrl(detail, latestVision);
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
          label="Strict mapping"
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
          detail={curve.data ? `${curve.data.points.length} 个条件点 · active ${curve.data.active_horizon_minutes} 分钟` : "无可用曲线"}
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
      {frameUrl ? (
        <VisionFramePreview frameUrl={frameUrl} vision={latestVision} />
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
      <AnalysisState label={statusLabel} lifecycle={lifecycle} section={section} />
      <span>{detail}</span>
      <code title={section.reason}>{section.reason || "reason_not_provided"}</code>
    </div>
  );
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
  ready: { label: "进程运行", detail: "shadow_worker 运行中" },
  delayed: { label: "延迟", detail: "shadow_worker 心跳延迟" },
  stale: { label: "陈旧", detail: "shadow_worker 心跳陈旧" },
  missing: { label: "缺失", detail: "shadow_worker 心跳缺失" },
  invalid: { label: "数据无效", detail: "shadow_worker 数据无效" },
  unconfirmed: { label: "等待确认", detail: "shadow_worker 等待确认" },
  degraded: { label: "降级", detail: "shadow_worker 降级" },
  unhealthy: { label: "故障", detail: "shadow_worker 故障" },
  stopped: { label: "未运行", detail: "shadow_worker 未运行" },
};

function shadowWorkerPresentation(status: unknown): { label: string; detail: string } {
  if (
    typeof status === "string"
    && Object.prototype.hasOwnProperty.call(shadowWorkerPresentations, status)
  ) {
    return shadowWorkerPresentations[status as StrategyReadinessStatus];
  }
  return { label: "状态未知", detail: "shadow_worker 状态未知" };
}

function VisionFramePreview({
  frameUrl,
  vision,
}: {
  frameUrl: string;
  vision: MatchDetail["latest_vision"];
}) {
  const [failedFrameUrl, setFailedFrameUrl] = useState<string | null>(null);
  const failed = failedFrameUrl === frameUrl;
  if (failed) {
    return (
      <div className="vision-frame-unavailable" role="status">
        <WarningCircle size={16} aria-hidden="true" />
        <span>已捕获画面加载失败</span>
      </div>
    );
  }
  return (
    <div className="vision-frame-preview" aria-label="最近有效视觉观测">
      <div className="vision-frame-stage">
        <img
          alt="最近有效视觉观测画面"
          onError={() => setFailedFrameUrl(frameUrl)}
          src={frameUrl}
        />
      </div>
      <dl className="vision-frame-meta">
        <div><dt>捕获时间</dt><dd>{vision ? formatDateTime(vision.captured_at) : "无"}</dd></div>
        <div><dt>确认状态</dt><dd>{vision ? (vision.confirmed === 1 ? "已确认" : "未确认") : "无观测"}</dd></div>
        <div><dt>画面状态</dt><dd>{vision?.screen_state || "未识别"}</dd></div>
      </dl>
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
