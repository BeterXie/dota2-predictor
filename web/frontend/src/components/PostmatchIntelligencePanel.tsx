import { Button, Skeleton, SkeletonItem } from "@fluentui/react-components";
import {
  ArrowClockwise,
  ChartBar,
  CheckCircle,
  ClockCounterClockwise,
  WarningCircle,
} from "@phosphor-icons/react";
import { useEffect, useMemo, useState } from "react";

import { fetchExactPostmatchAttribution } from "../api";
import { formatClock, formatPercent } from "../format";
import type {
  ExactPostmatchAttribution,
  ExactPostmatchEvent,
  ExactPostmatchPayload,
  IntelligencePlayerMapScore,
  IntelligencePlayerPerformance,
  IntelligenceStateLabel,
  IntelligenceTeamState,
} from "../types";

interface PostmatchIntelligencePanelProps {
  mapNumber: number | null;
  raybetMatchId: string;
  teamOne: string;
  teamTwo: string;
}

const STATE_LABELS: Record<IntelligenceStateLabel, string> = {
  comeback: "翻盘局",
  throw: "被翻盘局",
  stomp: "碾压局",
  stomp_loss: "被碾压局",
  advantage: "优势局",
  disadvantage: "劣势局",
  even: "均势局",
  state_unscorable: "局势数据不足",
};

const EVENT_LABELS: Record<ExactPostmatchEvent["event_type"], string> = {
  economy: "经济",
  objective: "目标",
  teamfight: "团战",
  buyback: "买活",
};

const AVAILABILITY_LABELS: Array<[
  Exclude<keyof ExactPostmatchPayload["event_availability"], "missing_reasons">,
  string,
]> = [
  ["gold_advantage", "金币曲线"],
  ["xp_advantage", "经验曲线"],
  ["objectives", "目标事件"],
  ["teamfights", "团战事件"],
  ["buybacks", "买活事件"],
  ["odds_game_clock_alignment", "赔率时钟"],
];

export function PostmatchIntelligencePanel({
  mapNumber,
  raybetMatchId,
}: PostmatchIntelligencePanelProps) {
  const [result, setResult] = useState<ExactPostmatchAttribution | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    if (mapNumber == null) {
      setResult(null);
      setError(null);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setResult(null);
    setError(null);
    setLoading(true);
    fetchExactPostmatchAttribution(raybetMatchId, mapNumber, controller.signal)
      .then((value) => {
        if (value.raybet_match_id !== raybetMatchId || value.map_number !== mapNumber) {
          throw new Error("赛后归因响应与当前比赛局号不一致");
        }
        setResult(value);
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        setError(reason instanceof Error && reason.message ? reason.message : "无法读取赛后归因");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [mapNumber, raybetMatchId, refresh]);

  return (
    <section className="workspace-section postmatch-section" aria-label="OpenDota 赛后归因">
      <div className="section-heading compact postmatch-heading">
        <div>
          <h2>OpenDota 赛后归因</h2>
          <p>仅显示 exact mapping、confirmed reconciliation 与同局时钟对齐结果</p>
        </div>
        {result && <PostmatchStatus status={result.status} />}
      </div>

      {mapNumber == null ? (
        <PostmatchNotice
          detail="当前赔率盘没有可解析的局号，因此不会请求或猜测 OpenDota 比赛。"
          title="赛后归因不可用"
        />
      ) : loading ? (
        <PostmatchSkeleton />
      ) : error ? (
        <div className="postmatch-error" role="alert">
          <WarningCircle size={20} weight="fill" aria-hidden="true" />
          <div><strong>赛后归因读取失败</strong><span>{error}</span></div>
          <Button
            appearance="secondary"
            icon={<ArrowClockwise size={15} />}
            onClick={() => setRefresh((value) => value + 1)}
          >
            重试
          </Button>
        </div>
      ) : result?.status === "review" ? (
        <PostmatchNotice
          code={result.reason}
          detail={reasonText(result.reason, "该局映射或结算存在冲突，需人工复核后才能展示赛后事实。")}
          review
          title="赛后归因待复核"
        />
      ) : result?.status === "unavailable" ? (
        <PostmatchNotice
          code={result.reason}
          detail={reasonText(result.reason, "该局尚无满足 exact link 要求的 OpenDota 赛后事实。")}
          title="赛后归因不可用"
        />
      ) : result?.status === "available" && result.postmatch ? (
        <AvailablePostmatch
          payload={result.postmatch}
          result={result}
        />
      ) : result ? (
        <PostmatchNotice
          code={result.reason || "available_payload_missing"}
          detail="接口状态与赛后数据不一致，已停止展示。"
          title="赛后归因不可用"
        />
      ) : null}
    </section>
  );
}

function AvailablePostmatch({
  payload,
  result,
}: {
  payload: ExactPostmatchPayload;
  result: ExactPostmatchAttribution;
}) {
  const teamNames = resolvePostmatchTeamNames(
    result.mapping,
    payload.match.radiant_team_id,
    payload.match.dire_team_id,
  );
  const players = useMemo(
    () => mergePlayers(payload.player_performance, payload.player_scores),
    [payload.player_performance, payload.player_scores],
  );

  return (
    <div className="postmatch-content">
      {result.warnings?.map((warning, index) => (
        <PostmatchNotice
          code={warning}
          detail={reasonText(
            warning,
            "历史赛后事实已按不可变映射保留，但当前 live mapping 状态需要复核。",
          )}
          key={`${warning}-${index}`}
          review
          title="当前 mapping 状态待复核"
        />
      ))}
      <div className="postmatch-source-line">
        <span><CheckCircle size={16} weight="fill" aria-hidden="true" />OpenDota #{payload.match.match_id}</span>
        <span>赔率时间点 {result.odds_timeline.length}</span>
        <span>第 {result.map_number} 局</span>
      </div>

      <div className="postmatch-state-grid">
        <StateSummary name={teamNames.radiant} state={payload.states.radiant} />
        <StateSummary name={teamNames.dire} state={payload.states.dire} />
      </div>

      <div className="postmatch-availability" aria-label="赛后事件来源完整性">
        {AVAILABILITY_LABELS.map(([key, label]) => (
          <span className={payload.event_availability[key] ? "ready" : "missing"} key={key}>
            {label} {payload.event_availability[key] ? "完整" : "缺失"}
          </span>
        ))}
      </div>
      {payload.event_availability.missing_reasons.length > 0 && (
        <div className="postmatch-missing-reasons">
          <span>来源缺失原因</span>
          {payload.event_availability.missing_reasons.map((reason) => <code key={reason}>{reason}</code>)}
        </div>
      )}

      <section className="postmatch-subsection" aria-labelledby="postmatch-events-title">
        <div className="postmatch-subheading">
          <div>
            <h3 id="postmatch-events-title">赔率与局内事件时间线</h3>
            <p>概率取事件时刻之前最近的同局可信赔率点</p>
          </div>
          <ClockCounterClockwise size={18} aria-hidden="true" />
        </div>
        {payload.events.length ? (
          <EventTable
            events={payload.events}
            teamOne={teamNames.teamOne}
            teamTwo={teamNames.teamTwo}
          />
        ) : (
          <div className="postmatch-empty">
            {eventSourcesComplete(payload.event_availability)
              ? "完整来源中未记录到可展示事件"
              : "事件来源不完整，不能把空结果解释为没有事件"}
          </div>
        )}
      </section>

      <section className="postmatch-subsection" aria-labelledby="postmatch-players-title">
        <div className="postmatch-subheading">
          <div>
            <h3 id="postmatch-players-title">选手实际表现</h3>
            <p>OpenDota 赛后统计与本项目逐局评分并列展示</p>
          </div>
          <ChartBar size={18} aria-hidden="true" />
        </div>
        {players.length ? <PlayerTable players={players} /> : (
          <div className="postmatch-empty">该局暂无可展示的选手赛后表现</div>
        )}
      </section>
    </div>
  );
}

function PostmatchStatus({ status }: { status: ExactPostmatchAttribution["status"] }) {
  const text = status === "available" ? "exact 已确认" : status === "review" ? "待复核" : "不可用";
  return <span className={`postmatch-status ${status}`}>{text}</span>;
}

function PostmatchNotice({
  code,
  detail,
  review = false,
  title,
}: {
  code?: string;
  detail: string;
  review?: boolean;
  title: string;
}) {
  return (
    <div className={review ? "postmatch-notice review" : "postmatch-notice"}>
      <WarningCircle size={20} aria-hidden="true" />
      <div>
        <strong>{title}</strong>
        <span>{detail}</span>
        {code && <code>{code}</code>}
      </div>
    </div>
  );
}

function StateSummary({ name, state }: { name: string; state: IntelligenceTeamState | null }) {
  if (!state) {
    return <article className="postmatch-state empty"><strong>{name}</strong><span>局势证据不可用</span></article>;
  }
  return (
    <article className={`postmatch-state state-${state.label}`}>
      <header><strong>{name}</strong><span>{STATE_LABELS[state.label]}</span></header>
      <dl>
        <div><dt>最大优势</dt><dd>{signedAmount(state.max_lead)}</dd></div>
        <div><dt>最大劣势</dt><dd>{signedAmount(state.max_deficit)}</dd></div>
        <div><dt>领先占比</dt><dd>{formatPercent(state.ahead_fraction)}</dd></div>
        <div><dt>曲线覆盖</dt><dd>{formatPercent(state.curve_coverage)}</dd></div>
      </dl>
    </article>
  );
}

function EventTable({
  events,
  teamOne,
  teamTwo,
}: {
  events: ExactPostmatchEvent[];
  teamOne: string;
  teamTwo: string;
}) {
  return (
    <div className="postmatch-table-scroll postmatch-event-scroll">
      <table className="postmatch-table">
        <thead><tr>
          <th>时间</th><th>类型</th><th>事件</th><th>天辉金币差</th><th>天辉经验差</th>
          <th>{teamOne}（team_one）概率</th><th>{teamTwo}（team_two）概率</th>
        </tr></thead>
        <tbody>
          {events.map((event, index) => (
            <tr key={`${event.game_time_seconds}-${event.event_type}-${index}`}>
              <td className="postmatch-number">{formatClock(event.game_time_seconds)}</td>
              <td>{EVENT_LABELS[event.event_type]}</td>
              <td><strong>{eventLabel(event.label)}</strong><small>{sideLabel(event.side)}</small></td>
              <td className="postmatch-number">{signedAmount(event.radiant_gold_adv)}</td>
              <td className="postmatch-number">{signedAmount(event.radiant_xp_adv)}</td>
              <td className="postmatch-number">{formatPercent(event.team_one_probability)}</td>
              <td className="postmatch-number">{formatPercent(event.team_two_probability)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

interface PlayerRow extends IntelligencePlayerPerformance {
  score: IntelligencePlayerMapScore | null;
}

function PlayerTable({ players }: { players: PlayerRow[] }) {
  return (
    <div className="postmatch-table-scroll">
      <table className="postmatch-table postmatch-player-table">
        <thead><tr>
          <th>阵营</th><th>选手</th><th>英雄</th><th>K/D/A</th><th>GPM / XPM</th><th>净值</th>
          <th>英雄伤害</th><th>执行分</th><th>赛果修正</th>
        </tr></thead>
        <tbody>
          {players.map((player) => (
            <tr key={`${player.player_slot}-${player.account_id ?? "unknown"}`}>
              <td>{sideLabel(player.side)}</td>
              <td><strong>{player.player_name || `账号 ${player.account_id ?? "未知"}`}</strong></td>
              <td>{player.hero_name || `英雄 ${player.hero_id ?? "未知"}`}</td>
              <td className="postmatch-number">{performanceTriple(player, "kills", "deaths", "assists")}</td>
              <td className="postmatch-number">{performancePair(player, "gold_per_min", "xp_per_min")}</td>
              <td className="postmatch-number">{integer(player.performance?.net_worth)}</td>
              <td className="postmatch-number">{integer(player.performance?.hero_damage)}</td>
              <td className="postmatch-number score">{decimal(player.score?.execution_score)}</td>
              <td className="postmatch-number">{decimal(player.score?.result_adjusted_score)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function mergePlayers(
  performance: IntelligencePlayerPerformance[],
  scores: IntelligencePlayerMapScore[],
): PlayerRow[] {
  const bySlot = new Map(scores.map((score) => [score.player_slot, score]));
  const rows = performance.map((player) => ({ ...player, score: bySlot.get(player.player_slot) || null }));
  const slots = new Set(performance.map((player) => player.player_slot));
  for (const score of scores) {
    if (!slots.has(score.player_slot)) rows.push({ ...score, score });
  }
  return rows.sort((left, right) => left.player_slot - right.player_slot);
}

function performanceTriple(
  player: IntelligencePlayerPerformance,
  first: "kills",
  second: "deaths",
  third: "assists",
): string {
  const value = player.performance;
  return value ? `${integer(value[first])} / ${integer(value[second])} / ${integer(value[third])}` : "-";
}

function performancePair(
  player: IntelligencePlayerPerformance,
  first: "gold_per_min",
  second: "xp_per_min",
): string {
  const value = player.performance;
  return value ? `${integer(value[first])} / ${integer(value[second])}` : "-";
}

function sideLabel(side: "radiant" | "dire" | null): string {
  return side === "radiant" ? "天辉" : side === "dire" ? "夜魇" : "未知";
}

const MISSING_MAPPING_NAME = "映射名称缺失";

interface PostmatchTeamNames {
  teamOne: string;
  teamTwo: string;
  radiant: string;
  dire: string;
}

function resolvePostmatchTeamNames(
  mapping: ExactPostmatchAttribution["mapping"],
  radiantTeamId: number | null,
  direTeamId: number | null,
): PostmatchTeamNames {
  const missing = {
    teamOne: MISSING_MAPPING_NAME,
    teamTwo: MISSING_MAPPING_NAME,
    radiant: MISSING_MAPPING_NAME,
    dire: MISSING_MAPPING_NAME,
  };
  const canonicalTeams = mapping?.canonical_teams;
  if (!Array.isArray(canonicalTeams)) return missing;

  const teamOneCandidates = canonicalTeams.filter((team) => team?.side === "team_one");
  const teamTwoCandidates = canonicalTeams.filter((team) => team?.side === "team_two");
  if (teamOneCandidates.length !== 1 || teamTwoCandidates.length !== 1) return missing;

  const teamOne = teamOneCandidates[0];
  const teamTwo = teamTwoCandidates[0];
  const validTeam = (team: typeof teamOne) => Number.isSafeInteger(team.team_id)
    && team.team_id > 0
    && typeof team.team_name === "string"
    && team.team_name.trim().length > 0;
  if (!validTeam(teamOne) || !validTeam(teamTwo) || teamOne.team_id === teamTwo.team_id) {
    return missing;
  }

  const teamOneName = teamOne.team_name.trim();
  const teamTwoName = teamTwo.team_name.trim();
  const namesById = new Map([
    [teamOne.team_id, teamOneName],
    [teamTwo.team_id, teamTwoName],
  ]);
  const radiantName = radiantTeamId == null ? undefined : namesById.get(radiantTeamId);
  const direName = direTeamId == null ? undefined : namesById.get(direTeamId);
  const hasUniqueStateMapping = radiantTeamId !== direTeamId
    && radiantName !== undefined
    && direName !== undefined;

  return {
    teamOne: teamOneName,
    teamTwo: teamTwoName,
    radiant: hasUniqueStateMapping ? radiantName : MISSING_MAPPING_NAME,
    dire: hasUniqueStateMapping ? direName : MISSING_MAPPING_NAME,
  };
}

function signedAmount(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "-";
  return `${value > 0 ? "+" : ""}${Math.round(value).toLocaleString("zh-CN")}`;
}

function integer(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "-" : Math.round(value).toLocaleString("zh-CN");
}

function decimal(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "-" : value.toFixed(1);
}

function reasonText(reason: string, fallback: string): string {
  return {
    accepted_mapping_missing: "该局没有当前有效的 strict mapping。",
    mapping_invalidated: "该局 strict mapping 已失效。",
    strict_mapping_schema_missing: "当前数据库缺少 strict mapping 协议结构。",
    raybet_metadata_missing: "该局缺少 RayBet 身份元数据。",
    canonical_team_missing: "该局映射缺少规范球队身份。",
    waiting_for_confirmed_draft: "该局已有 strict mapping，但仍缺少可信的已确认阵容与选边证据，赛后分析正在等待。",
    reconciliation_missing: "该局尚未建立赛后结算核对。",
    reconciliation_pending: "赛后结算核对尚未确认。",
    reconciliation_review_required: "赛后结算核对已进入人工复核。",
    reconciliation_causal_order_invalid: "结算核对时间早于 mapping、时间顺序异常，或缺少可验证时区。",
    reconciliation_schema_unavailable: "当前数据库缺少赛后结算核对协议结构。",
    reconciliation_mapping_authority_missing: "历史结算缺少不可变 mapping authority，需人工复核。",
    opendota_match_link_conflict: "同一 OpenDota 比赛被多个 RayBet 地图引用，已阻止展示。",
    opendota_match_identity_invalid: "已确认结算中的 OpenDota 比赛 ID 无效。",
    opendota_match_unavailable: "exact link 指向的 OpenDota 比赛尚未归档。",
    opendota_scope_schema_unavailable: "当前数据库缺少验证 OpenDota 正式赛事范围所需的协议结构。",
    opendota_match_out_of_scope: "exact link 对应比赛不在当前正式赛事范围。",
    opendota_ingest_schema_unavailable: "当前数据库缺少验证 OpenDota 入库身份所需的协议结构。",
    opendota_ingest_unavailable: "OpenDota 比赛尚未完成正式入库。",
    opendota_ingest_review_required: "OpenDota 入库身份需要人工复核。",
    opendota_event_identity_conflict: "OpenDota 赛事身份与 strict mapping 不一致。",
    opendota_map_number_conflict: "OpenDota 地图局号与 strict mapping 不一致。",
    opendota_team_identity_conflict: "OpenDota 双方球队身份与 strict mapping 不一致。",
    opendota_result_identity_conflict: "OpenDota 比赛结果缺失或类型无效，无法确认胜方身份。",
    opendota_winner_identity_conflict: "OpenDota 胜方身份与已确认结算不一致。",
    reconciliation_winner_conflict: "RayBet 与 OpenDota 的已确认胜方不一致。",
    settlement_evidence_schema_unavailable: "当前数据库缺少验证结算证据所需的协议结构。",
    settlement_evidence_missing: "已确认结算缺少不可变的双方赛果证据。",
    settlement_evidence_conflict: "已确认结算的赛果证据互相冲突。",
    reconciliation_mapping_lineage_unverified: "无法证明结算发生时使用的是同一条有效 mapping。",
    current_mapping_changed: "当前 live mapping 已变更，历史内容仍按结算时的不可变 mapping 展示。",
    map_result_schema_unavailable: "当前数据库缺少赛果 mapping authority 协议结构。",
    map_result_missing: "已确认结算缺少对应的不可变赛果记录。",
    map_result_mapping_lineage_unverified: "赛果记录与结算时的不可变 mapping 不一致。",
    map_result_causal_order_invalid: "赛果记录的时间顺序无法通过因果校验。",
    settlement_evidence_causal_order_invalid: "赛果证据早于 mapping，或证据时间缺少可验证时区。",
    raybet_match_schema_unavailable: "当前数据库缺少 RayBet 比赛身份协议结构。",
  }[reason] || fallback;
}

function eventLabel(value: string): string {
  return {
    economy_snapshot: "经济快照",
    teamfight: "团战",
    buyback: "买活",
    CHAT_MESSAGE_ROSHAN_KILL: "肉山击杀",
    CHAT_MESSAGE_TOWER_KILL: "防御塔摧毁",
    CHAT_MESSAGE_BARRACKS_KILL: "兵营摧毁",
  }[value] || value;
}

function eventSourcesComplete(
  value: ExactPostmatchPayload["event_availability"],
): boolean {
  return value.gold_advantage
    && value.xp_advantage
    && value.objectives
    && value.teamfights
    && value.buybacks;
}

function PostmatchSkeleton() {
  return (
    <Skeleton className="postmatch-skeleton" aria-label="正在加载 OpenDota 赛后归因">
      <SkeletonItem shape="rectangle" />
      <SkeletonItem shape="rectangle" />
      <SkeletonItem shape="rectangle" />
    </Skeleton>
  );
}
