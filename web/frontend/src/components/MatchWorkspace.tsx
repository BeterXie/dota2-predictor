import { Skeleton, SkeletonItem, Tab, TabList } from "@fluentui/react-components";
import { ChartLineUp, WarningCircle } from "@phosphor-icons/react";
import { lazy, Suspense, useEffect, useState } from "react";

import { formatDateTime, formatOdds, formatPercent } from "../format";
import type {
  GameWorkspaceDetail,
  MatchDetail,
  MatchGameDetail,
  MatchGameState,
  MarketOnlyMapEvidence,
  MonitorMatch,
  OddsCoveragePhase,
  OddsCoverageSummary as OddsCoverageData,
  VisionPoint,
} from "../types";
import { LiveDataControls } from "./live/LiveDataControls";
import { LiveScoreboard } from "./live/LiveScoreboard";
import { PostmatchDetails } from "./PostmatchDetails";


const ProbabilityChart = lazy(() => import("./ProbabilityChart").then((module) => ({
  default: module.ProbabilityChart,
})));


interface MatchWorkspaceProps {
  match: MonitorMatch | null;
  detail: MatchDetail | null;
  loading: boolean;
  error: string | null;
  now?: number;
  replay: boolean;
  csrfToken?: string | null;
}


export function MatchWorkspace({
  match,
  detail,
  loading,
  error,
  now,
  replay,
  csrfToken = null,
}: MatchWorkspaceProps) {
  const [selectedMapNumber, setSelectedMapNumber] = useState<number | null>(null);
  useEffect(() => {
    setSelectedMapNumber(defaultGame(detail)?.map_number || null);
  }, [match?.raybet_match_id, detail?.raybet_match_id]);
  if (!match) {
    if (loading) {
      return (
        <main className="workspace">
          <WorkspaceSkeleton />
        </main>
      );
    }
    return (
      <main className="workspace workspace-empty">
        <ChartLineUp size={32} aria-hidden="true" />
        <h2>没有可显示的赛事</h2>
        <p>启动 RayBet 采集后，Dota 2 赛事会出现在这里。</p>
      </main>
    );
  }

  const selectedGame = selectedGameDetail(detail, selectedMapNumber);
  const gameDetail: GameWorkspaceDetail | null = detail && selectedGame
    ? { ...detail, ...selectedGame, current_map_number: selectedGame.map_number }
    : null;
  const latestVision = selectedGame?.latest_vision || null;
  const liveWinner = selectedGame?.winner || null;
  const seriesPrematchWinner = selectedGame
    ? null
    : detail?.prematch_winner || match.prematch_winner || null;
  const prematchWinner = liveWinner
    ? null
    : selectedGame?.prematch_winner || seriesPrematchWinner;
  const prematchMarketMapNumber = selectedGame
    ? null
    : marketMapNumber(prematchWinner?.period);
  const winner = liveWinner || prematchWinner;
  const isPrematchSnapshot = prematchWinner != null;
  const watchLink = safeWatchLink(match.watch_link, match.raybet_match_id);

  return (
    <main className="workspace">
      {detail && selectedGame && (
        <SeriesGameSwitcher
          games={detail.games}
          onSelect={setSelectedMapNumber}
          selected={selectedGame}
        />
      )}
      {detail && detail.market_evidence.length > 0 && (
        <MarketEvidence evidence={detail.market_evidence} />
      )}
      <section
        className={`match-decision-hero${isPrematchSnapshot ? " prematch" : ""}`}
        aria-label={isPrematchSnapshot ? "赛前赛事概览" : "赛事与市场概览"}
      >
        <LiveScoreboard
          match={match}
          now={now}
          oddsObservedAt={winner?.observed_at || null}
          oddsAgePrefix={isPrematchSnapshot ? "赛前赔率 " : "赔率 "}
          oddsSnapshotLabel={replay ? "历史归档" : null}
          gameState={selectedGame?.state || "unconfirmed"}
          mapNumber={selectedGame?.map_number || null}
          trustedVision={latestVision}
          watchLink={watchLink}
        />

        <section className="quote-strip" aria-label="最新胜负盘">
          <QuoteCell
            label={match.team_one || "队伍一"}
            odds={winner?.prices?.team_one}
            probability={winner?.probabilities?.team_one}
            side="one"
          />
          <div className="quote-context">
            <span>{replay
              ? "历史对局"
              : selectedGame?.state === "live"
                ? "实时对局"
                : isPrematchSnapshot ? "赛前快照" : gameStateLabel(selectedGame?.state)}</span>
            <strong>{latestVision?.map_number
              ? `第 ${latestVision.map_number} 局`
              : selectedGame
                ? `第 ${selectedGame.map_number} 局`
                : prematchMarketMapNumber
                  ? `第 ${prematchMarketMapNumber} 局盘口 · 尚未开打`
                  : "局数待确认"}</strong>
            <small>赔率仅用于赛事详情展示，不进入 P0/P1</small>
            {!selectedGame && match.readiness?.vision.reason === "waiting_for_watch_window" && (
              <small>Vision 将于 {formatDateTime(match.readiness.vision.watch_starts_at || null)} 自动开始直播探测</small>
            )}
            {!selectedGame && match.readiness?.vision.reason === "stream_probe_pending" && (
              <small>Vision 已进入直播探测窗口，等待可验证画面</small>
            )}
          </div>
          <QuoteCell
            label={match.team_two || "队伍二"}
            odds={winner?.prices?.team_two}
            probability={winner?.probabilities?.team_two}
            side="two"
          />
        </section>

        {error && (
          <div className="inline-error" role="alert">
            <WarningCircle size={18} aria-hidden="true" />
            <span>{error}</span>
          </div>
        )}
      </section>

      {selectedGame && (
        <section className="workspace-section chart-section" aria-label="市场概率走势">
          <div className="section-heading compact">
            <div>
              <h2>市场概率走势</h2>
              <p>每个点来自同一采集时刻的完整双方胜负盘；纵轴为去水概率，超过 150 秒的数据空档会断开曲线。</p>
            </div>
            <span className="method-note">去水概率 · 不进入 P0/P1</span>
          </div>
          <OddsCoverageSummary coverage={selectedGame.odds_coverage} />
          <Suspense fallback={<div className="chart-empty"><span>正在加载概率走势</span></div>}>
            <ProbabilityChart
              key={selectedGame.game_id}
              emptyDescription={isPrematchSnapshot
                ? "上方已显示最近一次完整赛前胜负盘；比赛开始并收到新快照后绘制实时走势"
                : undefined}
              emptyTitle={isPrematchSnapshot ? "实时走势尚未开始" : undefined}
              preferredPeriod={selectedGame.period}
              teamOne={match.team_one || "队伍一"}
              teamTwo={match.team_two || "队伍二"}
              timeline={selectedGame.winner_timeline}
            />
          </Suspense>
        </section>
      )}

      {loading && !detail ? <WorkspaceSkeleton /> : gameDetail && (
        <>
          <DecisionChain detail={gameDetail} />
          <PostmatchDetails postmatch={gameDetail.postmatch} />
          <LiveDataControls csrfToken={csrfToken} detail={gameDetail} readOnly={replay} />
          <VisionEvidence detail={gameDetail} latest={latestVision} />
        </>
      )}
    </main>
  );
}


function DecisionChain({ detail }: { detail: GameWorkspaceDetail }) {
  return (
    <section className="workspace-section" aria-label="Map 决策链">
      <div className="section-heading compact">
        <div>
          <h2>Map 决策链</h2>
          <p>{detail.game_id} · 固定 1 unit shadow</p>
        </div>
      </div>
      {detail.decision_checkpoints.length === 0 ? (
        <div className="chart-empty"><span>尚无检查点</span></div>
      ) : (
        <dl className="live-state-summary">
          {detail.decision_checkpoints.map((checkpoint) => {
            const liveModel = recordValue(
              checkpoint.feature_availability.live_probability_model,
            );
            const validation = recordValue(liveModel?.validation);
            const levels = recordValue(checkpoint.feature_availability.levels);
            const objectives = recordValue(checkpoint.feature_availability.objectives);
            const priorRadiant = numberValue(liveModel?.prior_radiant_probability);
            const goldCoefficient = numberValue(liveModel?.coefficient_per_1000_gold);
            const modelVersion = textValue(liveModel?.model_version)
              || textValue(checkpoint.input_versions.live_probability_model_version)
              || checkpoint.strategy_version;
            return (
              <div key={checkpoint.checkpoint_id}>
                <dt>{checkpoint.phase === "pregame" ? "赛前" : `${checkpoint.checkpoint_minute} 分钟`}</dt>
                <dd>
                  <span className="decision-trace-summary">
                    {decisionLabel(checkpoint.decision, detail)} · {checkpoint.reason}
                    {checkpoint.evaluation_eligible ? "" : ` · 排除离线评估 (${checkpoint.evaluation_exclusion_reason})`}
                    {checkpoint.settlement ? ` · ${checkpoint.settlement.outcome} ${checkpoint.settlement.profit_units.toFixed(2)}u` : ""}
                  </span>
                  <ul className="decision-trace-details">
                    <li>模型 {modelVersion} · 策略 {checkpoint.strategy_version} · mapping v{checkpoint.mapping_version ?? "-"}</li>
                    {checkpoint.model_probability_team_one != null && checkpoint.model_probability_team_two != null && (
                      <li>模型概率：{detail.team_one} {formatPercent(checkpoint.model_probability_team_one)} · {detail.team_two} {formatPercent(checkpoint.model_probability_team_two)}</li>
                    )}
                    {checkpoint.market_probability_team_one != null && checkpoint.market_probability_team_two != null && (
                      <li>市场概率：{detail.team_one} {formatPercent(checkpoint.market_probability_team_one)} · {detail.team_two} {formatPercent(checkpoint.market_probability_team_two)}</li>
                    )}
                    {checkpoint.selected_edge != null && (
                      <li>价值差 {formatPercent(checkpoint.selected_edge)}{checkpoint.observed_price == null ? "" : ` · 采用赔率 ${formatOdds(checkpoint.observed_price)}`}</li>
                    )}
                    <li>赔率年龄 {formatDuration(checkpoint.odds_age_seconds)} / {formatDuration(checkpoint.odds_max_age_seconds)} · Vision 年龄 {formatDuration(checkpoint.vision_age_seconds)} / {formatDuration(checkpoint.vision_max_age_seconds)}</li>
                    {checkpoint.odds_vision_gap_seconds != null && (
                      <li>赔率/Vision 时间差 {formatDuration(checkpoint.odds_vision_gap_seconds)} / {formatDuration(checkpoint.odds_vision_gap_max_seconds)}</li>
                    )}
                    {checkpoint.vision_game_time_seconds != null && (
                      <li>Vision：比赛 {formatDuration(checkpoint.vision_game_time_seconds)} · Radiant 经济 {formatSigned(checkpoint.vision_networth_lead)} · 击杀 {checkpoint.vision_radiant_kills ?? "-"}:{checkpoint.vision_dire_kills ?? "-"}</li>
                    )}
                    {priorRadiant != null && (
                      <li>赛前 Radiant 先验 {formatPercent(priorRadiant)} · 每千经济系数 {formatDecimal(goldCoefficient)}</li>
                    )}
                    {numberValue(validation?.holdout_brier) != null && (
                      <li>时间留出验证：n={numberValue(validation?.holdout_samples)} · Brier {formatDecimal(numberValue(validation?.holdout_brier))} / 基线 {formatDecimal(numberValue(validation?.baseline_brier))} · log loss {formatDecimal(numberValue(validation?.holdout_log_loss))} / 基线 {formatDecimal(numberValue(validation?.baseline_log_loss))}</li>
                    )}
                    {(levels || objectives) && (
                      <li>可选特征：等级 {availabilityText(levels)} · 目标 {availabilityText(objectives)}</li>
                    )}
                    {checkpoint.odds_observation_key && <li className="decision-trace-ref">赔率引用 {checkpoint.odds_observation_key}</li>}
                    {checkpoint.vision_source_frame_ref && <li className="decision-trace-ref">Vision 引用 {checkpoint.vision_source_frame_ref}{checkpoint.vision_snapshot_id == null ? "" : ` · snapshot #${checkpoint.vision_snapshot_id}`}</li>}
                  </ul>
                </dd>
              </div>
            );
          })}
        </dl>
      )}
    </section>
  );
}


function recordValue(value: unknown): Record<string, unknown> | null {
  return value != null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}


function numberValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}


function textValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}


function decisionLabel(
  decision: "bet_team_a" | "bet_team_b" | "skip",
  detail: GameWorkspaceDetail,
): string {
  if (decision === "bet_team_a") return `bet_team_a (${detail.team_one})`;
  if (decision === "bet_team_b") return `bet_team_b (${detail.team_two})`;
  return "skip";
}


function availabilityText(value: Record<string, unknown> | null): string {
  if (!value) return "未记录";
  if (value.available === true) return "可用";
  return textValue(value.reason) || "缺失";
}


function formatSigned(value: number | null): string {
  if (value == null) return "-";
  return `${value > 0 ? "+" : ""}${Math.round(value).toLocaleString("zh-CN")}`;
}


function formatDecimal(value: number | null): string {
  return value == null ? "-" : value.toFixed(3);
}


function SeriesGameSwitcher({
  games,
  onSelect,
  selected,
}: {
  games: MatchGameDetail[];
  onSelect: (mapNumber: number) => void;
  selected: MatchGameDetail;
}) {
  return (
    <section className="series-game-switcher" aria-label="系列赛对局">
      <div>
        <strong>BO 系列赛</strong>
        <span>{selected.official_match_id
          ? `${selected.game_id} · Official Match ${selected.official_match_id}`
          : `${selected.game_id} · unlinked · ${selected.link_reason}`}</span>
      </div>
      <TabList
        aria-label="选择独立对局"
        onTabSelect={(_event, data) => onSelect(Number(data.value))}
        selectedValue={String(selected.map_number)}
      >
        {games.map((game) => (
          <Tab key={game.game_id} value={String(game.map_number)}>
            第 {game.map_number} 局 · {gameStateLabel(game.state)}
          </Tab>
        ))}
      </TabList>
    </section>
  );
}


function MarketEvidence({ evidence }: { evidence: MarketOnlyMapEvidence[] }) {
  return (
    <section className="workspace-section market-evidence" aria-label="未开打盘口证据">
      <div className="section-heading compact">
        <div>
          <h2>市场证据</h2>
          <p>这些盘口尚无可信开局证据，因此不计为实际比赛。</p>
        </div>
      </div>
      <dl className="live-state-summary">
        {evidence.map((market) => (
          <div key={market.market_id}>
            <dt>第 {market.map_number} 局盘口</dt>
            <dd>market_only · {market.reason} · {market.odds_coverage.prematch.observation_count + market.odds_coverage.live.observation_count} 个采集时点</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}


function defaultGame(detail: MatchDetail | null): MatchGameDetail | null {
  if (!detail?.games.length) return null;
  return detail.games.find((game) => game.map_number === detail.current_map_number)
    || [...detail.games].reverse().find((game) => game.state === "ended")
    || detail.games[0];
}


function selectedGameDetail(
  detail: MatchDetail | null,
  selectedMapNumber: number | null,
): MatchGameDetail | null {
  if (!detail?.games.length) return null;
  return detail.games.find((game) => game.map_number === selectedMapNumber)
    || defaultGame(detail);
}


function gameStateLabel(state?: MatchGameState): string {
  if (state === "live") return "进行中";
  if (state === "ended") return "已结束";
  if (state === "scheduled") return "待开始";
  return "状态待确认";
}


function marketMapNumber(period?: string): number | null {
  const match = /^map_([1-5])$/.exec(period || "");
  return match ? Number(match[1]) : null;
}


function OddsCoverageSummary({ coverage }: { coverage: OddsCoverageData }) {
  return (
    <section className="odds-coverage" aria-label="赔率采集覆盖">
      <dl className="probability-chart-summary odds-coverage-summary">
        <div><dt>赛前赔率</dt><dd>{formatOddsPhase(coverage.prematch, coverage.gap_threshold_seconds)}</dd></div>
        <div><dt>滚球赔率</dt><dd>{formatOddsPhase(coverage.live, coverage.gap_threshold_seconds)}</dd></div>
        <div><dt>收盘赔率</dt><dd>{formatClosingCoverage(coverage)}</dd></div>
      </dl>
      <p className="odds-coverage-source">来源：RayBet 直连响应 · 仅统计同一采集时点双方完整且有效的胜负盘</p>
    </section>
  );
}


function formatOddsPhase(phase: OddsCoveragePhase, gapThreshold: number): string {
  if (phase.status === "pending") return "尚未开始";
  if (phase.status === "missing") return "缺失 · 未采到完整双方盘";
  const gap = phase.gap_count > 0
    ? `${phase.gap_count} 次断档，最长 ${formatDuration(phase.longest_gap_seconds)}`
    : `无超过 ${Math.round(gapThreshold)} 秒的断档`;
  return `${phase.complete_snapshot_count} 个完整盘口 · ${phase.observation_count} 个采集时点 · ${gap}`;
}


function formatClosingCoverage(coverage: OddsCoverageData): string {
  if (coverage.closing.status === "pending") return "比赛结束后确认";
  if (coverage.closing.status === "missing") return "缺失 · 未采到完整收盘盘";
  if (coverage.closing.status === "unconfirmed") {
    return `已采到末次盘 · 对局身份待确认 · ${formatDateTime(coverage.closing.observed_at)}`;
  }
  return `已记录 · ${formatDateTime(coverage.closing.observed_at)}`;
}


function formatDuration(seconds: number | null): string {
  if (seconds == null) return "-";
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.round(seconds % 60);
  return remainder ? `${minutes} 分 ${remainder} 秒` : `${minutes} 分`;
}


function safeWatchLink(link: MonitorMatch["watch_link"], raybetMatchId: string): {
  kind: "public_stream" | "match_page" | "stream_resolver";
  url: string;
} | null {
  if (!link || link.availability !== "available" || !link.url) return null;
  if (link.kind === "stream_resolver") {
    const expected = `/api/monitor/matches/${encodeURIComponent(raybetMatchId)}/live-stream`;
    return link.url === expected ? { kind: link.kind, url: link.url } : null;
  }
  if (link.kind === "public_stream" || link.kind === "match_page") {
    return { kind: link.kind, url: link.url };
  }
  return null;
}


function QuoteCell({
  label,
  odds,
  probability,
  side,
}: {
  label: string;
  odds: number | undefined;
  probability: number | undefined;
  side: "one" | "two";
}) {
  return (
    <div className={`quote-cell ${side}`}>
      <span>{label}</span>
      <strong>{formatOdds(odds)}</strong>
      <small>{probability == null ? "-" : formatPercent(probability)}</small>
    </div>
  );
}


function VisionEvidence({ detail, latest }: { detail: GameWorkspaceDetail; latest: VisionPoint | null }) {
  const snapshot = detail.latest_game_snapshot;
  const capture = detail.latest_capture;
  return (
    <section className="workspace-section" aria-label="HUD 与 Vision 证据">
      <div className="section-heading compact">
        <div>
          <h2>HUD 与 Vision 证据</h2>
          <p>保留英雄、时钟、比分、经济、画面状态和人工修正证据。</p>
        </div>
      </div>
      <dl className="live-state-summary">
        <div><dt>画面状态</dt><dd>{latest?.screen_state || "等待识别"}</dd></div>
        <div><dt>暂停</dt><dd>{latest?.is_paused == null ? "-" : latest.is_paused ? "是" : "否"}</dd></div>
        <div><dt>时钟置信度</dt><dd>{latest ? formatPercent(latest.clock_confidence) : "-"}</dd></div>
        <div><dt>阵容置信度</dt><dd>{latest ? formatPercent(latest.draft_confidence) : "-"}</dd></div>
        <div><dt>击杀</dt><dd>{snapshot ? `${snapshot.radiant_kills ?? "-"} : ${snapshot.dire_kills ?? "-"}` : "-"}</dd></div>
        <div><dt>经济差</dt><dd>{snapshot ? snapshot.networth_lead.toLocaleString("zh-CN") : "-"}</dd></div>
      </dl>
      {capture?.frame_url && (
        <a href={capture.frame_url} rel="noreferrer" target="_blank">查看最近直播截图</a>
      )}
    </section>
  );
}


function WorkspaceSkeleton() {
  return (
    <Skeleton className="workspace-skeleton" aria-label="正在加载赛事详情">
      <SkeletonItem shape="rectangle" size={128} />
      <SkeletonItem shape="rectangle" size={128} />
    </Skeleton>
  );
}
