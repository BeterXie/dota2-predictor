import { ArrowSquareOut, Clock } from "@phosphor-icons/react";
import { formatClock, formatDateTime } from "../../format";
import type { MonitorMatch, VisionAnalysisData } from "../../types";
import { LifecycleBadge } from "../StatusBadge";

interface LiveScoreboardProps {
  match: MonitorMatch;
  trustedVision: { game_clock_seconds?: number | null; map_number?: number | null } | null;
  oddsLabel: string;
  oddsStale: boolean;
  watchLink: { kind: string; url: string } | null;
}

export function LiveScoreboard({
  match,
  trustedVision,
  oddsLabel,
  oddsStale,
  watchLink,
}: LiveScoreboardProps) {
  const isLive = match.lifecycle === "live";
  const gameClock = trustedVision?.game_clock_seconds;
  const mapNum = trustedVision?.map_number;

  return (
    <div
      className="live-scoreboard-banner"
      style={{
        display: "grid",
        gap: "12px",
        padding: "16px 20px",
        marginBottom: "16px",
        borderRadius: "var(--radius, 8px)",
        background: "linear-gradient(135deg, rgba(17, 24, 32, 0.95), rgba(26, 33, 41, 0.98))",
        border: "1px solid var(--border-accent, #2a3746)",
        boxShadow: "0 4px 20px rgba(0, 0, 0, 0.35)",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: "12px", color: "var(--text-dim)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          <strong style={{ color: "var(--text)", fontSize: "13px" }}>{match.tournament || "未知赛事"}</strong>
          <LifecycleBadge lifecycle={match.lifecycle} />
          <span style={{ fontFamily: "var(--mono)", fontSize: "11px" }}>BO{match.best_of || "?"}</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
          {watchLink && (
            <a
              href={watchLink.url}
              target="_blank"
              rel="noreferrer"
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "5px",
                padding: "4px 10px",
                borderRadius: "4px",
                background: "var(--accent-soft, rgba(97, 206, 193, 0.15))",
                color: "var(--accent, #61cec1)",
                fontSize: "12px",
                textDecoration: "none",
                fontWeight: 600,
                border: "1px solid rgba(97, 206, 193, 0.3)",
              }}
            >
              <ArrowSquareOut size={15} />
              {watchLink.kind === "match_page" ? "打开比赛页" : "打开直播"}
            </a>
          )}
          <span className={oddsStale ? "source-age stale" : "source-age"} style={{ display: "inline-flex", alignItems: "center", gap: "4px", fontSize: "11px" }}>
            <Clock size={14} />
            {oddsLabel}
          </span>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr auto 1fr", alignItems: "center", gap: "16px", padding: "4px 0" }}>
        {/* Team One */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: "12px" }}>
          <span style={{ fontSize: "22px", fontWeight: 700, color: "var(--text)", textShadow: "0 2px 10px rgba(0,0,0,0.5)" }}>
            {match.team_one || "队伍一"}
          </span>
        </div>

        {/* Center Score & Timer */}
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "4px" }}>
          <div
            style={{
              padding: "4px 16px",
              borderRadius: "20px",
              background: "rgba(11, 16, 20, 0.7)",
              border: "1px solid var(--border)",
              fontSize: "13px",
              fontWeight: 700,
              letterSpacing: "0.05em",
              color: isLive ? "var(--positive, #69c58b)" : "var(--text-muted)",
              display: "flex",
              alignItems: "center",
              gap: "6px",
            }}
          >
            {isLive && <span style={{ width: "8px", height: "8px", borderRadius: "50%", background: "var(--positive, #69c58b)", boxShadow: "0 0 8px #69c58b" }} />}
            {mapNum ? `第 ${mapNum} 局` : match.winner?.period || "比分板"}
            {gameClock != null && ` · ${formatClock(gameClock)}`}
          </div>
          <span style={{ fontSize: "11px", color: "var(--text-dim)", fontFamily: "var(--mono)" }}>
            {formatDateTime(match.scheduled_at)}
          </span>
        </div>

        {/* Team Two */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-start", gap: "12px" }}>
          <span style={{ fontSize: "22px", fontWeight: 700, color: "var(--team-two, #ef8b79)", textShadow: "0 2px 10px rgba(0,0,0,0.5)" }}>
            {match.team_two || "队伍二"}
          </span>
        </div>
      </div>
    </div>
  );
}
