import { Input } from "@fluentui/react-components";
import { CaretRight, MagnifyingGlass } from "@phosphor-icons/react";
import { useMemo, useState } from "react";

import {
  formatClock,
  formatOdds,
  lifecycleLabel,
} from "../format";
import {
  getMatchAttentionState,
  getTrustedVision,
  matchesAttentionFilter,
  sortMatchesByAttention,
  type MatchAttentionFilter,
  type MatchAttentionSort,
  type MatchDecisionAttention,
} from "../matchPresentation";
import type { Lifecycle, MonitorMatch } from "../types";
import { RelativeAge } from "./RelativeAge";
import { LifecycleBadge } from "./StatusBadge";

const lifecycleOrder: Lifecycle[] = ["live", "degraded", "upcoming", "ended"];
const attentionFilters: Array<{ label: string; value: MatchAttentionFilter }> = [
  { value: "all", label: "全部" },
  { value: "action", label: "需处理" },
  { value: "eligible", label: "策略合格" },
  { value: "review", label: "证据复核" },
  { value: "degraded", label: "数据延迟" },
  { value: "upcoming", label: "待开赛" },
];
const attentionSortLabels: Record<MatchAttentionSort, string> = {
  priority: "关注顺序",
  updated: "最近更新",
  scheduled: "开赛时间",
};

interface MatchRailProps {
  matches: MonitorMatch[];
  mode: "live" | "history";
  selectedId: string | null;
  onSelect: (matchId: string) => void;
  now?: number;
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
  const [attentionFilter, setAttentionFilter] = useState<MatchAttentionFilter>("all");
  const [attentionSort, setAttentionSort] = useState<MatchAttentionSort>("priority");
  const searched = useMemo(() => {
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
  const attentionCounts = useMemo(() => new Map(
    attentionFilters.map(({ value }) => [
      value,
      searched.filter((match) => matchesAttentionFilter(match, value)).length,
    ]),
  ), [searched]);
  const filtered = useMemo(
    () => mode === "live"
      ? searched.filter((match) => matchesAttentionFilter(match, attentionFilter))
      : searched,
    [attentionFilter, mode, searched],
  );
  const visibleMatches = useMemo(
    () => mode === "live" ? sortMatchesByAttention(filtered, attentionSort) : filtered,
    [attentionSort, filtered, mode],
  );
  const groups = useMemo(() => {
    if (mode === "live") {
      return visibleMatches.length
        ? [{ key: attentionSort, label: attentionSortLabels[attentionSort], matches: visibleMatches }]
        : [];
    }
    return lifecycleOrder.flatMap((lifecycle) => {
      const group = visibleMatches.filter((match) => match.lifecycle === lifecycle);
      return group.length
        ? [{ key: lifecycle, label: lifecycleLabel[lifecycle], matches: group }]
        : [];
    });
  }, [attentionSort, mode, visibleMatches]);

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
            {mode === "live" && (
              <div className="attention-toolbar" aria-label="实时赛事筛选与排序">
                <div className="attention-filters" role="group" aria-label="关注状态筛选">
                  {attentionFilters.map(({ label, value }) => (
                    <button
                      aria-pressed={attentionFilter === value}
                      className={`attention-filter${attentionFilter === value ? " active" : ""}`}
                      key={value}
                      onClick={() => setAttentionFilter(value)}
                      type="button"
                    >
                      <span>{label}</span>
                      <strong>{attentionCounts.get(value) || 0}</strong>
                    </button>
                  ))}
                </div>
                <label className="attention-sort">
                  <span>排序</span>
                  <select
                    aria-label="赛事排序"
                    value={attentionSort}
                    onChange={(event) => setAttentionSort(event.target.value as MatchAttentionSort)}
                  >
                    <option value="priority">优先级</option>
                    <option value="updated">最近更新</option>
                    <option value="scheduled">开赛时间</option>
                  </select>
                </label>
              </div>
            )}
          </div>

          <nav
            className="match-groups"
            aria-label={mode === "live" ? "实时赛事关注队列" : "按状态分组的赛事"}
          >
        {groups.map((group) => {
          return (
            <section className="match-group" key={group.key}>
              <div className="match-group-title">
                <span>{group.label}</span>
                <span>{group.matches.length}</span>
              </div>
              <div className="match-group-items">
                {group.matches.map((match) => {
                  const selected = match.raybet_match_id === selectedId;
                  const observedAt = match.winner?.observed_at;
                  const progress = matchProgressLabel(match);
                  const strategy = getMatchAttentionState(match);
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
                        <div className="match-row-strategy-labels">
                          <strong className={attentionTone(strategy.decision)}>
                            {strategy.primaryLabel}
                          </strong>
                          {strategy.healthLabel && (
                            <small className={`match-health-label ${strategy.health}`}>
                              {strategy.healthLabel}
                            </small>
                          )}
                        </div>
                        <span>{strategy.primaryDetail}</span>
                      </div>
                      <div className="match-row-prices">
                        <span>{formatOdds(match.winner?.prices?.team_one)}</span>
                        <span>{formatOdds(match.winner?.prices?.team_two)}</span>
                        <RelativeAge
                          className="age"
                          now={now}
                          observedAt={observedAt}
                          staleAfterSeconds={60}
                        />
                      </div>
                      <CaretRight className="match-row-enter" size={17} aria-hidden="true" />
                    </button>
                  );
                })}
              </div>
            </section>
          );
        })}
        {!visibleMatches.length && (
          <div className="rail-empty">
            <MagnifyingGlass size={24} aria-hidden="true" />
            <span>{query
              ? "没有匹配的赛事"
              : mode === "live" && attentionFilter !== "all"
                ? `当前没有${attentionFilters.find((item) => item.value === attentionFilter)?.label || "符合条件的"}赛事`
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
  const vision = getTrustedVision(match);
  if (vision?.map_number) {
    const clock = vision.game_clock_seconds == null
      ? "时钟待确认"
      : formatClock(vision.game_clock_seconds);
    return `第 ${vision.map_number} 局 · ${clock}`;
  }
  if (match.lifecycle === "upcoming") return "等待开赛";
  if (match.lifecycle === "ended") return "比赛已结束";
  return "等待可信比赛时钟";
}

function attentionTone(
  decision: MatchDecisionAttention,
): "positive" | "warning" | "neutral" | "critical" {
  if (decision === "review") return "critical";
  if (decision === "eligible") return "positive";
  if (decision === "blocked") return "warning";
  return "neutral";
}
