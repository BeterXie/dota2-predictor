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
import { lazy, Suspense } from "react";

import {
  formatAge,
  formatClock,
  formatDateTime,
  formatOdds,
  formatPercent,
} from "../format";
import type { MatchDetail, MonitorMatch } from "../types";
import { LifecycleBadge } from "./StatusBadge";

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
          {match.live_url && (
            <Button
              appearance="subtle"
              as="a"
              href={match.live_url}
              icon={<ArrowSquareOut size={16} />}
              rel="noreferrer"
              target="_blank"
            >
              打开直播
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
          <strong>{detail?.latest_vision?.map_number ? `第 ${detail.latest_vision.map_number} 局` : winner?.period || "局数待确认"}</strong>
          <small>
            {detail?.latest_vision?.game_clock_seconds != null
              ? `可信时钟 ${formatClock(detail.latest_vision.game_clock_seconds)}`
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
                preferredPeriod={winner?.period || null}
              />
            </Suspense>
          </section>

          <section className="workspace-lower-grid">
            <DecisionTimeline decisions={detail?.decisions || []} />
            <EvidenceSummary detail={detail} />
          </section>

          <MarketDrawer detail={detail} />
        </>
      )}
    </main>
  );
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

function DecisionTimeline({ decisions }: { decisions: MatchDetail["decisions"] }) {
  const latest = decisions.slice(-12).reverse();
  return (
    <section className="workspace-section decision-section">
      <div className="section-heading compact">
        <div>
          <h2>策略判断</h2>
          <p>{decisions.length} 条已记录</p>
        </div>
        <ClockCounterClockwise size={19} aria-hidden="true" />
      </div>
      {latest.length ? (
        <div className="decision-list">
          {latest.map((decision) => (
            <div className="decision-row" key={decision.decision_key || `${decision.decided_at}-${decision.reason}`}>
              <div>
                <span>{formatDateTime(decision.decided_at)}</span>
                <code>{decision.reason}</code>
              </div>
              <div className="decision-values">
                <span>模型 {formatPercent(decision.model_probability)}</span>
                <span>市场 {formatPercent(decision.market_probability)}</span>
                <strong className={decision.edge > 0 ? "positive" : ""}>
                  {decision.edge > 0 ? "+" : ""}{formatPercent(decision.edge)}
                </strong>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="subtle-empty">这场比赛尚无策略判断</div>
      )}
    </section>
  );
}

function EvidenceSummary({ detail }: { detail: MatchDetail | null }) {
  const latestVision = detail?.vision.at(-1);
  return (
    <section className="workspace-section evidence-section">
      <div className="section-heading compact">
        <div>
          <h2>证据摘要</h2>
          <p>只显示已持久化来源</p>
        </div>
        <Database size={19} aria-hidden="true" />
      </div>
      <dl className="evidence-list">
        <div>
          <dt>赔率快照</dt>
          <dd>{detail?.winner_timeline.length || 0} 个完整点</dd>
        </div>
        <div>
          <dt>视觉观测</dt>
          <dd>{detail?.vision.length || 0} 条</dd>
        </div>
        <div>
          <dt>最近画面</dt>
          <dd>{latestVision ? formatDateTime(latestVision.captured_at) : "无"}</dd>
        </div>
        <div>
          <dt>画面状态</dt>
          <dd className="inline-value">
            <Eye size={15} aria-hidden="true" />
            {latestVision?.screen_state || "未识别"}
          </dd>
        </div>
      </dl>
    </section>
  );
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
