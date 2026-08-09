import { ArrowSquareOut, Clock } from "@phosphor-icons/react";
import { formatClock, formatDateTime } from "../../format";
import type { MonitorMatch, WatchLink } from "../../types";
import { RelativeAge } from "../RelativeAge";
import { LifecycleBadge } from "../StatusBadge";

interface LiveScoreboardProps {
  match: MonitorMatch;
  trustedVision: { game_clock_seconds?: number | null; map_number?: number | null } | null;
  now?: number;
  oddsObservedAt: string | null;
  oddsAgePrefix: string;
  oddsSnapshotLabel: string | null;
  watchLink: { kind: WatchLink["kind"]; url: string } | null;
}

export function LiveScoreboard({
  match,
  trustedVision,
  now,
  oddsObservedAt,
  oddsAgePrefix,
  oddsSnapshotLabel,
  watchLink,
}: LiveScoreboardProps) {
  const isLive = match.lifecycle === "live";
  const isPrematch = match.lifecycle === "upcoming"
    || ["1", "upcoming", "scheduled", "not_started"]
      .includes(match.provider_status.trim().toLowerCase());
  const gameClock = trustedVision?.game_clock_seconds;
  const mapNum = trustedVision?.map_number;

  return (
    <section className="live-scoreboard-banner" aria-label="赛事概览">
      <div className="scoreboard-topline">
        <div className="scoreboard-meta">
          <strong>{match.tournament || "未知赛事"}</strong>
          <LifecycleBadge lifecycle={isPrematch ? "upcoming" : match.lifecycle} />
          <span className="scoreboard-best-of">BO{match.best_of || "?"}</span>
        </div>
        <div className="scoreboard-actions">
          {watchLink && (
            <a
              className="scoreboard-watch-link"
              href={watchLink.url}
              target="_blank"
              rel="noreferrer"
            >
              <ArrowSquareOut aria-hidden="true" size={15} />
              观看直播
            </a>
          )}
          {oddsSnapshotLabel ? (
            <span className="source-age" style={{ display: "inline-flex", alignItems: "center", gap: "4px", fontSize: "11px" }}>
              <Clock size={14} />
              {oddsSnapshotLabel}
            </span>
          ) : (
            <RelativeAge
              className="source-age"
              icon={<Clock size={14} />}
              now={now}
              observedAt={oddsObservedAt}
              prefix={oddsAgePrefix}
              staleAfterSeconds={60}
            />
          )}
        </div>
      </div>

      <div className="scoreboard-matchup">
        <div className="scoreboard-team team-one">
          <span>{match.team_one || "队伍一"}</span>
        </div>

        <div className="scoreboard-center">
          <div className={isLive ? "scoreboard-map live" : "scoreboard-map"}>
            {isLive && <span className="scoreboard-live-dot" />}
            {mapNum
              ? `第 ${mapNum} 局`
              : isPrematch ? "等待开赛" : match.winner?.period || "比分板"}
            {gameClock != null && ` · ${formatClock(gameClock)}`}
          </div>
          <span className="scoreboard-time">
            {formatDateTime(match.scheduled_at)}
          </span>
        </div>

        <div className="scoreboard-team team-two">
          <span>{match.team_two || "队伍二"}</span>
        </div>
      </div>
    </section>
  );
}
