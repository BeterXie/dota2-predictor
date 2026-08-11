import { Button, Input } from "@fluentui/react-components";
import { CaretRight, MagnifyingGlass } from "@phosphor-icons/react";
import { useMemo, useState } from "react";

import { formatClock, formatDateTime, formatOdds } from "../format";
import type { MonitorMatch } from "../types";
import { RelativeAge } from "./RelativeAge";
import { LifecycleBadge } from "./StatusBadge";


type MatchGroupKey = "live" | "prematch" | "degraded" | "history";

const PREMATCH_PROVIDER_STATUSES = new Set([
  "1",
  "upcoming",
  "scheduled",
  "not_started",
]);

const GROUP_LABELS: Record<MatchGroupKey, string> = {
  live: "正在进行",
  prematch: "赛前赛事",
  degraded: "数据待恢复",
  history: "历史归档",
};

const GROUP_ORDER: MatchGroupKey[] = ["live", "prematch", "degraded", "history"];


interface MatchRailProps {
  hasMore?: boolean;
  loadError?: string | null;
  loadingMore?: boolean;
  matches: MonitorMatch[];
  mode: "live" | "history" | "recap";
  selectedId: string | null;
  onLoadMore?: () => void;
  onSelect: (matchId: string) => void;
  variant?: "rail" | "page";
}


export function MatchRail({
  hasMore = false,
  loadError = null,
  loadingMore = false,
  matches,
  mode,
  onLoadMore,
  onSelect,
  selectedId,
  variant = "rail",
}: MatchRailProps) {
  const historical = mode === "history" || mode === "recap";
  const recap = mode === "recap";
  const [query, setQuery] = useState("");
  const [mobileExpanded, setMobileExpanded] = useState(false);
  const visible = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase("zh-CN");
    const filtered = needle
      ? matches.filter((match) => [
        match.raybet_match_id,
        match.display_name,
        match.tournament,
        match.team_one,
        match.team_two,
      ]
        .some((value) => value?.toLocaleLowerCase("zh-CN").includes(needle)))
      : matches;
    return [...filtered].sort(compareMatches);
  }, [matches, query]);
  const groups = useMemo(() => GROUP_ORDER.flatMap((key) => {
    const groupMatches = visible.filter((match) => matchGroup(match, mode) === key);
    return groupMatches.length ? [{ key, matches: groupMatches }] : [];
  }), [mode, visible]);
  const selectedMatch = matches.find((match) => match.raybet_match_id === selectedId) || null;
  const Root = variant === "page" ? "main" : "aside";

  return (
    <Root
      className={variant === "page"
        ? `match-list-page${recap ? " recap-list" : ""}`
        : `match-rail${mobileExpanded ? " expanded" : ""}`}
      aria-label={variant === "page"
        ? recap ? "比赛复盘赛事列表" : historical ? "历史赛事列表" : "实时与赛前赛事列表"
        : recap ? "比赛复盘赛事" : historical ? "历史赛事" : "实时赛事"}
    >
      {variant === "rail" && (
        <button
          aria-expanded={mobileExpanded}
          className="mobile-rail-summary"
          onClick={() => setMobileExpanded((current) => !current)}
          type="button"
        >
          <span>{recap ? "选择比赛" : historical ? "选择历史赛事" : "切换实时赛事"}</span>
          <strong>{selectedMatch?.team_one || "未选择"}</strong>
          <span>VS</span>
          <strong>{selectedMatch?.team_two || "未选择"}</strong>
        </button>
      )}

      <div className="rail-body">
        <header className="rail-header">
          <div>
            <h2>{recap ? "比赛复盘" : historical ? "历史结果" : "实时与赛前"}</h2>
            <span className="rail-count">{visible.length} 场</span>
          </div>
          <Input
            aria-label="搜索赛事"
            className="match-search"
            contentBefore={<MagnifyingGlass size={15} aria-hidden="true" />}
            onChange={(_, data) => setQuery(data.value)}
            placeholder="搜索赛事或队伍"
            value={query}
          />
        </header>

        <nav
          className="match-groups"
          aria-label={recap ? "比赛复盘赛事分组" : historical ? "历史赛事分组" : "实时与赛前赛事分组"}
        >
          {groups.map((group) => (
            <section className="match-group" key={group.key}>
              <div className="match-group-title">
                <span>{recap && group.key === "history" ? "最近结束" : GROUP_LABELS[group.key]}</span>
                <span>{group.matches.length}</span>
              </div>
              <div className="match-group-items">
                {group.matches.map((match) => {
                  const selected = match.raybet_match_id === selectedId;
                  const prematch = isPrematchMatch(match);
                  const displayLifecycle = prematch ? "upcoming" : match.lifecycle;
                  const quote = match.winner?.complete
                    ? match.winner
                    : match.prematch_winner || match.winner;
                  const observedAt = quote?.observed_at || match.latest_odds_activity_at;
                  return (
                    <button
                      aria-pressed={selected}
                      className={`match-row${selected ? " selected" : ""}`}
                      key={match.raybet_match_id}
                      onClick={() => {
                        onSelect(match.raybet_match_id);
                        setMobileExpanded(false);
                      }}
                      type="button"
                    >
                      <div className="match-row-meta">
                        <div className="match-row-event">
                          <span className="tournament-name" title={match.tournament || "未知赛事"}>
                            {match.tournament || "未知赛事"}
                          </span>
                          <small>{recap
                            ? formatDateTime(match.scheduled_at)
                            : `RayBet Series ${match.raybet_match_id}`}</small>
                        </div>
                        <LifecycleBadge lifecycle={displayLifecycle} />
                      </div>
                      <div className="match-row-teams">
                        <div>
                          <span>{match.team_one || "队伍一"}</span>
                          <span className="versus">VS</span>
                          <span>{match.team_two || "队伍二"}</span>
                        </div>
                        {!recap && <small>{matchProgressLabel(match)}</small>}
                      </div>
                      <div className="match-row-market">
                        <strong>{recap
                          ? "查看比赛复盘"
                          : mode === "history"
                          ? "收盘快照"
                          : prematch ? "赛前快照" : "实时胜负盘"}</strong>
                        <span>{recap
                          ? "赛果、阵容与关键走势"
                          : quote
                          ? "完整双方报价"
                          : prematch ? "进入详情查看最近报价" : "等待完整双方报价"}</span>
                      </div>
                      <div className="match-row-prices">
                        <span>{recap ? match.best_of ? `BO${match.best_of}` : "赛制待确认" : formatOdds(quote?.prices?.team_one)}</span>
                        {!recap && <span>{formatOdds(quote?.prices?.team_two)}</span>}
                        <RelativeAge
                          className="age"
                          observedAt={recap ? match.updated_at : observedAt}
                          staleAfterSeconds={60}
                        />
                      </div>
                      <CaretRight className="match-row-enter" size={17} aria-hidden="true" />
                    </button>
                  );
                })}
              </div>
            </section>
          ))}

          {!visible.length && (
            <div className="rail-empty">
              <MagnifyingGlass size={24} aria-hidden="true" />
              <span>{query
                ? "没有匹配的赛事"
                : historical && loadingMore ? "正在加载历史赛事…" : "暂无赛事"}</span>
            </div>
          )}

          {historical && (hasMore || loadError) && (
            <div className="rail-pagination">
              {loadError && <p role="alert">{loadError}</p>}
              {hasMore && onLoadMore && (
                <Button
                  disabled={loadingMore}
                  onClick={onLoadMore}
                  type="button"
                >
                  {loadingMore ? "正在加载…" : "加载更多历史赛事"}
                </Button>
              )}
            </div>
          )}
        </nav>
      </div>
    </Root>
  );
}


function matchGroup(match: MonitorMatch, mode: MatchRailProps["mode"]): MatchGroupKey {
  if (mode === "history" || mode === "recap" || match.lifecycle === "ended") return "history";
  if (isPrematchMatch(match)) return "prematch";
  if (match.lifecycle === "live") return "live";
  return "degraded";
}


function isPrematchMatch(match: MonitorMatch): boolean {
  return match.lifecycle === "upcoming"
    || PREMATCH_PROVIDER_STATUSES.has(match.provider_status.trim().toLowerCase());
}


function compareMatches(left: MonitorMatch, right: MonitorMatch): number {
  const leftGroup = GROUP_ORDER.indexOf(matchGroup(left, "live"));
  const rightGroup = GROUP_ORDER.indexOf(matchGroup(right, "live"));
  if (leftGroup !== rightGroup) return leftGroup - rightGroup;
  if (isPrematchMatch(left) && isPrematchMatch(right)) {
    return timestamp(left.scheduled_at) - timestamp(right.scheduled_at);
  }
  return timestamp(right.updated_at || right.scheduled_at) - timestamp(left.updated_at || left.scheduled_at);
}


function matchProgressLabel(match: MonitorMatch): string {
  if (isPrematchMatch(match)) return `开赛 ${formatDateTime(match.scheduled_at)}`;
  const vision = match.latest_vision;
  if (vision?.map_number) {
    const clock = vision.game_clock_seconds == null
      ? "时钟待确认"
      : formatClock(vision.game_clock_seconds);
    return `第 ${vision.map_number} 局 ${clock}`;
  }
  if (match.lifecycle === "ended") return "比赛已结束";
  return "等待可信比赛时钟";
}


function timestamp(value: string | null | undefined): number {
  return Date.parse(value || "") || 0;
}
