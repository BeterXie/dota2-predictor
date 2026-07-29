import {
  Button,
  Input,
  Select,
  Skeleton,
  SkeletonItem,
  Tab,
  TabList,
} from "@fluentui/react-components";
import {
  ArrowClockwise,
  CaretDown,
  CaretLeft,
  CaretRight,
  CaretUp,
  ChartBar,
  CheckCircle,
  Database,
  MagnifyingGlass,
  Medal,
  Sword,
  Users,
  WarningCircle,
} from "@phosphor-icons/react";
import { type FormEvent, type ReactNode, useEffect, useMemo, useState } from "react";

import {
  fetchIntelligenceMatchDetail,
  fetchIntelligenceMatches,
  fetchIntelligenceOverview,
  fetchIntelligencePlayers,
  fetchIntelligenceTeams,
} from "../api";
import type {
  IntelligenceAvailabilityMode,
  IntelligenceDraftPrediction,
  IntelligenceDraftQualitySlice,
  IntelligenceMatchDetail,
  IntelligenceMatchPage,
  IntelligenceMatchRating,
  IntelligenceMatchSummary,
  IntelligenceOverview,
  IntelligencePagination,
  IntelligencePlayerMapScore,
  IntelligencePlayerPerformance,
  IntelligencePlayerPage,
  IntelligencePlayerRanking,
  IntelligenceRoshMinutePoint,
  IntelligenceRoshLineupScoreSection,
  IntelligenceStateLabel,
  IntelligenceTeamPage,
  IntelligenceTeamProfile,
  IntelligenceTeamState,
} from "../types";
import "../intelligence.css";
import { OverviewPanel } from "./intelligence/OverviewPanel";
import { MatchRatingPanel } from "./intelligence/MatchRatingPanel";
import { RoshScorePanel } from "./intelligence/RoshScorePanel";
import { summarizeCutoffs } from "../utils/intelligenceUtils";

export type IntelligenceMode = "matches" | "players" | "teams" | "drafts";

interface IntelligenceDashboardProps {
  initialMode?: IntelligenceMode;
  initialMatchId?: number | null;
  onMatchList?: () => void;
  onMatchOpen?: (matchId: number) => void;
}

const PAGE_SIZE = 12;
const TEAM_PROFILE_STABLE_SAMPLE_THRESHOLD = 15;

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

const COVERAGE_LABELS: Record<string, string> = {
  formal_maps: "正式地图",
  scored_matches: "含选手评分比赛",
  player_score_rows: "选手逐局评分",
  scored_players: "已识别选手",
  ranking_eligible_scores: "可排名评分",
  state_labeled_matches: "已分类比赛",
  team_state_rows: "队伍视角标签",
  profiled_teams: "球队画像",
  team_profiles: "球队画像版本行",
  draft_predicted_matches: "阵容预测比赛",
  draft_prediction_rows: "阵容预测切片",
};

const RATE_LABELS: Record<string, string> = {
  comeback_after_5000_deficit: "落后 5k 翻盘率",
  throw_after_5000_lead: "领先 5k 被翻盘率",
  closeout_after_5000_lead: "领先 5k 终结率",
  roshan_to_tower: "肉山转塔率",
};

export function IntelligenceDashboard({
  initialMode = "matches",
  initialMatchId = null,
  onMatchList,
  onMatchOpen,
}: IntelligenceDashboardProps) {
  const [mode, setMode] = useState<IntelligenceMode>(initialMode);
  const [overview, setOverview] = useState<IntelligenceOverview | null>(null);
  const [overviewLoading, setOverviewLoading] = useState(false);
  const [overviewError, setOverviewError] = useState<string | null>(null);
  const [overviewRefresh, setOverviewRefresh] = useState(0);
  const [dataRefresh, setDataRefresh] = useState(0);

  const [matchPage, setMatchPage] = useState<IntelligenceMatchPage | null>(null);
  const [matchPageNumber, setMatchPageNumber] = useState(1);
  const [matchSearchDraft, setMatchSearchDraft] = useState("");
  const [matchSearch, setMatchSearch] = useState("");
  const [matchLabel, setMatchLabel] = useState("");
  const [matchLoading, setMatchLoading] = useState(false);
  const [matchError, setMatchError] = useState<string | null>(null);
  const [selectedMatchId, setSelectedMatchId] = useState<number | null>(initialMatchId);
  const [matchDetail, setMatchDetail] = useState<IntelligenceMatchDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const [playerPage, setPlayerPage] = useState<IntelligencePlayerPage | null>(null);
  const [playerPageNumber, setPlayerPageNumber] = useState(1);
  const [playerSearchDraft, setPlayerSearchDraft] = useState("");
  const [playerSearch, setPlayerSearch] = useState("");
  const [playerPosition, setPlayerPosition] = useState("");
  const [playerLoading, setPlayerLoading] = useState(false);
  const [playerError, setPlayerError] = useState<string | null>(null);

  const [teamPage, setTeamPage] = useState<IntelligenceTeamPage | null>(null);
  const [teamPageNumber, setTeamPageNumber] = useState(1);
  const [teamSearch, setTeamSearch] = useState("");
  const [teamLoading, setTeamLoading] = useState(false);
  const [teamError, setTeamError] = useState<string | null>(null);

  useEffect(() => {
    setSelectedMatchId(initialMatchId);
  }, [initialMatchId]);

  useEffect(() => {
    if (mode !== "drafts") return;
    const controller = new AbortController();
    setOverviewLoading(true);
    fetchIntelligenceOverview(controller.signal)
      .then((value) => {
        setOverview(value);
        setOverviewError(null);
      })
      .catch((reason: Error) => {
        if (reason.name !== "AbortError") {
          setOverviewError(errorMessage(reason, "无法读取历史情报总览"));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setOverviewLoading(false);
      });
    return () => controller.abort();
  }, [mode, overviewRefresh]);

  useEffect(() => {
    if (mode !== "matches") return;
    const controller = new AbortController();
    setMatchLoading(true);
    fetchIntelligenceMatches({
      page: matchPageNumber,
      pageSize: PAGE_SIZE,
      label: matchLabel || undefined,
      search: matchSearch || undefined,
    }, controller.signal)
      .then((value) => {
        setMatchPage(value);
        setMatchError(null);
      })
      .catch((reason: Error) => {
        if (reason.name !== "AbortError") {
          setMatchError(errorMessage(reason, "无法读取历史比赛"));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setMatchLoading(false);
      });
    return () => controller.abort();
  }, [dataRefresh, matchLabel, matchPageNumber, matchSearch, mode]);

  useEffect(() => {
    if (mode !== "matches" || selectedMatchId == null) {
      if (selectedMatchId == null) setMatchDetail(null);
      return;
    }
    const controller = new AbortController();
    setDetailLoading(true);
    fetchIntelligenceMatchDetail(selectedMatchId, controller.signal)
      .then((value) => {
        setMatchDetail(value);
        setDetailError(null);
      })
      .catch((reason: Error) => {
        if (reason.name !== "AbortError") {
          setDetailError(errorMessage(reason, "无法读取比赛复盘详情"));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setDetailLoading(false);
      });
    return () => controller.abort();
  }, [dataRefresh, mode, selectedMatchId]);

  useEffect(() => {
    if (mode !== "players") return;
    const controller = new AbortController();
    setPlayerLoading(true);
    fetchIntelligencePlayers({
      page: playerPageNumber,
      pageSize: PAGE_SIZE,
      position: playerPosition ? Number(playerPosition) : undefined,
      search: playerSearch || undefined,
    }, controller.signal)
      .then((value) => {
        setPlayerPage(value);
        setPlayerError(null);
      })
      .catch((reason: Error) => {
        if (reason.name !== "AbortError") {
          setPlayerError(errorMessage(reason, "无法读取选手排名"));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setPlayerLoading(false);
      });
    return () => controller.abort();
  }, [dataRefresh, mode, playerPageNumber, playerPosition, playerSearch]);

  useEffect(() => {
    if (mode !== "teams") return;
    const controller = new AbortController();
    setTeamLoading(true);
    fetchIntelligenceTeams(controller.signal)
      .then((value) => {
        setTeamPage(value);
        setTeamError(null);
      })
      .catch((reason: Error) => {
        if (reason.name !== "AbortError") {
          setTeamError(errorMessage(reason, "无法读取球队画像"));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setTeamLoading(false);
      });
    return () => controller.abort();
  }, [dataRefresh, mode]);

  const filteredTeams = useMemo(() => {
    const normalized = teamSearch.trim().toLocaleLowerCase("zh-CN");
    const data = teamPage?.data || [];
    if (!normalized) return data;
    return data.filter((team) => (
      String(team.team_id).includes(normalized)
      || (team.team_name || "").toLocaleLowerCase("zh-CN").includes(normalized)
      || (team.team_tag || "").toLocaleLowerCase("zh-CN").includes(normalized)
    ));
  }, [teamPage, teamSearch]);
  const teamPagination = clientPagination(teamPageNumber, PAGE_SIZE, filteredTeams.length);
  const visibleTeams = filteredTeams.slice(
    (teamPagination.page - 1) * PAGE_SIZE,
    teamPagination.page * PAGE_SIZE,
  );

  const submitMatchSearch = (event: FormEvent) => {
    event.preventDefault();
    setMatchPageNumber(1);
    setMatchSearch(matchSearchDraft.trim());
  };

  const submitPlayerSearch = (event: FormEvent) => {
    event.preventDefault();
    setPlayerPageNumber(1);
    setPlayerSearch(playerSearchDraft.trim());
  };

  const retryAll = () => {
    setDataRefresh((value) => value + 1);
    setOverviewRefresh((value) => value + 1);
  };

  const selectMatch = (matchId: number) => {
    setSelectedMatchId(matchId);
    setDetailError(null);
    onMatchOpen?.(matchId);
  };

  const showMatchList = () => {
    setSelectedMatchId(null);
    setDetailError(null);
    onMatchList?.();
  };

  const changeMode = (next: IntelligenceMode) => {
    setMode(next);
    if (next !== "matches" && selectedMatchId != null) showMatchList();
  };

  return (
    <section className="intelligence-dashboard" aria-label="历史比赛情报">
      <header className="intel-header">
        <div>
          <span className="intel-title-icon"><Database size={19} weight="bold" /></span>
          <div>
            <h1>历史比赛情报</h1>
            <p>严格赛事范围内的可审计评分、局势分类与阵容模型</p>
          </div>
        </div>
        <Button
          appearance="subtle"
          aria-label="刷新历史情报"
          icon={<ArrowClockwise size={16} />}
          onClick={retryAll}
        >
          刷新
        </Button>
      </header>

      <div className="intel-mode-bar">
        <TabList
          aria-label="历史情报视图"
          selectedValue={mode}
          onTabSelect={(_, data) => changeMode(data.value as IntelligenceMode)}
          size="small"
        >
          <Tab icon={<Sword size={17} />} value="matches">比赛复盘</Tab>
          <Tab icon={<Medal size={17} />} value="players">选手评分</Tab>
          <Tab icon={<Users size={17} />} value="teams">球队画像</Tab>
          <Tab icon={<ChartBar size={17} />} value="drafts">阵容校准</Tab>
        </TabList>
      </div>

      {mode === "drafts" && (
        <OverviewPanel
          error={overviewError}
          loading={overviewLoading}
          onRetry={() => setOverviewRefresh((value) => value + 1)}
          overview={overview}
        />
      )}

      {mode === "matches" && (
        <MatchesView
          detail={matchDetail}
          detailError={detailError}
          detailLoading={detailLoading}
          error={matchError}
          label={matchLabel}
          loading={matchLoading}
          onLabelChange={(value) => {
            setMatchLabel(value);
            setMatchPageNumber(1);
          }}
          onPageChange={setMatchPageNumber}
          onSearchDraftChange={setMatchSearchDraft}
          onSearchSubmit={submitMatchSearch}
          onBack={showMatchList}
          onSelect={selectMatch}
          page={matchPage}
          searchDraft={matchSearchDraft}
          selectedId={selectedMatchId}
        />
      )}

      {mode === "players" && (
        <PlayersView
          error={playerError}
          loading={playerLoading}
          onPageChange={setPlayerPageNumber}
          onPositionChange={(value) => {
            setPlayerPosition(value);
            setPlayerPageNumber(1);
          }}
          onSearchDraftChange={setPlayerSearchDraft}
          onSearchSubmit={submitPlayerSearch}
          page={playerPage}
          position={playerPosition}
          searchDraft={playerSearchDraft}
        />
      )}

      {mode === "teams" && (
        <TeamsView
          error={teamError}
          loading={teamLoading}
          onPageChange={setTeamPageNumber}
          onSearchChange={(value) => {
            setTeamSearch(value);
            setTeamPageNumber(1);
          }}
          pagination={teamPagination}
          search={teamSearch}
          teams={visibleTeams}
        />
      )}
    </section>
  );
}

function MatchesView({
  detail,
  detailError,
  detailLoading,
  error,
  label,
  loading,
  onLabelChange,
  onBack,
  onPageChange,
  onSearchDraftChange,
  onSearchSubmit,
  onSelect,
  page,
  searchDraft,
  selectedId,
}: {
  detail: IntelligenceMatchDetail | null;
  detailError: string | null;
  detailLoading: boolean;
  error: string | null;
  label: string;
  loading: boolean;
  onLabelChange: (value: string) => void;
  onBack: () => void;
  onPageChange: (value: number) => void;
  onSearchDraftChange: (value: string) => void;
  onSearchSubmit: (event: FormEvent) => void;
  onSelect: (matchId: number) => void;
  page: IntelligenceMatchPage | null;
  searchDraft: string;
  selectedId: number | null;
}) {
  return (
    <div className="intel-match-layout">
      {selectedId == null ? <main className="intel-match-list intel-match-list-page" aria-label="OpenDota 比赛列表">
        <form className="intel-filter-row" onSubmit={onSearchSubmit}>
          <Input
            aria-label="搜索历史比赛"
            contentBefore={<MagnifyingGlass size={15} />}
            onChange={(_, data) => onSearchDraftChange(data.value)}
            placeholder="比赛 ID、队伍或赛事"
            value={searchDraft}
          />
          <Button appearance="secondary" type="submit">搜索</Button>
          <Select
            aria-label="局势分类筛选"
            onChange={(_, data) => onLabelChange(data.value)}
            value={label}
          >
            <option value="">全部局势</option>
            {(Object.entries(STATE_LABELS) as [IntelligenceStateLabel, string][]).map(([value, name]) => (
              <option key={value} value={value}>{name}</option>
            ))}
          </Select>
        </form>

        <div className="intel-match-columns" aria-hidden="true">
          <span>赛事与时间</span>
          <span>对阵与结果</span>
          <span>局势分类</span>
          <span />
        </div>

        {error && !page ? <ErrorState message={error} /> : null}
        {error && page ? <div className="intel-stale-note">比赛列表刷新失败，当前显示上一次成功结果</div> : null}
        {loading && !page ? <ListSkeleton /> : null}
        {!loading && page && !page.data.length ? (
          <EmptyState message="没有符合条件的历史比赛" />
        ) : null}
        {page?.data.length ? (
          <div className={loading ? "intel-match-rows refreshing" : "intel-match-rows"}>
            {page.data.map((match) => (
              <MatchListRow
                key={match.match_id}
                match={match}
                onSelect={onSelect}
              />
            ))}
          </div>
        ) : null}
        {page && <PaginationControls pagination={page.pagination} onPageChange={onPageChange} />}
      </main> : (
        <div className="intel-match-detail-view">
          <div className="intel-detail-toolbar">
            <button
              aria-label="返回 OpenDota 比赛列表"
              className="intel-detail-back"
              onClick={onBack}
              type="button"
            >
              <CaretLeft size={17} weight="bold" aria-hidden="true" />
              <span>OpenDota 比赛列表</span>
            </button>
            <span>比赛 #{selectedId}</span>
          </div>
          <MatchDetailPanel
            detail={detail}
            error={detailError}
            loading={detailLoading}
            selectedId={selectedId}
          />
        </div>
      )}
    </div>
  );
}

function MatchListRow({
  match,
  onSelect,
}: {
  match: IntelligenceMatchSummary;
  onSelect: (matchId: number) => void;
}) {
  const radiant = teamName(match.radiant_team_name, match.radiant_team_id, "天辉");
  const dire = teamName(match.dire_team_name, match.dire_team_id, "夜魇");
  const resultKnown = match.radiant_win !== null;
  const winner = match.radiant_win === true ? radiant : match.radiant_win === false ? dire : null;
  return (
    <button
      aria-label={`查看 ${radiant} 对 ${dire} 的比赛复盘详情${winner ? `，${winner} 获胜` : ""}`}
      className="intel-match-row"
      onClick={() => onSelect(match.match_id)}
      type="button"
    >
      <span className="intel-row-meta">
        <span>{match.league_name || `联赛 ${match.leagueid || "未知"}`}</span>
        <code>{formatUnixTime(match.start_time)}</code>
      </span>
      <span className="intel-row-teams">
        <strong className={resultKnown ? (match.radiant_win ? "winner" : "loser") : ""}>
          <span className="intel-team-name">{radiant}</span>
          {resultKnown && <span className={`intel-result-badge ${match.radiant_win ? "win" : "loss"}`}>{match.radiant_win ? "胜" : "负"}</span>}
        </strong>
        <span className="intel-kill-score">击杀比分 {match.radiant_score ?? "-"} : {match.dire_score ?? "-"}</span>
        <strong className={resultKnown ? (match.radiant_win === false ? "winner" : "loser") : ""}>
          <span className="intel-team-name">{dire}</span>
          {resultKnown && <span className={`intel-result-badge ${match.radiant_win === false ? "win" : "loss"}`}>{match.radiant_win === false ? "胜" : "负"}</span>}
        </strong>
      </span>
      <span className="intel-row-states">
        <StateTag state={match.radiant_state} />
        <StateTag state={match.dire_state} />
        <code>#{match.match_id}</code>
      </span>
      <CaretRight className="intel-row-enter" size={17} aria-hidden="true" />
    </button>
  );
}

function MatchDetailPanel({
  detail,
  error,
  loading,
  selectedId,
}: {
  detail: IntelligenceMatchDetail | null;
  error: string | null;
  loading: boolean;
  selectedId: number | null;
}) {
  if (selectedId == null) {
    return <EmptyState large message="从左侧选择一场比赛查看完整复盘证据" />;
  }
  if (loading && (!detail || normalizedDetail(detail).match.match_id !== selectedId)) {
    return <DetailSkeleton />;
  }
  if (error && (!detail || normalizedDetail(detail).match.match_id !== selectedId)) {
    return <ErrorState message={error} />;
  }
  if (!detail) return <EmptyState large message="这场比赛暂无复盘详情" />;

  const value = normalizedDetail(detail);
  if (value.match.match_id !== selectedId) return <DetailSkeleton />;
  const match = value.match;
  const hasPlayerScores = value.playerScores.length > 0;
  const radiant = teamName(match.radiant_team_name, match.radiant_team_id, "天辉");
  const dire = teamName(match.dire_team_name, match.dire_team_id, "夜魇");
  const scoreEvidence = collectScoreEvidence(value.playerScores);

  return (
    <main className="intel-match-detail" aria-live="polite">
      {error && <div className="intel-stale-note">详情刷新失败，当前显示上一次成功结果</div>}
      <header className="intel-detail-header">
        <div>
          <span>{match.league_name || `联赛 ${match.leagueid || "未知"}`}</span>
          <h2>
            <strong className={match.radiant_win == null ? "" : match.radiant_win ? "winner" : "loser"}>
              <span>{radiant}</span>
              {match.radiant_win != null && <span className={`intel-result-badge ${match.radiant_win ? "win" : "loss"}`}>{match.radiant_win ? "胜" : "负"}</span>}
            </strong>
            <small>击杀比分 {match.radiant_score ?? "-"} : {match.dire_score ?? "-"}</small>
            <strong className={match.radiant_win == null ? "" : match.radiant_win === false ? "winner" : "loser"}>
              <span>{dire}</span>
              {match.radiant_win != null && <span className={`intel-result-badge ${match.radiant_win === false ? "win" : "loss"}`}>{match.radiant_win === false ? "胜" : "负"}</span>}
            </strong>
          </h2>
          <p>
            比赛 #{match.match_id}　{formatUnixTime(match.start_time)}　时长 {formatDuration(match.duration)}
          </p>
        </div>
      </header>

      <ScoreEvidenceStrip
        benchmarkCutoffs={scoreEvidence.benchmarkCutoffs}
        scoreVersions={scoreEvidence.scoreVersions}
        source="本场逐局评分"
      />

      <MatchRatingPanel
        rating={value.matchRating}
        radiant={radiant}
        dire={dire}
      />

      <RoshScorePanel
        dire={dire}
        radiant={radiant}
        score={value.roshLineupScore}
      />

      <section className="intel-detail-section">
        <div className="intel-section-heading compact">
          <div>
            <h3>比赛局势分类</h3>
            <p>同一场比赛按天辉和夜魇两个视角分别分类</p>
          </div>
          <ChartBar size={19} aria-hidden="true" />
        </div>
        <div className="intel-state-pair">
          <StateDetail team={radiant} state={value.radiantState} />
          <StateDetail team={dire} state={value.direState} />
        </div>
      </section>

      <section className="intel-detail-section">
        <div className="intel-section-heading compact">
          <div>
            <h3>{hasPlayerScores ? "选手逐局评分" : "选手赛后表现"}</h3>
            <p>{hasPlayerScores ? "执行分与赛果修正分均为 0 到 100 分" : "评分证据尚未就绪，先显示独立归档的 OpenDota 赛后表现"}</p>
          </div>
          <Medal size={19} aria-hidden="true" />
        </div>
        <PlayerScoreTable
          scores={value.playerScores}
          performance={value.playerPerformance}
        />
      </section>

      <section className="intel-detail-section">
        <div className="intel-section-heading compact">
          <div>
            <h3>阵容胜率预测</h3>
            <p>模型概率独立于 Rosh 纯阵容分与选手修正分</p>
          </div>
          <Sword size={19} aria-hidden="true" />
        </div>
        <DraftPredictionTable predictions={value.draftPredictions} />
      </section>
    </main>
  );
}



function ScoreEvidenceStrip({
  benchmarkCutoffs,
  missingCutoffNote = "评分行未提供基准截止",
  scoreVersions,
  source,
}: {
  benchmarkCutoffs: string[];
  missingCutoffNote?: string;
  scoreVersions: string[];
  source: string;
}) {
  const cutoffSummary = summarizeCutoffs(benchmarkCutoffs);
  return (
    <dl className="intel-score-evidence" aria-label={`${source}版本证据`}>
      <div>
        <dt>评分版本</dt>
        <dd>
          {scoreVersions.length
            ? scoreVersions.map((value) => <code key={value}>{value}</code>)
            : "尚未生成"}
        </dd>
      </div>
      <div>
        <dt>基准截止</dt>
        <dd>
          {cutoffSummary
            ? (
              <span className="intel-cutoff-summary">
                <code title={cutoffSummary.first}>{formatCutoff(cutoffSummary.first)}</code>
                {cutoffSummary.last !== cutoffSummary.first && (
                  <><span aria-hidden="true">至</span><code title={cutoffSummary.last}>{formatCutoff(cutoffSummary.last)}</code></>
                )}
                {cutoffSummary.count > 1 && <small>{cutoffSummary.count} 个截止点</small>}
              </span>
            )
            : missingCutoffNote}
        </dd>
      </div>
    </dl>
  );
}

function StateDetail({ team, state }: { team: string; state: IntelligenceTeamState | null }) {
  if (!state) {
    return (
      <article className="intel-state-detail empty">
        <strong>{team}</strong>
        <span>暂无局势标签</span>
      </article>
    );
  }
  return (
    <article className={`intel-state-detail state-${state.label}`}>
      <header>
        <strong>{team}</strong>
        <StateTag state={state} />
      </header>
      <dl>
        <div><dt>最大优势</dt><dd>{gold(state.max_lead)}</dd></div>
        <div><dt>最大劣势</dt><dd>{gold(state.max_deficit)}</dd></div>
        <div><dt>领先占比</dt><dd>{percent(state.ahead_fraction)}</dd></div>
        <div><dt>落后占比</dt><dd>{percent(state.behind_fraction)}</dd></div>
        <div><dt>终结耗时</dt><dd>{formatDuration(state.closeout_seconds)}</dd></div>
        <div><dt>曲线覆盖</dt><dd>{percent(state.curve_coverage)}</dd></div>
      </dl>
      <code>{state.label_version}</code>
    </article>
  );
}

function PlayerScoreTable({
  scores,
  performance,
}: {
  scores: IntelligencePlayerMapScore[];
  performance: IntelligencePlayerPerformance[];
}) {
  const rows = mergePlayerRows(scores, performance);
  if (!rows.length) return <EmptyState compact message="本场暂无选手评分或赛后表现" />;
  return (
    <div className="intel-table-scroll">
      <table className="intel-table intel-player-score-table">
        <thead>
          <tr>
            <th>阵营</th>
            <th>选手</th>
            <th>英雄</th>
            <th>位置</th>
            <th>OpenDota 赛后表现</th>
            <th>执行分</th>
            <th>赛果修正</th>
            <th>覆盖率</th>
            <th>角色可信度</th>
            <th>排名资格</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const score = "execution_score" in row ? row : null;
            return (
            <tr key={`${row.player_slot}-${row.account_id ?? "unknown"}`}>
              <td>{sideLabel(row.side)}</td>
              <td>
                <strong>{row.player_name || `账号 ${row.account_id ?? "未知"}`}</strong>
                <small className="intel-cell-note">槽位 {row.player_slot}</small>
              </td>
              <td>{row.hero_name || `英雄 ${row.hero_id ?? "未知"}`}</td>
              <td className="intel-number">{score?.position || "-"}</td>
              <td>
                {row.performance ? (
                  <>
                    <strong>
                      K/D/A {metric(row.performance.kills)} / {metric(row.performance.deaths)} / {metric(row.performance.assists)}
                    </strong>
                    <small className="intel-cell-note">
                      GPM/XPM {metric(row.performance.gold_per_min)} / {metric(row.performance.xp_per_min)}
                      　补/反 {metric(row.performance.last_hits)} / {metric(row.performance.denies)}
                    </small>
                    <small className="intel-cell-note">
                      净值 {metric(row.performance.net_worth)}　英雄/建筑伤害 {metric(row.performance.hero_damage)} / {metric(row.performance.tower_damage)}
                    </small>
                  </>
                ) : "-"}
              </td>
              <td className="intel-number score-primary">{score ? decimal(score.execution_score, 1) : "-"}</td>
              <td className="intel-number">{score ? decimal(score.result_adjusted_score, 1) : "-"}</td>
              <td className="intel-number">{score ? percent(score.coverage) : "-"}</td>
              <td className="intel-number">{score ? percent(score.role_confidence) : "-"}</td>
              <td>{score ? (score.ranking_eligible ? "可排名" : "证据不足") : "评分待处理"}</td>
            </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function mergePlayerRows(
  scores: IntelligencePlayerMapScore[],
  performance: IntelligencePlayerPerformance[],
): (IntelligencePlayerMapScore | IntelligencePlayerPerformance)[] {
  const scoresBySlot = new Map(scores.map((row) => [row.player_slot, row]));
  const performanceBySlot = new Map(performance.map((row) => [row.player_slot, row]));
  const slots = Array.from(new Set([
    ...scoresBySlot.keys(),
    ...performanceBySlot.keys(),
  ])).sort((left, right) => left - right);

  return slots.map((slot) => {
    const score = scoresBySlot.get(slot);
    const archived = performanceBySlot.get(slot);
    if (!score) return archived!;
    if (!archived) return score;
    return {
      ...archived,
      ...score,
      account_id: score.account_id ?? archived.account_id,
      player_name: score.player_name ?? archived.player_name,
      team_id: score.team_id ?? archived.team_id,
      side: score.side ?? archived.side,
      hero_id: score.hero_id ?? archived.hero_id,
      hero_name: score.hero_name ?? archived.hero_name,
      performance: score.performance ?? archived.performance,
    };
  });
}

function DraftPredictionTable({ predictions }: { predictions: IntelligenceDraftPrediction[] }) {
  if (!predictions.length) return <EmptyState compact message="本场暂无当前版本的阵容预测" />;
  return (
    <div className="intel-table-scroll">
      <table className="intel-table">
        <thead>
          <tr>
            <th>模型</th>
            <th>时点</th>
            <th>数据模式</th>
            <th>天辉胜率</th>
            <th>不确定度</th>
            <th>支持样本</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          {predictions.map((prediction) => (
            <tr key={`${prediction.model_version}-${prediction.model_kind}-${prediction.horizon_minutes}-${prediction.availability_mode}`}>
              <td>{modelKindLabel(prediction.model_kind)}</td>
              <td className="intel-number">{prediction.horizon_minutes} 分钟</td>
              <td>
                <span className={prediction.availability_mode === "prospective" ? "intel-mode-tag prospective" : "intel-mode-tag"}>
                  {availabilityLabel(prediction.availability_mode)}
                </span>
              </td>
              <td className="intel-number score-primary">{percent(prediction.probability)}</td>
              <td className="intel-number">{decimal(prediction.uncertainty, 3)}</td>
              <td className="intel-number">{prediction.support}</td>
              <td>{predictionStatusLabel(prediction.status)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PlayersView({
  error,
  loading,
  onPageChange,
  onPositionChange,
  onSearchDraftChange,
  onSearchSubmit,
  page,
  position,
  searchDraft,
}: {
  error: string | null;
  loading: boolean;
  onPageChange: (value: number) => void;
  onPositionChange: (value: string) => void;
  onSearchDraftChange: (value: string) => void;
  onSearchSubmit: (event: FormEvent) => void;
  page: IntelligencePlayerPage | null;
  position: string;
  searchDraft: string;
}) {
  const scoreEvidence = collectRankingEvidence(page?.data || []);
  return (
    <section className="intel-view-panel">
      <div className="intel-view-heading">
        <div>
          <h2>选手评分排名</h2>
          <p>仅统计当前评分版本中证据完整、具备排名资格的逐局结果</p>
        </div>
        <form className="intel-filter-row" onSubmit={onSearchSubmit}>
          <Input
            aria-label="搜索选手"
            contentBefore={<MagnifyingGlass size={15} />}
            onChange={(_, data) => onSearchDraftChange(data.value)}
            placeholder="选手名或账号 ID"
            value={searchDraft}
          />
          <Button appearance="secondary" type="submit">搜索</Button>
          <Select
            aria-label="位置筛选"
            onChange={(_, data) => onPositionChange(data.value)}
            value={position}
          >
            <option value="">全部位置</option>
            {[1, 2, 3, 4, 5].map((value) => <option key={value} value={value}>位置 {value}</option>)}
          </Select>
        </form>
      </div>
      <ScoreEvidenceStrip
        benchmarkCutoffs={scoreEvidence.benchmarkCutoffs}
        scoreVersions={scoreEvidence.scoreVersions}
        source="当前聚合排名"
        missingCutoffNote="聚合接口未返回逐局基准截止"
      />
      {error && !page ? <ErrorState message={error} /> : null}
      {error && page ? <div className="intel-stale-note">选手排名刷新失败，当前显示上一次成功结果</div> : null}
      {loading && !page ? <TableSkeleton /> : null}
      {!loading && page && !page.data.length ? <EmptyState message="没有符合条件的选手评分" /> : null}
      {page?.data.length ? (
        <div className={loading ? "intel-table-scroll refreshing" : "intel-table-scroll"}>
          <table className="intel-table intel-ranking-table">
            <thead>
              <tr>
                <th>排名</th>
                <th>选手</th>
                <th>主位置</th>
                <th>地图数</th>
                <th>平均执行分</th>
                <th>平均赛果修正</th>
                <th>数据覆盖</th>
                <th>角色可信度</th>
              </tr>
            </thead>
            <tbody>
              {page.data.map((player) => (
                <tr key={`${player.account_id}-${player.position}`}>
                  <td className="intel-number rank">{player.rank}</td>
                  <td>
                    <strong>{player.player_name || `账号 ${player.account_id}`}</strong>
                    <small className="intel-cell-note">{player.account_id}</small>
                  </td>
                  <td className="intel-number">{player.position}</td>
                  <td className="intel-number">{player.map_count}</td>
                  <td className="intel-number score-primary">{decimal(player.average_execution_score, 1)}</td>
                  <td className="intel-number">{decimal(player.average_result_adjusted_score, 1)}</td>
                  <td className="intel-number">{percent(player.average_coverage)}</td>
                  <td className="intel-number">{percent(player.average_role_confidence)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {page && <PaginationControls pagination={page.pagination} onPageChange={onPageChange} />}
    </section>
  );
}

function TeamsView({
  error,
  loading,
  onPageChange,
  onSearchChange,
  pagination,
  search,
  teams,
}: {
  error: string | null;
  loading: boolean;
  onPageChange: (value: number) => void;
  onSearchChange: (value: string) => void;
  pagination: IntelligencePagination;
  search: string;
  teams: IntelligenceTeamProfile[];
}) {
  return (
    <section className="intel-view-panel">
      <div className="intel-view-heading">
        <div>
          <h2>球队长期风格画像</h2>
          <p>画像按截止时间冻结，样本权重与局势机会数保留版本证据</p>
        </div>
        <Input
          aria-label="搜索球队"
          contentBefore={<MagnifyingGlass size={15} />}
          onChange={(_, data) => onSearchChange(data.value)}
          placeholder="球队名、标签或 ID"
          value={search}
        />
      </div>
      {error && !teams.length ? <ErrorState message={error} /> : null}
      {error && teams.length ? <div className="intel-stale-note">球队画像刷新失败，当前显示上一次成功结果</div> : null}
      {loading && !teams.length ? <ListSkeleton /> : null}
      {!error && !loading && !teams.length ? <EmptyState message="没有符合条件的球队画像" /> : null}
      {teams.length ? (
        <div className={loading ? "intel-team-list refreshing" : "intel-team-list"}>
          {teams.map((team) => <TeamProfileRow key={team.team_id} team={team} />)}
        </div>
      ) : null}
      <PaginationControls pagination={pagination} onPageChange={onPageChange} />
    </section>
  );
}

function TeamProfileRow({ team }: { team: IntelligenceTeamProfile }) {
  const isLowSample = team.effective_sample_size < TEAM_PROFILE_STABLE_SAMPLE_THRESHOLD;
  const selectedRates = Object.keys(RATE_LABELS).map((metric) => ({
    metric,
    value: posteriorRate(team, metric),
  }));
  return (
    <article className="intel-team-row">
      <header>
        {team.logo_url ? <img alt="" src={team.logo_url} /> : <span className="intel-team-mark"><Users size={18} /></span>}
        <div>
          <h3>{team.team_name || `球队 ${team.team_id}`}</h3>
          <p>{team.team_tag || "无标签"}　ID {team.team_id}</p>
        </div>
        <dl>
          <div><dt>有效样本量</dt><dd>{decimal(team.effective_sample_size, 1)}</dd></div>
          <div><dt>画像截止</dt><dd>{formatIsoDate(team.profile_cutoff)}</dd></div>
        </dl>
      </header>
      {isLowSample && (
        <div className="intel-team-sample-warning" role="status">
          <WarningCircle size={14} weight="fill" aria-hidden="true" />
          低样本，画像不稳定（有效样本量 &lt; {TEAM_PROFILE_STABLE_SAMPLE_THRESHOLD}）
        </div>
      )}
      <div className="intel-team-rates">
        {selectedRates.map(({ metric, value }) => (
          <div key={metric}>
            <span>{RATE_LABELS[metric]}</span>
            <strong>{value == null ? "-" : percent(value.mean)}</strong>
            <small>{value ? `${value.opportunities} 次机会` : "无机会样本"}</small>
          </div>
        ))}
      </div>
      <div className="intel-team-state-counts">
        {(Object.keys(STATE_LABELS) as IntelligenceStateLabel[]).map((label) => (
          <span key={label}>{STATE_LABELS[label]} <strong>{team.state_counts[label] || 0}</strong></span>
        ))}
      </div>
      <code>{team.profile_version}</code>
    </article>
  );
}

function StateTag({ state }: { state?: IntelligenceTeamState | null }) {
  if (!state) return <span className="intel-state-tag missing">无标签</span>;
  return <span className={`intel-state-tag state-${state.label}`}>{STATE_LABELS[state.label]}</span>;
}

function StatusTag({ children, ok }: { children: ReactNode; ok: boolean }) {
  return (
    <span className={ok ? "intel-status-tag ok" : "intel-status-tag warning"}>
      {ok ? <CheckCircle size={14} weight="fill" /> : <WarningCircle size={14} weight="fill" />}
      {children}
    </span>
  );
}

function PaginationControls({
  onPageChange,
  pagination,
}: {
  onPageChange: (value: number) => void;
  pagination: IntelligencePagination;
}) {
  if (pagination.total_pages <= 1) return null;
  return (
    <nav className="intel-pagination" aria-label="分页">
      <Button
        appearance="subtle"
        aria-label="上一页"
        disabled={pagination.page <= 1}
        icon={<CaretLeft size={15} />}
        onClick={() => onPageChange(pagination.page - 1)}
      />
      <span>第 {pagination.page} / {pagination.total_pages} 页，共 {pagination.total} 条</span>
      <Button
        appearance="subtle"
        aria-label="下一页"
        disabled={pagination.page >= pagination.total_pages}
        icon={<CaretRight size={15} />}
        onClick={() => onPageChange(pagination.page + 1)}
      />
    </nav>
  );
}

function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="intel-error-state" role="alert">
      <WarningCircle size={22} weight="fill" aria-hidden="true" />
      <div><strong>历史情报读取失败</strong><span>{message}</span></div>
      {onRetry && <Button appearance="secondary" onClick={onRetry}>重试</Button>}
    </div>
  );
}

function EmptyState({
  compact = false,
  large = false,
  message,
}: {
  compact?: boolean;
  large?: boolean;
  message: string;
}) {
  const classes = ["intel-empty-state", compact ? "compact" : "", large ? "large" : ""]
    .filter(Boolean)
    .join(" ");
  return (
    <div className={classes}>
      <Database size={compact ? 18 : 26} aria-hidden="true" />
      <span>{message}</span>
    </div>
  );
}

function OverviewSkeleton() {
  return (
    <Skeleton className="intel-overview-skeleton" aria-label="正在加载历史情报总览">
      <SkeletonItem shape="rectangle" />
      <div><SkeletonItem shape="rectangle" /><SkeletonItem shape="rectangle" /></div>
      <SkeletonItem shape="rectangle" />
    </Skeleton>
  );
}

function ListSkeleton() {
  return (
    <Skeleton className="intel-list-skeleton" aria-label="正在加载列表">
      {[0, 1, 2, 3].map((value) => <SkeletonItem key={value} shape="rectangle" />)}
    </Skeleton>
  );
}

function TableSkeleton() {
  return (
    <Skeleton className="intel-table-skeleton" aria-label="正在加载表格">
      <SkeletonItem shape="rectangle" />
      <SkeletonItem shape="rectangle" />
    </Skeleton>
  );
}

function DetailSkeleton() {
  return (
    <Skeleton className="intel-detail-skeleton" aria-label="正在加载比赛复盘详情">
      <SkeletonItem shape="rectangle" />
      <SkeletonItem shape="rectangle" />
      <SkeletonItem shape="rectangle" />
    </Skeleton>
  );
}

function normalizedDetail(detail: IntelligenceMatchDetail) {
  if ("match" in detail) {
    return {
      match: detail.match,
      radiantState: detail.radiant_state,
      direState: detail.dire_state,
      playerPerformance: detail.player_performance || [],
      playerScores: detail.player_scores,
      matchRating: detail.match_rating,
      roshLineupScore: detail.rosh_lineup_score ?? null,
      draftPredictions: detail.draft_predictions,
    };
  }
  return {
    match: detail,
    radiantState: detail.states?.radiant ?? detail.radiant_state ?? null,
    direState: detail.states?.dire ?? detail.dire_state ?? null,
    playerPerformance: detail.player_performance || [],
    playerScores: detail.player_scores,
    matchRating: detail.match_rating,
    roshLineupScore: detail.rosh_lineup_score ?? null,
    draftPredictions: detail.draft_predictions,
  };
}

function qualitySlices(overview: IntelligenceOverview): IntelligenceDraftQualitySlice[] {
  if (overview.draft_quality_slices) return overview.draft_quality_slices;
  if (Array.isArray(overview.draft_quality)) return overview.draft_quality;
  return overview.draft_quality?.slices || [];
}

function availabilityStatus(
  overview: IntelligenceOverview,
  slices: IntelligenceDraftQualitySlice[],
  mode: IntelligenceAvailabilityMode,
): boolean {
  if (overview.availability?.[mode] != null) return Boolean(overview.availability[mode]);
  if (overview.draft_quality && !Array.isArray(overview.draft_quality)) {
    const status = overview.draft_quality.availability?.[mode];
    if (status != null) return Boolean(status);
  }
  return slices.some((item) => item.availability_mode === mode && item.availability_status === "available");
}

function posteriorRate(team: IntelligenceTeamProfile, metric: string): {
  mean: number;
  opportunities: number;
} | null {
  if (!Array.isArray(team.posterior_rates)) return null;
  const value = team.posterior_rates.find((item) => (
    typeof item === "object" && item !== null && (item as Record<string, unknown>).metric === metric
  ));
  if (!value || typeof value !== "object") return null;
  const row = value as Record<string, unknown>;
  return typeof row.mean === "number" && typeof row.opportunities === "number"
    ? { mean: row.mean, opportunities: row.opportunities }
    : null;
}

function clientPagination(page: number, pageSize: number, total: number): IntelligencePagination {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  return { page: Math.min(page, totalPages), page_size: pageSize, total, total_pages: totalPages };
}

function collectScoreEvidence(scores: IntelligencePlayerMapScore[]): {
  scoreVersions: string[];
  benchmarkCutoffs: string[];
} {
  return {
    scoreVersions: uniqueNonEmpty(scores.map((score) => score.score_version)),
    benchmarkCutoffs: uniqueNonEmpty(scores.map((score) => score.benchmark_cutoff)),
  };
}

function collectRankingEvidence(rows: IntelligencePlayerRanking[]): {
  scoreVersions: string[];
  benchmarkCutoffs: string[];
} {
  return {
    scoreVersions: uniqueNonEmpty(rows.map((row) => row.score_version)),
    benchmarkCutoffs: uniqueNonEmpty([
      ...rows.flatMap((row) => row.benchmark_cutoffs || []),
      ...rows.map((row) => row.benchmark_cutoff),
      ...rows.map((row) => row.benchmark_cutoff_min),
      ...rows.map((row) => row.benchmark_cutoff_max),
    ]),
  };
}

function uniqueNonEmpty(values: Array<string | null | undefined>): string[] {
  return Array.from(new Set(values.filter((value): value is string => Boolean(value))));
}

function formatCutoff(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : value.slice(0, 10);
}

function versionLabel(name: string): string {
  return {
    player_score: "选手评分",
    team_state: "局势标签",
    team_profile: "球队画像",
    draft_score: "阵容输入",
    draft_model: "阵容模型",
    draft_backtest: "阵容回测",
    draft_features: "阵容特征",
  }[name] || name;
}

function modelKindLabel(value: string): string {
  return value === "pure_draft" ? "纯阵容" : value === "context_adjusted" ? "上下文修正" : value;
}

function availabilityLabel(value: IntelligenceAvailabilityMode): string {
  return value === "prospective" ? "真实前瞻" : "历史重建";
}

function qualityStatusLabel(value: IntelligenceDraftQualitySlice["status"]): string {
  return {
    passed: "通过",
    failed: "未通过",
    unsupported: "样本不足",
    missing: "无数据",
    provisional: "暂定",
  }[value];
}

function gateFailureLabel(value: string): string {
  return {
    support_below_100: "样本少于 100",
    "brier_not_below_0.25": "Brier 未低于 0.25",
    log_loss_not_below_ln2: "Log loss 未低于 ln2",
    "ece_above_0.10": "ECE 高于 0.10",
    "ece_upper_bound_above_0.15": "ECE 90% 上界高于 0.15",
    ece_upper_bound_missing: "ECE 90% 上界缺失",
    calibration_bins_not_valid_five_bin_ece: "ECE 无法形成五个有效分箱",
    prospective_data_missing: "前瞻数据尚未建立",
    reconstructed_data_missing: "历史重建数据缺失",
  }[value] || value;
}

function predictionStatusLabel(value: IntelligenceDraftPrediction["status"]): string {
  return {
    predicted: "已预测",
    settled: "已结算",
    insufficient_evidence: "证据不足",
  }[value];
}

function sideLabel(value: "radiant" | "dire" | null): string {
  return value === "radiant" ? "天辉" : value === "dire" ? "夜魇" : "未知";
}

function teamName(name: string | null, id: number | null, fallback: string): string {
  return name || (id ? `球队 ${id}` : fallback);
}

function formatUnixTime(value: number | null): string {
  if (!value) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value * 1000));
}

function formatIsoDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "未知";
  const minutes = Math.floor(seconds / 60);
  const rest = Math.floor(seconds % 60);
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

function decimal(value: number | null | undefined, digits: number): string {
  return value == null || !Number.isFinite(value) ? "-" : value.toFixed(digits);
}

function signedDecimal(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "-";
  return `${value > 0 ? "+" : ""}${value.toFixed(2)}`;
}

function percentagePoints(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "-" : `${value.toFixed(1)}%`;
}

function roshAdvantageLabel(value: number | null | undefined, radiant: string, dire: string): string {
  if (value == null || !Number.isFinite(value)) return "不可用";
  const magnitude = Math.abs(value).toFixed(2);
  if (value > 0) return `${radiant} 占优 ${magnitude}`;
  if (value < 0) return `${dire} 占优 ${magnitude}`;
  return `均势 ${magnitude}`;
}

function percent(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "-" : `${(value * 100).toFixed(1)}%`;
}

function integer(value: number): string {
  return new Intl.NumberFormat("zh-CN").format(value);
}

function metric(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "-" : integer(Math.round(value));
}

function gold(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "-";
  return `${value > 0 ? "+" : ""}${Math.round(value).toLocaleString("zh-CN")}`;
}

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}
