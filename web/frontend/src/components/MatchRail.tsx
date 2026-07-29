import { Input } from "@fluentui/react-components";
import { CaretRight, MagnifyingGlass } from "@phosphor-icons/react";
import { useMemo, useState } from "react";

import {
  ageSeconds,
  formatAge,
  formatClock,
  formatOdds,
  lifecycleLabel,
} from "../format";
import type { Lifecycle, MonitorMatch } from "../types";
import { LifecycleBadge } from "./StatusBadge";

const lifecycleOrder: Lifecycle[] = ["live", "degraded", "upcoming", "ended"];

interface MatchRailProps {
  matches: MonitorMatch[];
  mode: "live" | "history";
  selectedId: string | null;
  onSelect: (matchId: string) => void;
  now: number;
  historyHasMore?: boolean;
  historyLoading?: boolean;
  onLoadMore?: () => void;
  variant?: "rail" | "page";
}

export function MatchRail({
  matches,
  mode,
  selectedId,
  onSelect,
  now,
  historyHasMore = false,
  historyLoading = false,
  onLoadMore,
  variant = "rail",
}: MatchRailProps) {
  const [query, setQuery] = useState("");
  const [mobileExpanded, setMobileExpanded] = useState(false);
  const filtered = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase("zh-CN");
    if (!normalized) return matches;
    return matches.filter((match) =>
      [
        match.raybet_match_id,
        match.tournament,
        match.team_one,
        match.team_two,
      ].some((value) => String(value || "").toLocaleLowerCase("zh-CN").includes(normalized)),
    );
  }, [matches, query]);

  const Root = variant === "page" ? "main" : "aside";

  return (
    <Root
      className={variant === "page"
        ? "match-list-page"
        : `match-rail${mobileExpanded ? " expanded" : ""}`}
      aria-label={variant === "page"
        ? mode === "history" ? "历史赛事列表" : "实时赛事列表"
        : "赛事列表"}
    >
        {variant === "rail" && <button
          aria-expanded={mobileExpanded}
          className="mobile-rail-summary"
          onClick={() => setMobileExpanded((current) => !current)}
          type="button"
        >
          <span>{mode === "history" ? "选择历史赛事" : "切换实时赛事"}</span>
          <strong>{matches.find((item) => item.raybet_match_id === selectedId)?.team_one || "未选择"}</strong>
          <span>VS</span>
          <strong>{matches.find((item) => item.raybet_match_id === selectedId)?.team_two || "未选择"}</strong>
        </button>}
        <div className="rail-body">
          <div className="rail-header">
            <div>
              <h2>{mode === "history" ? "历史复盘" : "实时赛事"}</h2>
              <span>{matches.length} 场</span>
            </div>
            <Input
              aria-label="搜索赛事"
              className="match-search"
              contentBefore={<MagnifyingGlass size={16} aria-hidden="true" />}
              placeholder="队伍或赛事"
              size="small"
              value={query}
              onChange={(_, data) => setQuery(data.value)}
            />
          </div>

          <nav className="match-groups" aria-label="按状态分组的赛事">
        {lifecycleOrder.map((lifecycle) => {
          const group = filtered.filter((match) => match.lifecycle === lifecycle);
          if (!group.length) return null;
          return (
            <section className="match-group" key={lifecycle}>
              <div className="match-group-title">
                <span>{lifecycleLabel[lifecycle]}</span>
                <span>{group.length}</span>
              </div>
              <div className="match-group-items">
                {group.map((match) => {
                  const selected = match.raybet_match_id === selectedId;
                  const observedAt = match.winner?.observed_at;
                  const age = ageSeconds(observedAt, now);
                  const progress = matchProgressLabel(match);
                  const strategy = matchStrategySummary(match);
                  return (
                    <button
                      className={`match-row${selected ? " selected" : ""}`}
                      key={match.raybet_match_id}
                      onClick={() => {
                        onSelect(match.raybet_match_id);
                        setMobileExpanded(false);
                      }}
                      type="button"
                    >
                      <div className="match-row-meta">
                        <span className="tournament-name" title={match.tournament || "未知赛事"}>
                          {match.tournament || "未知赛事"}
                        </span>
                        <LifecycleBadge lifecycle={match.lifecycle} />
                      </div>
                      <div className="match-row-teams">
                        <div>
                          <span>{match.team_one || "队伍一"}</span>
                          <span className="versus">VS</span>
                          <span>{match.team_two || "队伍二"}</span>
                        </div>
                        <small>{progress}</small>
                      </div>
                      <div className="match-row-strategy">
                        <strong className={strategy.tone}>{strategy.label}</strong>
                        <span>{strategy.detail}</span>
                      </div>
                      <div className="match-row-prices">
                        <span>{formatOdds(match.winner?.prices?.team_one)}</span>
                        <span>{formatOdds(match.winner?.prices?.team_two)}</span>
                        <span className={age != null && age > 60 ? "age stale" : "age"}>
                          {formatAge(age)}
                        </span>
                      </div>
                      <CaretRight className="match-row-enter" size={17} aria-hidden="true" />
                    </button>
                  );
                })}
              </div>
            </section>
          );
        })}
        {!filtered.length && (
          <div className="rail-empty">
            <MagnifyingGlass size={24} aria-hidden="true" />
            <span>{query
              ? "没有匹配的赛事"
              : mode === "history" && historyLoading
                ? "正在加载历史比赛"
                : mode === "history" ? "暂无历史比赛" : "暂无实时赛事"}</span>
          </div>
        )}
        {mode === "history" && (historyHasMore || historyLoading) && (
          <button
            aria-label="加载更多历史比赛"
            className="history-load-more"
            disabled={historyLoading || !historyHasMore}
            onClick={onLoadMore}
            type="button"
          >
            {historyLoading ? "正在加载" : "加载更多"}
          </button>
        )}
          </nav>
        </div>
    </Root>
  );
}

function matchProgressLabel(match: MonitorMatch): string {
  const vision = match.latest_vision;
  if (vision?.confirmed === 1 && vision.map_number) {
    const clock = vision.game_clock_seconds == null
      ? "时钟待确认"
      : formatClock(vision.game_clock_seconds);
    return `第 ${vision.map_number} 局 · ${clock}`;
  }
  if (match.lifecycle === "upcoming") return "等待开赛";
  if (match.lifecycle === "ended") return "比赛已结束";
  return "等待可信比赛时钟";
}

function matchStrategySummary(match: MonitorMatch): {
  detail: string;
  label: string;
  tone: "positive" | "warning" | "neutral" | "critical";
} {
  const decision = match.latest_decision;
  if (!decision) {
    return match.lifecycle === "upcoming"
      ? { label: "等待开赛", detail: "尚未形成策略结论", tone: "neutral" }
      : { label: "等待判断", detail: "等待下一次可信输入", tone: "neutral" };
  }
  const direction = decision.underdog_side === "team_one"
    ? match.team_one
    : decision.underdog_side === "team_two" ? match.team_two : null;
  if (decision.eligible === 1) {
    return {
      label: "策略合格",
      detail: direction ? `关注 ${direction}` : "纸面候选已通过",
      tone: "positive",
    };
  }
  const invalid = decision.reason.includes("invalid") || decision.reason.includes("mismatch");
  return {
    label: invalid ? "证据需复核" : "策略拒绝",
    detail: direction ? `当前关注 ${direction}` : "等待下一次可信输入",
    tone: invalid ? "critical" : "warning",
  };
}
