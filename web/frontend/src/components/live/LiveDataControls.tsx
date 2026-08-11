import { Button } from "@fluentui/react-components";
import {
  ArrowsLeftRight,
  Lock,
  MagnifyingGlass,
  Pulse,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";

import {
  correctLiveGameSnapshot,
  createLiveDraftPrediction,
  fetchLiveDraftPrediction,
  fetchHeroGrid,
  fetchTeamGrid,
  saveLiveDraftMapping,
} from "../../api";
import { formatClock, formatDateTime, formatOdds, formatPercent } from "../../format";
import type {
  CanonicalTeam,
  GameWorkspaceDetail,
  LiveDraftContextTeam,
  LiveDraftMapping,
  LiveDraftProspectivePrediction,
  LiveDraftSlot,
  MapDecisionCheckpoint,
  LiveHudObservation,
  PrematchHero,
  PrematchHeroGrid,
} from "../../types";

interface LiveDataControlsProps {
  csrfToken: string | null;
  detail: GameWorkspaceDetail;
  readOnly?: boolean;
}

const sides = ["radiant", "dire"] as const;
type Side = (typeof sides)[number];
type Attribute = keyof PrematchHeroGrid;
const POSITIONS = ["Carry", "Mid", "Offlane", "Soft Support", "Hard Support"];
const ATTRIBUTE_LABELS: Record<Attribute, string> = {
  str: "力量",
  agi: "敏捷",
  int: "智力",
  all: "全才",
};
const PREDICTION_CONFIRMATION = "本次模型只使用队伍历史与已锁定阵容，不使用击杀、经济、经验、防御塔、肉山、实时赔率或其他游戏内状态。";

function blankSlots(): LiveDraftSlot[] {
  return sides.flatMap((side) => Array.from({ length: 5 }, (_, index) => ({
    team_id: 0,
    side,
    position: index + 1,
    hero_id: 0,
    player_id: null,
  })));
}

function contextSlots(detail: GameWorkspaceDetail): LiveDraftSlot[] {
  if (detail.draft_mapping) return detail.draft_mapping.slots;
  const teams = detail.draft_context?.status === "ready"
    ? detail.draft_context.teams
    : [];
  if (teams.length !== 2) return blankSlots();
  return teams.flatMap((team, teamIndex) => Array.from({ length: 5 }, (_, index) => ({
    team_id: team.team_id,
    side: teamIndex === 0 ? "radiant" as const : "dire" as const,
    position: index + 1,
    hero_id: 0,
    player_id: null,
  })));
}

export function LiveDataControls({ csrfToken, detail, readOnly = false }: LiveDataControlsProps) {
  const [mapping, setMapping] = useState<LiveDraftMapping | null>(
    detail.draft_mapping || null,
  );
  const [slots, setSlots] = useState<LiveDraftSlot[]>(
    contextSlots(detail),
  );
  const [mapNumber, setMapNumber] = useState(
    detail.current_map_number
      || detail.draft_mapping?.map_number
      || detail.latest_game_snapshot?.map_number
      || detail.latest_hud_observation?.map_number
      || detail.latest_vision?.map_number
      || detail.latest_capture?.map_number
      || 1,
  );
  const [locked, setLocked] = useState(detail.draft_mapping?.is_locked || false);
  const [evidenceSourceUrl, setEvidenceSourceUrl] = useState(
    detail.draft_mapping?.evidence_source_url || "",
  );
  const [editorOpen, setEditorOpen] = useState(false);
  const [mappingBusy, setMappingBusy] = useState(false);
  const [mappingMessage, setMappingMessage] = useState<string | null>(null);
  const [prediction, setPrediction] = useState<LiveDraftProspectivePrediction | null>(null);
  const [decisionCheckpoints, setDecisionCheckpoints] = useState<MapDecisionCheckpoint[]>(
    detail.decision_checkpoints || [],
  );
  const [previousPrediction, setPreviousPrediction] = useState<LiveDraftProspectivePrediction | null>(null);
  const [predictionConfirmed, setPredictionConfirmed] = useState(false);
  const [predictionBusy, setPredictionBusy] = useState(false);
  const [predictionMessage, setPredictionMessage] = useState<string | null>(null);
  const [snapshots, setSnapshots] = useState(detail.game_snapshots || []);
  const latest = snapshots.at(-1) || detail.latest_game_snapshot || null;
  const pregameCheckpoint = decisionCheckpoints.find((item) => item.phase === "pregame") || null;
  const latestHud = detail.latest_hud_observation || null;
  const visionRuntime = detail.vision_runtime || null;
  const [gameValues, setGameValues] = useState({
    game_time_seconds: latest?.game_time_seconds
      ?? latestHud?.game_clock_seconds
      ?? detail.latest_vision?.game_clock_seconds
      ?? 0,
    radiant_networth: latest?.radiant_networth ?? 0,
    dire_networth: latest?.dire_networth ?? 0,
    radiant_kills: latest?.radiant_kills ?? latestHud?.radiant_kills ?? null,
    dire_kills: latest?.dire_kills ?? latestHud?.dire_kills ?? null,
  });
  const [snapshotBusy, setSnapshotBusy] = useState(false);
  const [snapshotMessage, setSnapshotMessage] = useState<string | null>(null);
  const [heroGrid, setHeroGrid] = useState<PrematchHeroGrid>({
    str: [], agi: [], int: [], all: [],
  });
  const [heroCatalogLoaded, setHeroCatalogLoaded] = useState(false);
  const [heroCatalogBusy, setHeroCatalogBusy] = useState(false);
  const [heroCatalogError, setHeroCatalogError] = useState<string | null>(null);
  const [teamCatalog, setTeamCatalog] = useState<CanonicalTeam[]>([]);
  const [teamCatalogLoaded, setTeamCatalogLoaded] = useState(false);
  const [teamCatalogBusy, setTeamCatalogBusy] = useState(false);
  const [teamCatalogError, setTeamCatalogError] = useState<string | null>(null);
  const [picker, setPicker] = useState<{ side: Side; position: number } | null>(null);
  const [attribute, setAttribute] = useState<Attribute>("str");
  const [heroSearch, setHeroSearch] = useState("");
  const heroSearchRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    setMapping(detail.draft_mapping || null);
    setSlots(contextSlots(detail));
    setLocked(detail.draft_mapping?.is_locked || false);
    setEvidenceSourceUrl(detail.draft_mapping?.evidence_source_url || "");
    setSnapshots(detail.game_snapshots || []);
    const nextLatest = detail.latest_game_snapshot;
    if (nextLatest) {
      setGameValues({
        game_time_seconds: nextLatest.game_time_seconds,
        radiant_networth: nextLatest.radiant_networth,
        dire_networth: nextLatest.dire_networth,
        radiant_kills: nextLatest.radiant_kills,
        dire_kills: nextLatest.dire_kills,
      });
      setMapNumber(nextLatest.map_number);
    } else if (detail.current_map_number) {
      setMapNumber(detail.current_map_number);
    } else if (detail.draft_mapping) {
      setMapNumber(detail.draft_mapping.map_number);
    }
  }, [
    detail.raybet_match_id,
    detail.current_map_number,
    detail.draft_mapping?.version,
    detail.latest_game_snapshot?.snapshot_id,
  ]);

  useEffect(() => {
    if (!mapping?.is_locked) {
      setPrediction(null);
      setDecisionCheckpoints([]);
      setPredictionMessage(null);
      return undefined;
    }
    const controller = new AbortController();
    void fetchLiveDraftPrediction(
      detail.raybet_match_id,
      mapping.map_number,
      mapping.version,
      controller.signal,
    ).then((response) => {
      setPrediction((current) => current ?? response.prediction);
      setDecisionCheckpoints(response.decision_checkpoints || []);
    }).catch((error) => {
      if (!controller.signal.aborted) {
        setPredictionMessage(error instanceof Error ? error.message : "阵容预测读取失败");
      }
    });
    return () => controller.abort();
  }, [detail.raybet_match_id, mapping?.is_locked, mapping?.map_number, mapping?.version]);

  const validSlots = useMemo(() => (
    slots.length === 10
    && slots.every((slot) => slot.team_id > 0 && slot.hero_id > 0)
    && new Set(slots.map((slot) => slot.hero_id)).size === 10
    && slots.find((slot) => slot.side === "radiant")?.team_id
      !== slots.find((slot) => slot.side === "dire")?.team_id
  ), [slots]);

  useEffect(() => {
    if (!editorOpen || teamCatalogLoaded || teamCatalogBusy) return undefined;
    const controller = new AbortController();
    setTeamCatalogBusy(true);
    setTeamCatalogError(null);
    void fetchTeamGrid(controller.signal).then((catalog) => {
      setTeamCatalog(catalog);
      setTeamCatalogLoaded(true);
    }).catch((error: Error) => {
      if (!controller.signal.aborted) {
        setTeamCatalogError(error.message || "队伍目录加载失败");
      }
    }).finally(() => {
      if (!controller.signal.aborted) setTeamCatalogBusy(false);
    });
    return () => controller.abort();
  }, [editorOpen, teamCatalogLoaded]);

  useEffect(() => {
    if (!picker) return undefined;
    const timer = window.setTimeout(() => heroSearchRef.current?.focus(), 0);
    return () => window.clearTimeout(timer);
  }, [picker]);

  const heroes = useMemo(
    () => Object.values(heroGrid).flat(),
    [heroGrid],
  );
  const heroesById = useMemo(
    () => new Map(heroes.map((hero) => [hero.hero_id, hero])),
    [heroes],
  );
  const pickerHeroes = useMemo(() => {
    const query = heroSearch.trim().toLocaleLowerCase("zh-CN");
    return heroGrid[attribute].filter((hero) => (
      !query || hero.localized_name.toLocaleLowerCase("zh-CN").includes(query)
    ));
  }, [attribute, heroGrid, heroSearch]);
  const selectedHeroIds = useMemo(
    () => new Set(slots.filter((slot) => slot.hero_id > 0).map((slot) => slot.hero_id)),
    [slots],
  );
  const selectableTeams = useMemo(() => {
    const contextTeams = (detail.draft_context?.teams || []).map((team) => ({
      team_id: team.team_id,
      team_name: team.team_name,
      tag: null,
    }));
    return teamCatalog.length > 0 ? teamCatalog : contextTeams;
  }, [detail.draft_context?.teams, teamCatalog]);
  const pickerHeroId = picker
    ? slots.find((slot) => (
      slot.side === picker.side && slot.position === picker.position
    ))?.hero_id || 0
    : 0;

  const teamForSide = (side: Side): LiveDraftContextTeam | null => {
    const teamId = slots.find((slot) => slot.side === side)?.team_id;
    const contextTeam = detail.draft_context?.teams.find((team) => team.team_id === teamId);
    if (contextTeam) return contextTeam;
    const selected = teamCatalog.find((team) => team.team_id === teamId);
    return selected ? {
      match_side: side === "radiant" ? "team_one" : "team_two",
      team_id: selected.team_id,
      team_name: selected.team_name,
    } : null;
  };

  const setTeamForSide = (side: Side, teamId: number) => {
    setSlots((current) => current.map((slot) => (
      slot.side === side ? { ...slot, team_id: teamId } : slot
    )));
  };

  const loadHeroCatalog = () => {
    if (heroCatalogLoaded || heroCatalogBusy) return;
    setHeroCatalogBusy(true);
    setHeroCatalogError(null);
    void fetchHeroGrid().then((catalog) => {
      setHeroGrid(catalog);
      setHeroCatalogLoaded(true);
    }).catch((error: Error) => {
      setHeroCatalogError(error.message || "英雄目录加载失败");
    }).finally(() => setHeroCatalogBusy(false));
  };

  const openHeroPicker = (side: Side, position: number) => {
    setPicker({ side, position });
    loadHeroCatalog();
  };

  const closeHeroPicker = () => {
    setPicker(null);
    setHeroSearch("");
  };

  const selectHero = (hero: PrematchHero) => {
    if (!picker) return;
    setSlots((current) => current.map((slot) => (
      slot.side === picker.side && slot.position === picker.position
        ? { ...slot, hero_id: hero.hero_id }
        : slot
    )));
    closeHeroPicker();
  };

  const clearHero = (side: Side, position: number) => {
    setSlots((current) => current.map((slot) => (
      slot.side === side && slot.position === position
        ? { ...slot, hero_id: 0 }
        : slot
    )));
  };

  const swapSides = () => {
    setSlots((current) => current.map((slot) => ({
      ...slot,
      side: slot.side === "radiant" ? "dire" : "radiant",
    })));
  };

  const saveMapping = async (event: FormEvent) => {
    event.preventDefault();
    if (!csrfToken || !validSlots) return;
    setMappingBusy(true);
    setMappingMessage(null);
    try {
      const saved = await saveLiveDraftMapping(
        detail.raybet_match_id,
        mapNumber,
        slots,
        locked,
        locked ? evidenceSourceUrl.trim() : null,
        csrfToken,
      );
      if (prediction && prediction.identity.mapping_version !== saved.version) {
        setPreviousPrediction(prediction);
      }
      const automation = saved.prediction_automation;
      setPrediction(automation?.prediction || null);
      if (saved.decision_checkpoint) {
        setDecisionCheckpoints((current) => [
          ...current.filter((item) => item.checkpoint_id !== saved.decision_checkpoint?.checkpoint_id),
          saved.decision_checkpoint as MapDecisionCheckpoint,
        ]);
      }
      if (automation) {
        setPredictionMessage(automation.status === "blocked"
          ? automation.missing_reason || "阵容预测前置条件不可用"
          : automation.status === "unchanged" ? "已复用不可变预测" : "阵容预测已自动保存");
      }
      setPredictionConfirmed(false);
      setMapping(saved);
      setSlots(saved.slots);
      setLocked(saved.is_locked);
      setMappingMessage(`已保存版本 ${saved.version}`);
      setEditorOpen(false);
    } catch (error) {
      setMappingMessage(error instanceof Error ? error.message : "阵容保存失败");
    } finally {
      setMappingBusy(false);
    }
  };

  const generatePrediction = async () => {
    if (!csrfToken || !mapping?.is_locked || !predictionConfirmed) return;
    setPredictionBusy(true);
    setPredictionMessage(null);
    try {
      const response = await createLiveDraftPrediction(
        detail.raybet_match_id,
        mapping.map_number,
        mapping.version,
        csrfToken,
      );
      setPrediction(response.prediction);
      if (response.decision_checkpoint) {
        setDecisionCheckpoints((current) => [
          ...current.filter((item) => item.checkpoint_id !== response.decision_checkpoint?.checkpoint_id),
          response.decision_checkpoint as MapDecisionCheckpoint,
        ]);
      }
      setPredictionMessage(response.status === "blocked"
        ? response.missing_reason || "阵容预测前置条件不可用"
        : response.status === "unchanged" ? "已复用不可变预测" : "阵容预测已保存");
    } catch (error) {
      setPredictionMessage(error instanceof Error ? error.message : "阵容预测失败");
    } finally {
      setPredictionBusy(false);
    }
  };

  const saveSnapshot = async (event: FormEvent) => {
    event.preventDefault();
    if (!csrfToken) return;
    setSnapshotBusy(true);
    setSnapshotMessage(null);
    try {
      const saved = await correctLiveGameSnapshot(
        detail.raybet_match_id,
        mapNumber,
        gameValues,
        csrfToken,
      );
      setSnapshots((current) => [...current, saved].slice(-120));
      setSnapshotMessage("比赛状态修正已追加");
    } catch (error) {
      setSnapshotMessage(error instanceof Error ? error.message : "状态修正失败");
    } finally {
      setSnapshotBusy(false);
    }
  };

  return (
    <section className="live-data-grid" aria-label="本局人工阵容与 Vision 实时状态">
      <section className="workspace-section live-draft-summary">
        <div className="section-heading compact">
          <div>
            <h2>本局阵容映射</h2>
            <p>{mapping
              ? `版本 ${mapping.version} · ${mapping.is_locked ? "已人工锁定" : "尚未锁定"} · ${formatDateTime(mapping.created_at)}`
              : "等待人工确认队伍、英雄与 1–5 号位"}</p>
          </div>
          <Lock size={19} aria-hidden="true" />
        </div>
        {mapping?.evidence_source_url && (
          <a href={mapping.evidence_source_url} rel="noreferrer" target="_blank">
            阵容证据来源
          </a>
        )}
        <div className="live-draft-summary-teams">
          <DraftSummaryTeam
            side="radiant"
            slots={slots.filter((slot) => slot.side === "radiant")}
            team={teamForSide("radiant")}
          />
          <DraftSummaryTeam
            side="dire"
            slots={slots.filter((slot) => slot.side === "dire")}
            team={teamForSide("dire")}
          />
        </div>
        <div className="live-form-actions">
          {(detail.current_map_number || mapping || latest) && (
            <span className="live-form-message">第 {mapNumber} 局</span>
          )}
          {!readOnly && (
            <Button
              appearance="primary"
              onClick={() => setEditorOpen(true)}
              type="button"
            >
              {mapping ? "编辑阵容" : "录入阵容"}
            </Button>
          )}
        </div>
        {detail.draft_context?.status !== "ready" && !mapping && (
          <div className="live-state-empty" role="status">
            <WarningCircle size={18} aria-hidden="true" />
            <span>当前赛事的 canonical 队伍尚未唯一解析，请在阵容录入中手动选择。</span>
          </div>
        )}
        {mappingMessage && <p className="live-form-message" role="status">{mappingMessage}</p>}
        {!mapping?.is_locked ? (
          <div className="live-state-empty" role="status">
            <WarningCircle size={18} aria-hidden="true" />
            <span>请先确认并锁定阵容。</span>
          </div>
        ) : prediction ? (
          <>
            <div className="live-state-summary" aria-label="阵容 prospective shadow 预测">
              <div><dt>Mapping</dt><dd>v{prediction.identity.mapping_version}</dd></div>
              <div><dt>Team Rating P0</dt><dd>{formatPercent(prediction.p0_probability)}</dd></div>
              <div><dt>R.O.S.H. P1</dt><dd>{prediction.p1_probability == null ? "P0-only" : formatPercent(prediction.p1_probability)}</dd></div>
              <div><dt>Pure score</dt><dd>{prediction.pure_rosh_score?.toFixed(4) ?? "-"}</dd></div>
              {prediction.missing_reason && <div><dt>缺失原因</dt><dd>{prediction.missing_reason}</dd></div>}
              <div><dt>因果状态</dt><dd>{prediction.causal_evidence.causal_status}</dd></div>
              <div><dt>生成时间</dt><dd>{formatDateTime(prediction.created_at)}</dd></div>
            </div>
            {pregameCheckpoint ? (
              <>
                <div className="live-state-summary" aria-label="影子投注决策">
                  <div>
                    <dt>影子决策</dt>
                    <dd>{pregameCheckpoint.decision}</dd>
                  </div>
                  <div><dt>策略版本</dt><dd>map-decision-shadow-v1</dd></div>
                  {pregameCheckpoint.selected_edge != null && (
                    <div><dt>价值差</dt><dd>{formatPercent(pregameCheckpoint.selected_edge)}</dd></div>
                  )}
                  {pregameCheckpoint.observed_price != null && (
                    <div><dt>决策赔率</dt><dd>{formatOdds(pregameCheckpoint.observed_price)}</dd></div>
                  )}
                  <div><dt>原因</dt><dd>{pregameCheckpoint.reason}</dd></div>
                  <div><dt>检查点 ID</dt><dd>#{pregameCheckpoint.checkpoint_id}</dd></div>
                </div>
                <p className="live-form-message">
                  服务端已追加只读记录；该建议不会回写 P0/P1、创建订单或提交真实投注。
                </p>
              </>
            ) : (
              <div className="live-state-empty" role="status">
                <WarningCircle size={18} aria-hidden="true" />
                <span>尚未保存赛前决策检查点</span>
              </div>
            )}
          </>
        ) : readOnly ? (
          <div className="live-state-empty" role="status">该 mapping 没有保存预测。</div>
        ) : (
          <div className="live-draft-prediction-control">
            <label className="lock-toggle">
              <input
                checked={predictionConfirmed}
                onChange={(event) => setPredictionConfirmed(event.target.checked)}
                type="checkbox"
              />
              {PREDICTION_CONFIRMATION}
            </label>
            <Button
              appearance="primary"
              disabled={!csrfToken || !predictionConfirmed || predictionBusy}
              onClick={() => void generatePrediction()}
              type="button"
            >
              {predictionBusy ? "生成中…" : "生成实时阵容预测"}
            </Button>
          </div>
        )}
        {previousPrediction && mapping && previousPrediction.identity.mapping_version !== mapping.version && (
          <p className="live-form-message" role="status">
            旧预测绑定 mapping v{previousPrediction.identity.mapping_version}；当前为 v{mapping.version}，不会覆盖旧记录。
          </p>
        )}
        {predictionMessage && <p className="live-form-message" role="status">{predictionMessage}</p>}
      </section>

      <form className="workspace-section live-state-panel" onSubmit={saveSnapshot}>
        <div className="section-heading compact">
          <div>
            <h2>Vision 实时状态</h2>
            <p>{latest
              ? `${latest.source === "vision" ? "自动识别" : "人工修正"} · ${formatDateTime(latest.captured_at)}`
              : latestHud?.status === "available"
                ? `Vision HUD · ${formatDateTime(latestHud.captured_at)}`
              : visionRuntime
                ? `Stable Vision · ${formatVisionCaptureState(visionRuntime.capture_state)} · ${formatDateTime(visionRuntime.observed_at)}`
                : "等待稳定确认的比赛时间与双方经济"}</p>
          </div>
          <Pulse size={19} aria-hidden="true" />
        </div>
        {latest ? (
          <dl className="live-state-summary">
            <div><dt>比赛时间</dt><dd>{formatClock(latest.game_time_seconds)}</dd></div>
            <div><dt>天辉经济</dt><dd>{formatNetworth(latest.radiant_networth)}</dd></div>
            <div><dt>夜魇经济</dt><dd>{formatNetworth(latest.dire_networth)}</dd></div>
            <div><dt>经济差</dt><dd>{formatLead(latest.networth_lead)}</dd></div>
            <div><dt>击杀</dt><dd>{latest.radiant_kills ?? "-"} : {latest.dire_kills ?? "-"}</dd></div>
            <div><dt>置信度</dt><dd>{formatPercent(latest.vision_confidence)}</dd></div>
          </dl>
        ) : latestHud?.status === "available" ? (
          <dl className="live-state-summary">
            <div><dt>比赛时间</dt><dd>{formatClock(latestHud.game_clock_seconds ?? 0)}</dd></div>
            <div><dt>状态</dt><dd>{latestHud.is_paused === null ? "-" : latestHud.is_paused ? "暂停" : "进行中"}</dd></div>
            <div><dt>击杀</dt><dd>{latestHud.radiant_kills ?? "-"} : {latestHud.dire_kills ?? "-"}</dd></div>
            <div><dt>经济领先</dt><dd>{formatHudAdvantage(latestHud)}</dd></div>
            <div><dt>精确总经济</dt><dd>{formatHudNetworth(latestHud)}</dd></div>
            <div><dt>HUD 置信度</dt><dd>{formatPercent(latestHud.hud_confidence)}</dd></div>
          </dl>
        ) : (
          <div className="live-state-empty">
            <WarningCircle size={18} aria-hidden="true" />
            <span>{visionRuntime?.blocker_code || visionRuntime?.reason
              ? `Vision 暂不产出可信状态：${formatVisionBlocker(visionRuntime.blocker_code || visionRuntime.reason)}`
              : latestHud?.unavailable_reason
                ? `HUD 暂不可用：${formatVisionBlocker(latestHud.unavailable_reason)}`
                : "绝对总经济 OCR 尚未产生有效快照，可先人工纠正。"}</span>
          </div>
        )}
        {!readOnly && <div className="manual-state-grid">
          <NumericField
            label="比赛时间（秒）"
            onChange={(value) => setGameValues((current) => ({
              ...current,
              game_time_seconds: value || 0,
            }))}
            value={gameValues.game_time_seconds}
          />
          <NumericField
            label="天辉总经济"
            onChange={(value) => setGameValues((current) => ({
              ...current,
              radiant_networth: value || 0,
            }))}
            value={gameValues.radiant_networth || null}
          />
          <NumericField
            label="夜魇总经济"
            onChange={(value) => setGameValues((current) => ({
              ...current,
              dire_networth: value || 0,
            }))}
            value={gameValues.dire_networth || null}
          />
          <NumericField
            label="天辉击杀（可选）"
            onChange={(value) => setGameValues((current) => ({
              ...current,
              radiant_kills: value,
            }))}
            value={gameValues.radiant_kills}
          />
          <NumericField
            label="夜魇击杀（可选）"
            onChange={(value) => setGameValues((current) => ({
              ...current,
              dire_kills: value,
            }))}
            value={gameValues.dire_kills}
          />
        </div>}
        <div className="live-form-actions">
          {detail.latest_capture?.frame_url && (
            <a href={detail.latest_capture.frame_url} rel="noreferrer" target="_blank">
              查看最近截图
            </a>
          )}
          {!readOnly && <Button
            disabled={
              !csrfToken
              || snapshotBusy
              || gameValues.radiant_networth <= 0
              || gameValues.dire_networth <= 0
            }
            type="submit"
          >
            {snapshotBusy ? "保存中" : "追加人工修正"}
          </Button>}
        </div>
        {snapshotMessage && <p className="live-form-message" role="status">{snapshotMessage}</p>}
        {snapshots.length > 1 && (
          <div className="live-state-history">
            {snapshots.slice(-5).reverse().map((snapshot) => (
              <span key={snapshot.snapshot_id}>
                {formatClock(snapshot.game_time_seconds)} · {formatLead(snapshot.networth_lead)}
              </span>
            ))}
          </div>
        )}
      </form>

      {editorOpen && !readOnly && (
        <div
          aria-label="阵容录入"
          aria-modal="true"
          className="live-draft-modal"
          role="dialog"
        >
          <form
            className="workspace-section live-draft-editor live-draft-modal-card"
            onSubmit={saveMapping}
          >
            <div className="section-heading compact">
              <div>
                <h2>{mapping ? "编辑本局阵容" : "录入本局阵容"}</h2>
                <p>选择双方英雄，并确认每个英雄的 1–5 号位。</p>
              </div>
              <button
                aria-label="关闭阵容录入"
                className="live-draft-modal-close"
                onClick={() => setEditorOpen(false)}
                type="button"
              >
                <X size={19} />
              </button>
            </div>
            <div className="live-draft-toolbar">
              <label className="live-field compact-field">
                <span>当前局数</span>
                <input
                  max={5}
                  min={1}
                  onChange={(event) => setMapNumber(Number(event.target.value))}
                  type="number"
                  value={mapNumber}
                />
              </label>
              <Button
                icon={<ArrowsLeftRight size={17} />}
                onClick={swapSides}
                type="button"
              >
                交换天辉 / 夜魇
              </Button>
            </div>
            <div className="live-team-selectors">
              {sides.map((side) => (
                <label className="live-field" key={side}>
                  <span>{side === "radiant" ? "天辉 canonical 队伍" : "夜魇 canonical 队伍"}</span>
                  <select
                    aria-label={side === "radiant" ? "选择天辉队伍" : "选择夜魇队伍"}
                    onChange={(event) => setTeamForSide(side, Number(event.target.value))}
                    value={slots.find((slot) => slot.side === side)?.team_id || 0}
                  >
                    <option value={0}>请选择队伍</option>
                    {selectableTeams.map((team) => (
                      <option key={team.team_id} value={team.team_id}>
                        {team.team_name}{team.tag ? ` · ${team.tag}` : ""}
                      </option>
                    ))}
                  </select>
                </label>
              ))}
            </div>
            {teamCatalogBusy && <p className="live-form-message" role="status">正在加载 canonical 队伍目录…</p>}
            {teamCatalogError && <p className="live-form-message" role="alert">{teamCatalogError}</p>}
            <div className="prematch-lineups live-draft-lineups">
              <DraftTeamLineup
                heroesById={heroesById}
                onClearHero={(position) => clearHero("radiant", position)}
                onOpenPicker={(position) => openHeroPicker("radiant", position)}
                side="radiant"
                slots={slots.filter((slot) => slot.side === "radiant")}
                team={teamForSide("radiant")}
              />
              <div className="prematch-versus" aria-hidden="true">VS</div>
              <DraftTeamLineup
                heroesById={heroesById}
                onClearHero={(position) => clearHero("dire", position)}
                onOpenPicker={(position) => openHeroPicker("dire", position)}
                side="dire"
                slots={slots.filter((slot) => slot.side === "dire")}
                team={teamForSide("dire")}
              />
            </div>
            <div className="live-form-actions">
              {locked && (
                <label className="live-field draft-evidence-source">
                  <span>阵容证据 URL</span>
                  <input
                    onChange={(event) => setEvidenceSourceUrl(event.target.value)}
                    required
                    type="url"
                    value={evidenceSourceUrl}
                  />
                </label>
              )}
              <label className="lock-toggle">
                <input
                  checked={locked}
                  onChange={(event) => setLocked(event.target.checked)}
                  type="checkbox"
                />
                锁定后允许生成实时阵容预测
              </label>
              <Button
                appearance="primary"
                disabled={
                  !csrfToken
                  || !validSlots
                  || mappingBusy
                  || (locked && !evidenceSourceUrl.trim())
                }
                type="submit"
              >
                {mappingBusy ? "保存中" : mapping ? "保存新版本" : "提交阵容"}
              </Button>
            </div>
            {mappingMessage && <p className="live-form-message" role="status">{mappingMessage}</p>}
          </form>
        </div>
      )}

      {picker && (
        <div aria-label="英雄选择器" className="hero-picker-dialog live-hero-picker" role="dialog">
          <div className="hero-picker-body">
            <header className="live-hero-picker-title">
              <div>
                <strong>{picker.side === "radiant" ? "天辉" : "夜魇"}</strong>
                <small>{picker.position} 号位 · {POSITIONS[picker.position - 1]}</small>
              </div>
              <button aria-label="关闭英雄选择器" onClick={closeHeroPicker} type="button">
                <X size={18} />
              </button>
            </header>
            <div className="hero-picker-content">
              <div className="hero-picker-controls">
                <div aria-label="英雄属性" role="tablist">
                  {(Object.keys(ATTRIBUTE_LABELS) as Attribute[]).map((key) => (
                    <button
                      aria-selected={attribute === key}
                      className={attribute === key ? "active" : ""}
                      key={key}
                      onClick={() => setAttribute(key)}
                      role="tab"
                      type="button"
                    >
                      {ATTRIBUTE_LABELS[key]}
                    </button>
                  ))}
                </div>
                <label className="live-hero-search">
                  <MagnifyingGlass size={16} aria-hidden="true" />
                  <input
                    aria-label="搜索英雄"
                    onChange={(event) => setHeroSearch(event.target.value)}
                    placeholder="搜索英雄"
                    ref={heroSearchRef}
                    value={heroSearch}
                  />
                </label>
              </div>
              {heroCatalogBusy && <p>正在加载英雄目录…</p>}
              {heroCatalogError && <p className="live-form-message" role="alert">{heroCatalogError}</p>}
              <div className="hero-picker-grid">
                {pickerHeroes.map((hero) => {
                  const unavailable = selectedHeroIds.has(hero.hero_id)
                    && hero.hero_id !== pickerHeroId;
                  return (
                    <button
                      disabled={unavailable}
                      key={hero.hero_id}
                      onClick={() => selectHero(hero)}
                      type="button"
                    >
                      <HeroImage hero={hero} />
                      <span>{hero.localized_name}</span>
                    </button>
                  );
                })}
                {heroCatalogLoaded && pickerHeroes.length === 0 && <p>没有匹配的英雄</p>}
              </div>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

function DraftSummaryTeam({
  side,
  slots,
  team,
}: {
  side: Side;
  slots: LiveDraftSlot[];
  team: LiveDraftContextTeam | null;
}) {
  const label = side === "radiant" ? "天辉" : "夜魇";
  return (
    <section className={`live-draft-summary-team ${side}`}>
      <header>
        <strong>{label} · {team?.team_name || "队伍待解析"}</strong>
        <span>{slots.filter((slot) => slot.hero_id > 0).length}/5</span>
      </header>
      <p>按 1–5 号位确认英雄</p>
    </section>
  );
}

function DraftTeamLineup({
  heroesById,
  onClearHero,
  onOpenPicker,
  side,
  slots,
  team,
}: {
  heroesById: ReadonlyMap<number, PrematchHero>;
  onClearHero: (position: number) => void;
  onOpenPicker: (position: number) => void;
  side: Side;
  slots: LiveDraftSlot[];
  team: LiveDraftContextTeam | null;
}) {
  const label = side === "radiant" ? "天辉" : "夜魇";
  return (
    <section className={`prematch-team ${side}`} aria-label={`${label} 阵容`}>
      <header>
        <div>
          <h3>{label} · {team?.team_name || "队伍待解析"}</h3>
          <small>位置由操作者确认</small>
        </div>
        <span>{slots.filter((slot) => slot.hero_id > 0).length}/5</span>
      </header>
      <div className="prematch-hero-slots">
        {[...slots].sort((left, right) => left.position - right.position).map((slot) => {
          const hero = heroesById.get(slot.hero_id) || null;
          return (
            <div className={slot.hero_id > 0 ? "filled" : ""} key={slot.position}>
              <button
                aria-label={`选择 ${label} ${slot.position} 号位英雄`}
                onClick={() => onOpenPicker(slot.position)}
                type="button"
              >
                <span className="position-number">{slot.position}</span>
                {hero ? <HeroImage hero={hero} /> : <span className="hero-placeholder" />}
                <span className="hero-slot-copy">
                  <strong>{hero?.localized_name || (slot.hero_id > 0 ? `英雄 ${slot.hero_id}` : POSITIONS[slot.position - 1])}</strong>
                  <small>{POSITIONS[slot.position - 1]}</small>
                </span>
              </button>
              {slot.hero_id > 0 && (
                <button
                  aria-label={`清除 ${label} ${slot.position} 号位英雄`}
                  className="live-clear-hero"
                  onClick={() => onClearHero(slot.position)}
                  type="button"
                >
                  <X size={15} />
                </button>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}

function HeroImage({ hero }: { hero: PrematchHero }) {
  return hero.image_url ? (
    <img alt="" loading="lazy" src={hero.image_url} />
  ) : (
    <span className="hero-image-fallback" aria-hidden="true">
      {hero.localized_name.slice(0, 1)}
    </span>
  );
}


function NumericField({
  label,
  onChange,
  value,
}: {
  label: string;
  onChange: (value: number | null) => void;
  value: number | null;
}) {
  return (
    <label className="live-field">
      <span>{label}</span>
      <input
        min={0}
        onChange={(event) => onChange(
          event.target.value === "" ? null : Number(event.target.value),
        )}
        type="number"
        value={value ?? ""}
      />
    </label>
  );
}

function formatNetworth(value: number): string {
  return value >= 1000 ? `${(value / 1000).toFixed(1)}K` : String(value);
}

function formatLead(value: number): string {
  if (value === 0) return "持平";
  return `${value > 0 ? "天辉" : "夜魇"} +${Math.abs(value).toLocaleString("zh-CN")}`;
}

function formatHudAdvantage(observation: LiveHudObservation): string {
  const side = observation.net_worth_advantage_side;
  const minimum = observation.net_worth_advantage_min;
  const maximum = observation.net_worth_advantage_max;
  if (!side || minimum === null || maximum === null) return "未提供";
  const label = side === "radiant" ? "天辉" : "夜魇";
  const range = minimum === maximum
    ? minimum.toLocaleString("zh-CN")
    : `${minimum.toLocaleString("zh-CN")}–${maximum.toLocaleString("zh-CN")}`;
  return `${label} +${range}`;
}

function formatHudNetworth(observation: LiveHudObservation): string {
  if (observation.radiant_net_worth === null && observation.dire_net_worth === null) {
    return "未提供";
  }
  if (observation.radiant_net_worth === null || observation.dire_net_worth === null) {
    return "不完整";
  }
  return `天辉 ${formatNetworth(observation.radiant_net_worth)} / 夜魇 ${formatNetworth(observation.dire_net_worth)}`;
}

function formatVisionCaptureState(value?: string | null): string {
  if (value === "capturing_partial") return "采集中，等待可信帧";
  if (value === "capturing") return "采集中";
  if (value === "producing_trusted") return "直播状态已可信";
  if (value === "producing") return "稳定产出";
  return value || "状态待确认";
}

function formatVisionBlocker(value?: string | null): string {
  if (value === "replay_gate_untrusted") return "直播或回放状态尚未可信确认";
  if (value === "draft_incomplete" || value === "draft_unconfirmed") {
    return "十英雄阵容尚未可信确认";
  }
  if (value === "clock_unconfirmed") return "比赛时钟尚未可信确认";
  if (value?.startsWith("screen_state_")) return `当前画面为 ${value.slice(13)}`;
  return value || "原因待确认";
}
