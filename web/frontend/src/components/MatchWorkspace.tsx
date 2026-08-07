import { Skeleton, SkeletonItem } from "@fluentui/react-components";
import { ChartLineUp, WarningCircle } from "@phosphor-icons/react";
import { lazy, Suspense } from "react";

import { formatOdds, formatPercent } from "../format";
import type { MatchDetail, MonitorMatch, VisionPoint } from "../types";
import { LiveDataControls } from "./live/LiveDataControls";
import { LiveScoreboard } from "./live/LiveScoreboard";


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

  const latestVision = latestVisionPoint(detail);
  const winner = detail?.winner || match.winner;
  const watchLink = match.watch_link?.availability === "available" && match.watch_link.url
    ? { kind: match.watch_link.kind, url: match.watch_link.url }
    : null;

  return (
    <main className="workspace">
      <LiveScoreboard
        match={match}
        now={now}
        oddsObservedAt={winner?.observed_at || null}
        oddsSnapshotLabel={replay ? "历史归档" : null}
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
          <span>{replay ? "历史赛事" : "实时赛事"}</span>
          <strong>{latestVision?.map_number ? `第 ${latestVision.map_number} 局` : "局数待确认"}</strong>
          <small>赔率仅用于赛事详情展示，不进入 P0/P1</small>
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

      {detail && (
        <section className="workspace-section chart-section" aria-label="市场概率走势">
          <div className="section-heading compact">
            <div>
              <h2>市场概率走势</h2>
              <p>每个点来自同一采集时刻的完整双方胜负盘；纵轴为去水概率，超过 150 秒的数据空档会断开曲线。</p>
            </div>
            <span className="method-note">去水概率 · 不进入 P0/P1</span>
          </div>
          <Suspense fallback={<div className="chart-empty"><span>正在加载概率走势</span></div>}>
            <ProbabilityChart
              key={match.raybet_match_id}
              preferredPeriod={winner?.period || null}
              teamOne={match.team_one || "队伍一"}
              teamTwo={match.team_two || "队伍二"}
              timeline={detail.winner_timeline}
            />
          </Suspense>
        </section>
      )}

      {loading && !detail ? <WorkspaceSkeleton /> : detail && (
        <>
          <LiveDataControls csrfToken={csrfToken} detail={detail} readOnly={replay} />
          <VisionEvidence detail={detail} latest={latestVision} />
        </>
      )}
    </main>
  );
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


function VisionEvidence({ detail, latest }: { detail: MatchDetail; latest: VisionPoint | null }) {
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


function latestVisionPoint(detail: MatchDetail | null): VisionPoint | null {
  if (!detail) return null;
  return detail.vision.at(-1) || detail.latest_vision || null;
}


function WorkspaceSkeleton() {
  return (
    <Skeleton className="workspace-skeleton" aria-label="正在加载赛事详情">
      <SkeletonItem shape="rectangle" size={128} />
      <SkeletonItem shape="rectangle" size={128} />
    </Skeleton>
  );
}
