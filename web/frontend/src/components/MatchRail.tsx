import { Input } from "@fluentui/react-components";
import { CaretRight, MagnifyingGlass } from "@phosphor-icons/react";
import { useMemo, useState } from "react";

import { formatOdds } from "../format";
import type { MonitorMatch } from "../types";
import { LifecycleBadge } from "./StatusBadge";


interface MatchRailProps {
  matches: MonitorMatch[];
  mode: "live" | "history";
  selectedId: string | null;
  onSelect: (matchId: string) => void;
}


export function MatchRail({ matches, mode, selectedId, onSelect }: MatchRailProps) {
  const [query, setQuery] = useState("");
  const visible = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase("zh-CN");
    const filtered = needle
      ? matches.filter((match) => [match.tournament, match.team_one, match.team_two]
        .some((value) => value?.toLocaleLowerCase("zh-CN").includes(needle)))
      : matches;
    return [...filtered].sort((left, right) => {
      const leftTime = Date.parse(left.updated_at || left.scheduled_at || "") || 0;
      const rightTime = Date.parse(right.updated_at || right.scheduled_at || "") || 0;
      return rightTime - leftTime;
    });
  }, [matches, query]);

  return (
    <aside className="match-rail" aria-label={mode === "history" ? "历史赛事" : "实时赛事"}>
      <header className="rail-header">
        <div><strong>{mode === "history" ? "历史结果" : "实时赛事"}</strong><span>{visible.length}</span></div>
        <Input
          contentBefore={<MagnifyingGlass size={15} />}
          onChange={(_, data) => setQuery(data.value)}
          placeholder="搜索赛事或队伍"
          value={query}
        />
      </header>
      <div className="rail-groups">
        {visible.map((match) => (
          <button
            className={match.raybet_match_id === selectedId ? "match-card selected" : "match-card"}
            key={match.raybet_match_id}
            onClick={() => onSelect(match.raybet_match_id)}
            type="button"
          >
            <span className="tournament-name">{match.tournament || "未知赛事"}</span>
            <LifecycleBadge lifecycle={match.lifecycle} />
            <div className="match-teams">
              <span>{match.team_one || "队伍一"}</span>
              <span>{match.team_two || "队伍二"}</span>
            </div>
            <div className="match-odds">
              <span>{formatOdds(match.winner?.prices?.team_one)}</span>
              <span>{formatOdds(match.winner?.prices?.team_two)}</span>
            </div>
            <CaretRight size={16} aria-hidden="true" />
          </button>
        ))}
        {!visible.length && <div className="subtle-empty">暂无赛事</div>}
      </div>
    </aside>
  );
}
