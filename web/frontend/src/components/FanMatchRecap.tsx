import {
  CaretDown,
  CheckCircle,
  Clock,
  Crown,
  Sword,
  TrendUp,
  Trophy,
} from "@phosphor-icons/react";
import { useEffect, useMemo, useState } from "react";

import { formatClock, formatDateTime, formatOdds, formatPercent } from "../format";
import type {
  MatchDetail,
  MonitorMatch,
  PostmatchDraftAction,
  PostmatchGame,
  PostmatchPlayer,
} from "../types";


const HERO_IMAGE_BASE = "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/heroes";


interface FanMatchRecapProps {
  detail: MatchDetail | null;
  error: string | null;
  loading: boolean;
  match: MonitorMatch | null;
}


type CompletedGame = PostmatchGame & { result: NonNullable<PostmatchGame["result"]> };


export function FanMatchRecap({ detail, error, loading, match }: FanMatchRecapProps) {
  const games = useMemo(
    () => (detail?.postmatch.games || []).filter(isCompletedGame),
    [detail?.postmatch.games],
  );
  const [selectedMap, setSelectedMap] = useState<number | null>(games[0]?.map_number ?? null);

  useEffect(() => {
    if (!games.some((game) => game.map_number === selectedMap)) {
      setSelectedMap(games[0]?.map_number ?? null);
    }
  }, [games, selectedMap]);

  if (!match) return <RecapState title="没有可显示的比赛" detail="请先从比赛复盘列表选择一场赛事。" />;
  if (loading && !detail) return <RecapLoading />;
  if (error && !detail) return <RecapState title="比赛详情加载失败" detail={error} />;
  if (!detail || !games.length) {
    return (
      <RecapState
        title="赛后数据整理中"
        detail={`${match.team_one || "队伍一"} vs ${match.team_two || "队伍二"} 的官方赛后数据尚未完整到达。`}
      />
    );
  }

  const series = seriesSummary(games);
  const game = games.find((item) => item.map_number === selectedMap) || games[0];

  return (
    <main className="fan-recap" aria-label="比赛复盘">
      <SeriesResult games={games} match={match} series={series} />
      <MapSelector games={games} onSelect={setSelectedMap} selectedMap={game.map_number} />
      <MapRecap game={game} />
      <RecapFootnotes detail={detail} match={match} />
    </main>
  );
}


function SeriesResult({
  games,
  match,
  series,
}: {
  games: CompletedGame[];
  match: MonitorMatch;
  series: ReturnType<typeof seriesSummary>;
}) {
  const winner = series.left.score === series.right.score
    ? null
    : series.left.score > series.right.score ? series.left : series.right;
  return (
    <section className="fan-series-result" aria-label="系列赛结果">
      <div className="fan-series-meta">
        <span>{match.tournament || "未知赛事"}</span>
        <span>{match.best_of ? `BO${match.best_of}` : `${games.length} 局`}</span>
        <time>{formatDateTime(match.scheduled_at)}</time>
      </div>
      <div className="fan-series-title">
        <Trophy size={20} weight="fill" aria-hidden="true" />
        <strong>{winner ? `${winner.name} 赢下系列赛` : "系列赛结束"}</strong>
      </div>
      <div
        className="fan-series-scoreboard"
        aria-label={`系列赛比分 ${series.left.name} ${series.left.score} 比 ${series.right.score} ${series.right.name}`}
      >
        <div className={winner?.key === series.left.key ? "winner" : ""}>
          <strong>{series.left.name}</strong>
          {winner?.key === series.left.key && <span><Crown size={14} weight="fill" />胜方</span>}
        </div>
        <div className="fan-series-score">
          <strong>{series.left.score}</strong>
          <span>:</span>
          <strong>{series.right.score}</strong>
        </div>
        <div className={winner?.key === series.right.key ? "winner" : ""}>
          <strong>{series.right.name}</strong>
          {winner?.key === series.right.key && <span><Crown size={14} weight="fill" />胜方</span>}
        </div>
      </div>
    </section>
  );
}


function MapSelector({
  games,
  onSelect,
  selectedMap,
}: {
  games: CompletedGame[];
  onSelect: (mapNumber: number) => void;
  selectedMap: number;
}) {
  return (
    <nav className="fan-map-selector" aria-label="选择比赛局数" role="tablist">
      {games.map((game) => {
        const result = game.result;
        const winner = result.radiant_win
          ? friendlyTeamName(result.radiant_team_name)
          : friendlyTeamName(result.dire_team_name);
        return (
          <button
            aria-selected={game.map_number === selectedMap}
            className={game.map_number === selectedMap ? "active" : ""}
            key={game.map_number}
            onClick={() => onSelect(game.map_number)}
            role="tab"
            type="button"
          >
            <span>第 {game.map_number} 局</span>
            <strong>{integer(result.radiant_score)} : {integer(result.dire_score)}</strong>
            <small>{winner} 胜</small>
          </button>
        );
      })}
    </nav>
  );
}


function MapRecap({ game }: { game: CompletedGame }) {
  const result = game.result;
  const radiant = friendlyTeamName(result.radiant_team_name) || "天辉";
  const dire = friendlyTeamName(result.dire_team_name) || "夜魇";
  const winner = result.radiant_win ? radiant : dire;
  const winnerScore = result.radiant_win ? result.radiant_score : result.dire_score;
  const loserScore = result.radiant_win ? result.dire_score : result.radiant_score;
  const picks = game.draft.filter((action) => action.is_pick);
  const lead = largestLead(game, radiant, dire);
  const roshanKills = game.objectives.filter(isRoshanKill).length;

  return (
    <>
      <section className="fan-map-overview" aria-label={`第 ${game.map_number} 局结果`}>
        <div className="fan-map-story">
          <span>第 {game.map_number} 局</span>
          <h1>{winner} {integer(winnerScore)} : {integer(loserScore)} 取胜</h1>
          <p>{winner} 用时 {formatClock(result.duration_seconds)} 赢下本局。</p>
          <div className="fan-map-metrics" aria-label="本局关键数据">
            <Metric icon={<Sword size={17} />} label="击杀" value={`${integer(result.radiant_score)} : ${integer(result.dire_score)}`} />
            <Metric icon={<Clock size={17} />} label="时长" value={formatClock(result.duration_seconds)} />
            <Metric icon={<TrendUp size={17} />} label="最大经济领先" value={lead} />
            <Metric icon={<Trophy size={17} />} label="肉山" value={`${roshanKills} 次`} />
          </div>
        </div>
        <div className="fan-lineups" aria-label="本局十英雄阵容">
          <HeroLineup
            actions={picks.filter((action) => action.side === "radiant")}
            label={radiant}
            side="radiant"
          />
          <HeroLineup
            actions={picks.filter((action) => action.side === "dire")}
            label={dire}
            side="dire"
          />
        </div>
      </section>

      <section className="fan-section" aria-labelledby={`fan-players-${game.map_number}`}>
        <SectionHeading id={`fan-players-${game.map_number}`} title="选手表现" subtitle="按阵营查看本局最终数据" />
        <div className="fan-team-rosters">
          <TeamRoster name={radiant} players={game.players.filter((player) => player.side === "radiant")} />
          <TeamRoster name={dire} players={game.players.filter((player) => player.side === "dire")} />
        </div>
      </section>

      <section className="fan-section" aria-labelledby={`fan-trends-${game.map_number}`}>
        <SectionHeading id={`fan-trends-${game.map_number}`} title="比赛走势" subtitle="曲线向上代表天辉领先，向下代表夜魇领先" />
        <div className="fan-trend-grid">
          <RecapTrend label="经济差" points={game.advantages.gold} radiant={radiant} dire={dire} />
          <RecapTrend label="经验差" points={game.advantages.xp} radiant={radiant} dire={dire} />
        </div>
      </section>

      <section className="fan-section" aria-labelledby={`fan-events-${game.map_number}`}>
        <SectionHeading id={`fan-events-${game.map_number}`} title="关键节点" subtitle="只展示对比赛进程有明确意义的事件" />
        <FriendlyEvents game={game} />
      </section>

      <DataCompleteness game={game} />
    </>
  );
}


function Metric({ icon, label, value }: { icon: React.ReactNode; label: string; value: string }) {
  return <div><span>{icon}{label}</span><strong>{value}</strong></div>;
}


function HeroLineup({
  actions,
  label,
  side,
}: {
  actions: PostmatchDraftAction[];
  label: string;
  side: "radiant" | "dire";
}) {
  return (
    <div className={`fan-lineup ${side}`}>
      <strong>{label}</strong>
      <div>
        {actions.map((action) => (
          <span key={`${action.order}-${action.hero_id}`} title={action.hero_name}>
            <HeroPortrait heroKey={action.hero_key} name={action.hero_name} />
            <small>{action.hero_name}</small>
          </span>
        ))}
      </div>
    </div>
  );
}


function TeamRoster({ name, players }: { name: string; players: PostmatchPlayer[] }) {
  return (
    <section className="fan-team-roster" aria-label={`${name} 选手数据`}>
      <header><strong>{name}</strong><span>{players.length} 名选手</span></header>
      <div className="fan-player-columns" aria-hidden="true">
        <span>选手 / 英雄</span><span>K / D / A</span><span>GPM / XPM</span><span>英雄伤害</span><span>净值</span>
      </div>
      {players.map((player) => (
        <div className="fan-player-row" key={`${player.player_slot}-${player.hero_id}`}>
          <div className="fan-player-identity">
            <HeroPortrait heroKey={player.hero_key} name={player.hero_name} />
            <span>
              <strong>{player.player_name || "未公开姓名"}</strong>
              <small>{player.hero_name}{player.position ? ` · ${player.position} 号位` : ""}</small>
            </span>
          </div>
          <PlayerStat
            current={`${integer(player.kills)} / ${integer(player.deaths)} / ${integer(player.assists)}`}
            history={historyKda(player)}
          />
          <PlayerStat
            current={`${integer(player.gold_per_min)} / ${integer(player.xp_per_min)}`}
            history={historyPair(player, "gold_per_min", "xp_per_min")}
          />
          <PlayerStat
            current={compactNumber(player.hero_damage)}
            history={historyNumber(player, "hero_damage", true)}
          />
          <PlayerStat
            current={compactNumber(player.net_worth)}
            history={historyNumber(player, "net_worth", true)}
          />
          <small className="fan-player-history-meta">{historySampleLabel(player)}</small>
        </div>
      ))}
    </section>
  );
}


function PlayerStat({ current, history }: { current: string; history: string }) {
  return <span className="fan-player-stat"><strong>{current}</strong><small>{history}</small></span>;
}


function RecapTrend({
  dire,
  label,
  points,
  radiant,
}: {
  dire: string;
  label: string;
  points: Array<{ minute: number; value: number }>;
  radiant: string;
}) {
  const path = useMemo(() => chartPoints(points), [points]);
  const latest = points.at(-1)?.value ?? null;
  const leader = latest == null ? "数据不足" : `${latest >= 0 ? radiant : dire} +${integer(Math.abs(latest))}`;
  return (
    <article className="fan-trend">
      <header><strong>{label}</strong><span>{leader}</span></header>
      {points.length > 1 ? (
        <svg aria-label={`${label}走势`} preserveAspectRatio="none" role="img" viewBox="0 0 560 150">
          <line x1="8" x2="552" y1="75" y2="75" />
          <polyline points={path} />
        </svg>
      ) : <p>暂无完整走势</p>}
      <footer><span>{radiant}</span><span>{dire}</span></footer>
    </article>
  );
}


function FriendlyEvents({ game }: { game: CompletedGame }) {
  const objectiveEvents = game.objectives.flatMap((event, index) => {
    if (event.time_seconds == null || event.time_seconds < 0) return [];
    const label = friendlyObjective(event.type, event.key, event.unit);
    return label ? [{ key: `objective-${index}`, time: event.time_seconds, label }] : [];
  });
  const fights = game.teamfights.flatMap((fight, index) => (
    fight.start_time == null || fight.start_time < 0
      ? []
      : [{
        key: `fight-${index}`,
        time: fight.start_time,
        label: `团战造成 ${integer(fight.damage)} 英雄伤害，${integer(fight.deaths)} 人阵亡`,
      }]
  ));
  const events = [...objectiveEvents, ...fights]
    .sort((left, right) => left.time - right.time)
    .slice(0, 12);
  if (!events.length) return <p className="fan-empty">本局没有可展示的关键节点。</p>;
  return (
    <ol className="fan-event-list">
      {events.map((event) => (
        <li key={event.key}><time>{formatClock(event.time)}</time><span>{event.label}</span></li>
      ))}
    </ol>
  );
}


const COMPLETENESS_LABELS: Array<[keyof PostmatchGame["availability"], string]> = [
  ["result", "赛果"],
  ["players", "十名选手统计"],
  ["player_names", "选手姓名"],
  ["historical_averages", "赛前历史均值"],
  ["positions", "选手位置"],
  ["draft", "BP 阵容"],
  ["gold_advantage", "经济走势"],
  ["xp_advantage", "经验走势"],
  ["objectives", "地图目标"],
  ["teamfights", "团战记录"],
];


function DataCompleteness({ game }: { game: CompletedGame }) {
  const available = COMPLETENESS_LABELS.filter(([key]) => game.availability[key] === "available").length;
  return (
    <section className="fan-section fan-completeness" aria-labelledby={`fan-completeness-${game.map_number}`}>
      <SectionHeading
        id={`fan-completeness-${game.map_number}`}
        title="数据完整性"
        subtitle={`${available} / ${COMPLETENESS_LABELS.length} 项完整，缺失项不会被推测补齐`}
      />
      <div className="fan-completeness-grid">
        {COMPLETENESS_LABELS.map(([key, label]) => {
          const status = game.availability[key];
          return (
            <div className={status} key={key}>
              <span>{label}</span>
              <strong>{availabilityLabel(status)}</strong>
            </div>
          );
        })}
      </div>
      <p className="fan-source-line">
        官方 Match ID {game.official_match_id} · 赛果与统计 OpenDota
        {stratzPositionStatus(game.enrichment)}
      </p>
    </section>
  );
}


function stratzPositionStatus(enrichment: PostmatchGame["enrichment"]): string {
  if (enrichment.status === "available") return " · 位置补充 STRATZ";
  if (enrichment.status !== "blocked") return " · STRATZ 位置补充未到达";
  if (["stratz_http_401", "stratz_http_403"].includes(enrichment.reason)) {
    return " · STRATZ 位置补充暂不可用（认证被拒绝）";
  }
  if (enrichment.reason === "stratz_http_429") {
    return " · STRATZ 位置补充暂不可用（请求受限）";
  }
  return " · STRATZ 位置补充暂不可用";
}


function RecapFootnotes({ detail, match }: { detail: MatchDetail; match: MonitorMatch }) {
  const quote = detail.winner || match.winner;
  return (
    <section className="fan-footnotes" aria-label="补充信息">
      {quote?.prices && (
        <details>
          <summary><TrendUp size={17} />收盘赔率<CaretDown size={15} /></summary>
          <div className="fan-closing-odds">
            <span>{match.team_one}<strong>{formatOdds(quote.prices.team_one)}</strong><small>{formatPercent(quote.probabilities?.team_one)}</small></span>
            <span>{match.team_two}<strong>{formatOdds(quote.prices.team_two)}</strong><small>{formatPercent(quote.probabilities?.team_two)}</small></span>
          </div>
        </details>
      )}
      <details>
        <summary><CheckCircle size={17} />数据说明<CaretDown size={15} /></summary>
        <p>Map 身份与赛果以精确关联的 Valve/Dota 比赛记录为准；OpenDota 提供赛后阵容与统计，STRATZ 只补充位置。选手均值只统计各局开赛前已经采集到的 OpenDota 样本，并显示样本局数和日期范围。</p>
      </details>
    </section>
  );
}


function SectionHeading({ id, subtitle, title }: { id: string; subtitle: string; title: string }) {
  return <header className="fan-section-heading"><h2 id={id}>{title}</h2><p>{subtitle}</p></header>;
}


function HeroPortrait({ heroKey, name }: { heroKey: string; name: string }) {
  const [failed, setFailed] = useState(false);
  if (!heroKey || failed) return <span className="fan-hero-fallback">{name.slice(0, 1)}</span>;
  return <img alt="" onError={() => setFailed(true)} src={`${HERO_IMAGE_BASE}/${heroKey}.png`} />;
}


function RecapLoading() {
  return (
    <main className="fan-recap fan-recap-loading" aria-label="正在加载比赛复盘">
      <div /><div /><div />
    </main>
  );
}


function RecapState({ detail, title }: { detail: string; title: string }) {
  return (
    <main className="fan-recap fan-recap-state" aria-label="比赛复盘状态">
      <Trophy size={30} aria-hidden="true" />
      <h1>{title}</h1>
      <p>{detail}</p>
    </main>
  );
}


function seriesSummary(games: CompletedGame[]) {
  const first = games[0].result;
  const left = {
    key: teamKey(first.radiant_team_id, first.radiant_team_name),
    name: friendlyTeamName(first.radiant_team_name) || "天辉",
    score: 0,
  };
  const right = {
    key: teamKey(first.dire_team_id, first.dire_team_name),
    name: friendlyTeamName(first.dire_team_name) || "夜魇",
    score: 0,
  };
  for (const game of games) {
    const result = game.result;
    const winnerKey = result.radiant_win
      ? teamKey(result.radiant_team_id, result.radiant_team_name)
      : teamKey(result.dire_team_id, result.dire_team_name);
    if (winnerKey === left.key) left.score += 1;
    if (winnerKey === right.key) right.score += 1;
  }
  return { left, right };
}


function isCompletedGame(game: PostmatchGame): game is CompletedGame {
  return game.status === "available" && game.result != null;
}


function teamKey(id: number | null, name: string | null): string {
  return id ? `id:${id}` : `name:${normalizeName(name)}`;
}


function friendlyTeamName(name: string | null): string {
  return String(name || "").replace(/^_+/, "").trim();
}


function normalizeName(name: string | null): string {
  return friendlyTeamName(name).toLocaleLowerCase("en-US").replace(/[^a-z0-9]+/g, "");
}


function largestLead(game: CompletedGame, radiant: string, dire: string): string {
  const point = game.advantages.gold.reduce<{ value: number } | null>((largest, current) => (
    largest == null || Math.abs(current.value) > Math.abs(largest.value) ? current : largest
  ), null);
  if (!point) return "数据不足";
  return `${point.value >= 0 ? radiant : dire} +${integer(Math.abs(point.value))}`;
}


function isRoshanKill(event: PostmatchGame["objectives"][number]): boolean {
  return `${event.type} ${event.key} ${event.unit}`.toLocaleLowerCase("en-US").includes("roshan");
}


function friendlyObjective(type: string, key: string, unit: string): string | null {
  const value = `${type} ${key} ${unit}`.toLocaleLowerCase("en-US");
  if (value.includes("roshan_kill") || value.includes("roshan")) return "击杀肉山";
  if (value.includes("aegis")) return "拾取不朽之守护";
  if (value.includes("miniboss")) return "击杀大型中立目标";
  if (value.includes("courier")) return "信使被击杀";
  if (value.includes("barracks")) return "兵营被摧毁";
  if (value.includes("tower")) {
    const lane = value.includes("top") ? "上路" : value.includes("mid") ? "中路" : value.includes("bot") ? "下路" : "";
    const tier = value.includes("tower1") ? "一塔" : value.includes("tower2") ? "二塔" : value.includes("tower3") ? "高地塔" : "防御塔";
    return `${lane}${tier}被摧毁`;
  }
  return null;
}


function chartPoints(points: Array<{ minute: number; value: number }>): string {
  if (!points.length) return "";
  const maxMinute = Math.max(...points.map((point) => point.minute), 1);
  const maxAbs = Math.max(...points.map((point) => Math.abs(point.value)), 1);
  return points.map((point) => {
    const x = 8 + (point.minute / maxMinute) * 544;
    const y = 75 - (point.value / maxAbs) * 64;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(" ");
}


function integer(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "-" : Math.round(value).toLocaleString("zh-CN");
}


function compactNumber(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return "-";
  if (Math.abs(value) < 10_000) return integer(value);
  return `${(value / 1000).toFixed(1)}k`;
}


function historyKda(player: PostmatchPlayer): string {
  const history = player.historical_average;
  if (!history) return "暂无历史样本";
  return `前 ${history.sample_size} 局 ${decimal(history.kills)}/${decimal(history.deaths)}/${decimal(history.assists)}`;
}


function historySampleLabel(player: PostmatchPlayer): string {
  const history = player.historical_average;
  if (!history) return "OpenDota · 当前比赛前无已收集样本";
  const dateRange = history.sample_start_date === history.sample_end_date
    ? history.sample_start_date
    : `${history.sample_start_date} 至 ${history.sample_end_date}`;
  return `OpenDota · ${dateRange} · ${history.sample_size} 局`;
}


function historyPair(
  player: PostmatchPlayer,
  first: "gold_per_min" | "xp_per_min",
  second: "gold_per_min" | "xp_per_min",
): string {
  const history = player.historical_average;
  if (!history) return "暂无历史样本";
  return `均 ${integer(history[first])} / ${integer(history[second])}`;
}


function historyNumber(
  player: PostmatchPlayer,
  field: "hero_damage" | "net_worth",
  compact = false,
): string {
  const history = player.historical_average;
  if (!history) return "暂无历史样本";
  const value = compact ? compactNumber(history[field]) : integer(history[field]);
  return `均 ${value}`;
}


function decimal(value: number | null): string {
  return value == null || !Number.isFinite(value) ? "-" : value.toFixed(1);
}


function availabilityLabel(status: PostmatchGame["availability"][keyof PostmatchGame["availability"]]): string {
  if (status === "available") return "完整";
  if (status === "partial") return "部分到达";
  return "未到达";
}
