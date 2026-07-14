import { Input } from "@fluentui/react-components";
import { MagnifyingGlass } from "@phosphor-icons/react";
import { useMemo, useState } from "react";

import { ageSeconds, formatAge, formatOdds, lifecycleLabel } from "../format";
import type { Lifecycle, MonitorMatch } from "../types";
import { LifecycleBadge } from "./StatusBadge";

const lifecycleOrder: Lifecycle[] = ["live", "degraded", "upcoming", "ended"];

interface MatchRailProps {
  matches: MonitorMatch[];
  selectedId: string | null;
  onSelect: (matchId: string) => void;
  now: number;
}

export function MatchRail({ matches, selectedId, onSelect, now }: MatchRailProps) {
  const [query, setQuery] = useState("");
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

  return (
    <aside className="match-rail" aria-label="赛事列表">
      <div className="rail-header">
        <div>
          <h2>赛事</h2>
          <span>{matches.length} 场已发现</span>
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
                  return (
                    <button
                      className={`match-row${selected ? " selected" : ""}`}
                      key={match.raybet_match_id}
                      onClick={() => onSelect(match.raybet_match_id)}
                      type="button"
                    >
                      <div className="match-row-meta">
                        <span className="tournament-name" title={match.tournament || "未知赛事"}>
                          {match.tournament || "未知赛事"}
                        </span>
                        <LifecycleBadge lifecycle={match.lifecycle} />
                      </div>
                      <div className="match-row-teams">
                        <span>{match.team_one || "队伍一"}</span>
                        <span className="versus">VS</span>
                        <span>{match.team_two || "队伍二"}</span>
                      </div>
                      <div className="match-row-prices">
                        <span>{formatOdds(match.winner?.prices?.team_one)}</span>
                        <span>{formatOdds(match.winner?.prices?.team_two)}</span>
                        <span className={age != null && age > 60 ? "age stale" : "age"}>
                          {formatAge(age)}
                        </span>
                      </div>
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
            <span>没有匹配的赛事</span>
          </div>
        )}
      </nav>
    </aside>
  );
}
