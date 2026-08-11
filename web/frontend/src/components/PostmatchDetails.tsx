import {
  ChartLineUp,
  CheckCircle,
  ClockCounterClockwise,
  Database,
  WarningCircle,
} from "@phosphor-icons/react";
import { useEffect, useMemo, useState } from "react";

import { formatClock } from "../format";
import type {
  PostmatchDetail,
  PostmatchDraftAction,
  PostmatchGame,
  PostmatchPlayer,
} from "../types";


const HERO_IMAGE_BASE = "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/heroes";


export function PostmatchDetails({ postmatch }: { postmatch: PostmatchDetail }) {
  const [selectedMap, setSelectedMap] = useState(postmatch.games[0]?.map_number ?? null);

  useEffect(() => {
    if (!postmatch.games.some((game) => game.map_number === selectedMap)) {
      setSelectedMap(postmatch.games[0]?.map_number ?? null);
    }
  }, [postmatch.games, selectedMap]);

  const game = postmatch.games.find((item) => item.map_number === selectedMap)
    || postmatch.games[0]
    || null;
  const statusLabel = {
    available: "已同步",
    partial: "部分可用",
    waiting: "等待赛后数据",
    review: "需要复核",
  }[postmatch.status];

  return (
    <section className="workspace-section postmatch-section" aria-label="赛后比赛详情">
      <div className="section-heading compact postmatch-heading">
        <div>
          <h2>赛后比赛详情</h2>
          <p>官方 Match ID 严格绑定后展示；赛果和基础统计以 OpenDota 为准。</p>
        </div>
        <span className={`postmatch-status ${postmatch.status === "review" ? "review" : postmatch.games.length ? "available" : ""}`}>
          {statusLabel}
        </span>
      </div>

      <div className="postmatch-source-line" aria-label="赛后数据来源">
        <span>
          <Database size={15} aria-hidden="true" />
          OpenDota 主数据 · {sourceStatus(postmatch.sources.canonical.status)}
        </span>
        <span>
          STRATZ 增强 · {sourceStatus(postmatch.sources.enhancement.status)}
        </span>
      </div>

      {!game ? (
        <PostmatchNotice postmatch={postmatch} />
      ) : (
        <div className="postmatch-content">
          <div className="postmatch-map-tabs" role="tablist" aria-label="选择赛后局数">
            {postmatch.games.map((item) => (
              <button
                aria-selected={item.map_number === game.map_number}
                className={item.map_number === game.map_number ? "active" : ""}
                key={item.map_number}
                onClick={() => setSelectedMap(item.map_number)}
                role="tab"
                type="button"
              >
                第 {item.map_number} 局
              </button>
            ))}
          </div>
          <GameDetail game={game} />
        </div>
      )}
    </section>
  );
}


function GameDetail({ game }: { game: PostmatchGame }) {
  if (!game.result) {
    return (
      <div className="postmatch-notice">
        <ClockCounterClockwise size={20} aria-hidden="true" />
        <div>
          <strong>官方 Match ID {game.official_match_id} 已绑定</strong>
          <span>OpenDota 完整详情仍在同步，当前不展示不完整赛果。</span>
        </div>
      </div>
    );
  }
  const result = game.result;
  const radiant = result.radiant_team_name || "天辉";
  const dire = result.dire_team_name || "夜魇";
  const picks = game.draft.filter((action) => action.is_pick);
  const bans = game.draft.filter((action) => !action.is_pick);

  return (
    <>
      <div className="postmatch-result-bar">
        <div className={result.radiant_win ? "winner" : ""}>
          <span>天辉</span>
          <strong>{radiant}</strong>
          {result.radiant_win && <small><CheckCircle size={13} weight="fill" />胜方</small>}
        </div>
        <div className="postmatch-score">
          <span>OpenDota #{game.official_match_id}</span>
          <strong>{integer(result.radiant_score)} : {integer(result.dire_score)}</strong>
          <small>{formatClock(result.duration_seconds)}</small>
        </div>
        <div className={!result.radiant_win ? "winner" : ""}>
          <span>夜魇</span>
          <strong>{dire}</strong>
          {!result.radiant_win && <small><CheckCircle size={13} weight="fill" />胜方</small>}
        </div>
      </div>

      <div className="postmatch-source-line" aria-label="Official Match ID 关联证据">
        <span>
          <Database size={15} aria-hidden="true" />
          {game.identity_reason === "raybet_explicit_map_time_unique"
            ? `RayBet Map ${game.map_number} 显式时间 · Valve Series ${game.identity_evidence.official_series_id}`
            : "已确认赛果关联"}
        </span>
        {game.identity_evidence.delta_seconds != null && (
          <span>
            时间差 {formatClock(game.identity_evidence.delta_seconds)} / {formatClock(game.identity_evidence.maximum_delta_seconds || 0)}
          </span>
        )}
      </div>

      <Availability game={game} />

      <section className="postmatch-subsection" aria-labelledby={`postmatch-players-${game.map_number}`}>
        <Subheading
          description="十名选手的 OpenDota 最终统计"
          icon={<ChartLineUp size={18} aria-hidden="true" />}
          id={`postmatch-players-${game.map_number}`}
          title="选手记分板"
        />
        {game.players.length ? <PlayerTable players={game.players} /> : <Empty text="选手数据尚未入库" />}
      </section>

      <section className="postmatch-subsection" aria-labelledby={`postmatch-draft-${game.map_number}`}>
        <Subheading
          description="BP 顺序与阵营保持 OpenDota 原始语义"
          id={`postmatch-draft-${game.map_number}`}
          title="BP 阵容"
        />
        {game.draft.length ? <DraftBoard bans={bans} picks={picks} /> : <Empty text="BP 数据尚未入库" />}
      </section>

      <section className="postmatch-subsection" aria-labelledby={`postmatch-curve-${game.map_number}`}>
        <Subheading
          description="正值表示天辉领先，负值表示夜魇领先"
          icon={<ChartLineUp size={18} aria-hidden="true" />}
          id={`postmatch-curve-${game.map_number}`}
          title="经济与经验走势"
        />
        <div className="postmatch-curve-grid">
          <AdvantageChart label="金币差" points={game.advantages.gold} tone="gold" />
          <AdvantageChart label="经验差" points={game.advantages.xp} tone="xp" />
        </div>
      </section>

      <section className="postmatch-subsection" aria-labelledby={`postmatch-events-${game.map_number}`}>
        <Subheading
          description="目标事件和团战记录均来自同一 OpenDota Match ID"
          icon={<ClockCounterClockwise size={18} aria-hidden="true" />}
          id={`postmatch-events-${game.map_number}`}
          title="目标与团战"
        />
        <EventSummary game={game} />
      </section>
    </>
  );
}


function PostmatchNotice({ postmatch }: { postmatch: PostmatchDetail }) {
  const review = postmatch.status === "review";
  return (
    <div className={review ? "postmatch-notice review" : "postmatch-notice"}>
      {review
        ? <WarningCircle size={20} aria-hidden="true" />
        : <ClockCounterClockwise size={20} aria-hidden="true" />}
      <div>
        <strong>{review ? "赛后身份需要人工复核" : "等待官方比赛身份与赛后数据"}</strong>
        <span>{review
          ? "存在来源冲突，页面不会猜测或拼接比赛详情。"
          : "完成 exact map 绑定后会自动同步并显示每局详情。"}</span>
        <code>{postmatch.reason}</code>
      </div>
    </div>
  );
}


function Availability({ game }: { game: PostmatchGame }) {
  const labels: Array<[keyof PostmatchGame["availability"], string]> = [
    ["result", "赛果"],
    ["players", "选手"],
    ["draft", "BP"],
    ["gold_advantage", "经济"],
    ["xp_advantage", "经验"],
    ["objectives", "目标"],
    ["teamfights", "团战"],
  ];
  return (
    <div className="postmatch-availability" aria-label="OpenDota 字段可用性">
      {labels.map(([key, label]) => (
        <span className={game.availability[key] === "available" ? "ready" : "missing"} key={key}>
          {label} {availabilityLabel(game.availability[key])}
        </span>
      ))}
    </div>
  );
}


function PlayerTable({ players }: { players: PostmatchPlayer[] }) {
  return (
    <div className="postmatch-table-scroll">
      <table className="postmatch-table postmatch-player-table">
        <thead><tr>
          <th>阵营</th><th>英雄 / 账号</th><th>位置</th><th>K / D / A</th><th>GPM / XPM</th>
          <th>净值</th><th>正补 / 反补</th><th>英雄伤害</th><th>建筑伤害</th>
        </tr></thead>
        <tbody>
          {players.map((player) => (
            <tr key={`${player.player_slot}-${player.account_id ?? "unknown"}`}>
              <td>{player.side === "radiant" ? "天辉" : "夜魇"}</td>
              <td>
                <div className="postmatch-player-identity">
                  <HeroPortrait heroKey={player.hero_key} name={player.hero_name} />
                  <div><strong>{player.hero_name}</strong><small>{player.account_id ? `账号 ${player.account_id}` : "匿名选手"}</small></div>
                </div>
              </td>
              <td className="postmatch-number">
                {player.position ? `${player.position} 号位` : "-"}
                {player.position_source && <small>{player.position_source === "stratz" ? "STRATZ" : "OpenDota"}</small>}
              </td>
              <td className="postmatch-number score">{integer(player.kills)} / {integer(player.deaths)} / {integer(player.assists)}</td>
              <td className="postmatch-number">{integer(player.gold_per_min)} / {integer(player.xp_per_min)}</td>
              <td className="postmatch-number">{integer(player.net_worth)}</td>
              <td className="postmatch-number">{integer(player.last_hits)} / {integer(player.denies)}</td>
              <td className="postmatch-number">{integer(player.hero_damage)}</td>
              <td className="postmatch-number">{integer(player.tower_damage)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}


function DraftBoard({ bans, picks }: { bans: PostmatchDraftAction[]; picks: PostmatchDraftAction[] }) {
  return (
    <div className="postmatch-draft-grid">
      <DraftSide actions={picks.filter((item) => item.side === "radiant")} label="天辉选择" />
      <DraftSide actions={picks.filter((item) => item.side === "dire")} label="夜魇选择" />
      <DraftSide actions={bans.filter((item) => item.side === "radiant")} label="天辉禁用" muted />
      <DraftSide actions={bans.filter((item) => item.side === "dire")} label="夜魇禁用" muted />
    </div>
  );
}


function DraftSide({ actions, label, muted = false }: {
  actions: PostmatchDraftAction[];
  label: string;
  muted?: boolean;
}) {
  return (
    <div className={muted ? "postmatch-draft-side muted" : "postmatch-draft-side"}>
      <strong>{label}</strong>
      <div>
        {actions.map((action) => (
          <span key={`${action.order}-${action.hero_id}`} title={`${action.order + 1}. ${action.hero_name}`}>
            <HeroPortrait heroKey={action.hero_key} name={action.hero_name} />
            <small>{action.order + 1}</small>
          </span>
        ))}
      </div>
    </div>
  );
}


function AdvantageChart({
  label,
  points,
  tone,
}: {
  label: string;
  points: Array<{ minute: number; value: number }>;
  tone: "gold" | "xp";
}) {
  const path = useMemo(() => chartPoints(points), [points]);
  const latest = points.at(-1)?.value ?? null;
  return (
    <article className={`postmatch-advantage-chart ${tone}`}>
      <header><strong>{label}</strong><span>{signed(latest)}</span></header>
      {points.length > 1 ? (
        <svg aria-label={`${label}走势`} preserveAspectRatio="none" role="img" viewBox="0 0 560 120">
          <line x1="8" x2="552" y1="60" y2="60" />
          <polyline points={path} />
        </svg>
      ) : <Empty text={`${label}数据不足`} />}
    </article>
  );
}


function EventSummary({ game }: { game: PostmatchGame }) {
  if (!game.objectives.length && !game.teamfights.length) return <Empty text="目标与团战数据尚未入库" />;
  return (
    <div className="postmatch-event-columns">
      <div>
        <strong>目标事件 · {game.objectives.length}</strong>
        <ol>
          {game.objectives.slice(0, 12).map((event, index) => (
            <li key={`${event.time_seconds}-${event.type}-${index}`}>
              <time>{event.time_seconds == null ? "-" : formatClock(event.time_seconds)}</time>
              <span>{objectiveLabel(event.type, event.key)}</span>
            </li>
          ))}
        </ol>
      </div>
      <div>
        <strong>团战 · {game.teamfights.length}</strong>
        <ol>
          {game.teamfights.slice(0, 12).map((fight, index) => (
            <li key={`${fight.start_time}-${index}`}>
              <time>{fight.start_time == null ? "-" : formatClock(fight.start_time)}</time>
              <span>{integer(fight.deaths)} 次阵亡 · 伤害 {integer(fight.damage)}</span>
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}


function Subheading({ description, icon, id, title }: {
  description: string;
  icon?: React.ReactNode;
  id: string;
  title: string;
}) {
  return (
    <div className="postmatch-subheading">
      <div><h3 id={id}>{title}</h3><p>{description}</p></div>
      {icon}
    </div>
  );
}


function HeroPortrait({ heroKey, name }: { heroKey: string; name: string }) {
  const [failed, setFailed] = useState(false);
  if (!heroKey || failed) return <span className="hero-image-fallback">{name.slice(0, 1)}</span>;
  return <img alt="" onError={() => setFailed(true)} src={`${HERO_IMAGE_BASE}/${heroKey}.png`} />;
}


function Empty({ text }: { text: string }) {
  return <div className="postmatch-empty">{text}</div>;
}


function chartPoints(points: Array<{ minute: number; value: number }>): string {
  if (!points.length) return "";
  const maxMinute = Math.max(...points.map((point) => point.minute), 1);
  const maxAbs = Math.max(...points.map((point) => Math.abs(point.value)), 1);
  return points.map((point) => {
    const x = 8 + (point.minute / maxMinute) * 544;
    const y = 60 - (point.value / maxAbs) * 52;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(" ");
}


function sourceStatus(value: string): string {
  return {
    available: "可用",
    linked_not_ingested: "已绑定，待同步",
    waiting_for_exact_link: "等待精确绑定",
    not_available: "未提供",
    blocked: "暂不可用",
    partial: "部分可用",
    invalid: "数据无效",
  }[value] || value;
}


function availabilityLabel(value: string): string {
  return value === "available" ? "可用" : value === "partial" ? "部分" : "缺失";
}


function integer(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "-" : Math.round(value).toLocaleString("zh-CN");
}


function signed(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return "-";
  return `${value > 0 ? "+" : ""}${Math.round(value).toLocaleString("zh-CN")}`;
}


function objectiveLabel(type: string, key: string): string {
  return {
    CHAT_MESSAGE_ROSHAN_KILL: "肉山击杀",
    CHAT_MESSAGE_TOWER_KILL: "防御塔摧毁",
    CHAT_MESSAGE_BARRACKS_KILL: "兵营摧毁",
  }[type] || key || type || "目标事件";
}
