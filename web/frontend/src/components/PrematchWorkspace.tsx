import {
  Button,
  Dialog,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  Input,
  Select,
  Spinner,
} from "@fluentui/react-components";
import { AdvantageSparkline } from "./common/AdvantageSparkline";
import { PrematchPredictionView } from "./PrematchPredictionView";
import {
  ChartLineUp,
  MagicWand,
  MagnifyingGlass,
  ShieldCheck,
  X,
} from "@phosphor-icons/react";
import { useEffect, useMemo, useRef, useState } from "react";

import {
  createRoshAnalysis,
  fetchPrematchDraft,
  fetchPrematchHeroGrid,
  fetchPrematchLeagues,
  fetchPrematchRecentMatches,
  fetchRoshAnalysisRecords,
  fetchPrematchTeams,
} from "../api";
import type {
  PrematchDraft,
  PrematchDraftHero,
  PrematchHero,
  PrematchHeroGrid,
  PrematchLeague,
  PrematchRecentMatch,
  PrematchTeam,
  RoshAnalysisRunResponse,
} from "../types";

type Side = "radiant" | "dire";
type Attribute = keyof PrematchHeroGrid;

const EMPTY_LINEUP: Array<PrematchHero | null> = [null, null, null, null, null];
const POSITIONS = ["Carry", "Mid", "Offlane", "Soft Support", "Hard Support"];
const ATTRIBUTE_LABELS: Record<Attribute, string> = {
  str: "力量",
  agi: "敏捷",
  int: "智力",
  all: "全才",
};

export function PrematchWorkspace() {
  const [teams, setTeams] = useState<PrematchTeam[]>([]);
  const [leagues, setLeagues] = useState<PrematchLeague[]>([]);
  const [heroGrid, setHeroGrid] = useState<PrematchHeroGrid>({
    str: [], agi: [], int: [], all: [],
  });
  const [recentMatches, setRecentMatches] = useState<PrematchRecentMatch[]>([]);
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [catalogError, setCatalogError] = useState<string | null>(null);

  const [matchId, setMatchId] = useState("");
  const [radiantTeam, setRadiantTeam] = useState("");
  const [direTeam, setDireTeam] = useState("");
  const [league, setLeague] = useState("");
  const [radiantHeroes, setRadiantHeroes] = useState([...EMPTY_LINEUP]);
  const [direHeroes, setDireHeroes] = useState([...EMPTY_LINEUP]);
  const [sourceMatchId, setSourceMatchId] = useState<number | null>(null);
  const [sourceDateTime, setSourceDateTime] = useState<number | null>(null);
  const [sourceBusy, setSourceBusy] = useState(false);
  const [sourceStatus, setSourceStatus] = useState<string | null>(null);

  const [picker, setPicker] = useState<{ side: Side; position: number } | null>(null);
  const [attribute, setAttribute] = useState<Attribute>("str");
  const [heroSearch, setHeroSearch] = useState("");
  const heroSearchRef = useRef<HTMLInputElement>(null);
  const [prediction, setPrediction] = useState<RoshAnalysisRunResponse | null>(null);
  const [predicting, setPredicting] = useState(false);
  const [predictionError, setPredictionError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setCatalogLoading(true);
    Promise.all([
      fetchPrematchTeams(controller.signal),
      fetchPrematchLeagues(controller.signal),
      fetchPrematchHeroGrid(controller.signal),
      fetchPrematchRecentMatches(controller.signal),
    ]).then(([teamRows, leagueRows, heroes, matches]) => {
      setTeams(
        teamRows
          .filter((team) => team.match_count > 0)
          .sort((left, right) => right.match_count - left.match_count),
      );
      setLeagues(
        [...leagueRows].sort((left, right) =>
          (left.name || "").localeCompare(right.name || "", "zh-CN")),
      );
      setHeroGrid(heroes);
      setRecentMatches(matches);
      setCatalogError(null);
      setCatalogLoading(false);
    }).catch((reason: Error) => {
      if (reason.name === "AbortError") return;
      setCatalogError(reason.message || "无法加载赛前基础数据");
      setCatalogLoading(false);
    });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!picker) return undefined;
    const timer = window.setTimeout(() => heroSearchRef.current?.focus(), 0);
    return () => window.clearTimeout(timer);
  }, [picker]);

  const pickerHeroes = useMemo(() => {
    const query = heroSearch.trim().toLocaleLowerCase("zh-CN");
    return heroGrid[attribute].filter((hero) =>
      !query || hero.localized_name.toLocaleLowerCase("zh-CN").includes(query));
  }, [attribute, heroGrid, heroSearch]);
  const heroNames = useMemo(() => new Map(
    [
      ...Object.values(heroGrid).flat(),
      ...radiantHeroes.filter((hero): hero is PrematchHero => hero !== null),
      ...direHeroes.filter((hero): hero is PrematchHero => hero !== null),
    ].map((hero) => [hero.hero_id, hero.localized_name]),
  ), [direHeroes, heroGrid, radiantHeroes]);

  const clearSource = () => {
    setSourceMatchId(null);
    setSourceDateTime(null);
    setSourceStatus(null);
  };

  const setTeam = (side: Side, value: string) => {
    clearSource();
    if (side === "radiant") setRadiantTeam(value);
    else setDireTeam(value);
  };

  const closeHeroPicker = () => {
    setPicker(null);
    setHeroSearch("");
  };

  const setHero = (hero: PrematchHero) => {
    if (!picker) return;
    clearSource();
    const update = (current: Array<PrematchHero | null>) => {
      const next = [...current];
      next[picker.position] = hero;
      return next;
    };
    if (picker.side === "radiant") setRadiantHeroes(update);
    else setDireHeroes(update);
    closeHeroPicker();
  };

  const clearHero = (side: Side, position: number) => {
    clearSource();
    const update = (current: Array<PrematchHero | null>) => {
      const next = [...current];
      next[position] = null;
      return next;
    };
    if (side === "radiant") setRadiantHeroes(update);
    else setDireHeroes(update);
  };

  const applyDraft = (draft: PrematchDraft) => {
    const radiant = draft.radiant_heroes.map(draftHero);
    const dire = draft.dire_heroes.map(draftHero);
    setRadiantTeam(String(draft.radiant_team_id));
    setDireTeam(String(draft.dire_team_id));
    setLeague(draft.league_id ? String(draft.league_id) : "");
    setRadiantHeroes(paddedLineup(radiant));
    setDireHeroes(paddedLineup(dire));
    setSourceMatchId(
      radiant.length === 5 && dire.length === 5 ? draft.match_id : null,
    );
    setSourceDateTime(draft.end_time);
    setSourceStatus(`比赛 ${draft.match_id} 已载入，位置按 1-5 号位绑定`);
    setPrediction(null);
    setPredictionError(null);
  };

  const loadMatch = async (requested?: number) => {
    const parsed = requested || Number(matchId);
    if (!Number.isSafeInteger(parsed) || parsed <= 0) {
      setPredictionError("请输入有效的比赛 ID");
      return;
    }
    setMatchId(String(parsed));
    setSourceBusy(true);
    setPredictionError(null);
    try {
      applyDraft(await fetchPrematchDraft(parsed));
    } catch (reason) {
      setPredictionError(errorText(reason, "无法载入比赛阵容"));
    } finally {
      setSourceBusy(false);
    }
  };

  const submit = async () => {
    const radiantId = Number(radiantTeam);
    const direId = Number(direTeam);
    if (!radiantId || !direId) {
      setPredictionError("请选择 Radiant 与 Dire 队伍");
      return;
    }
    if (radiantId === direId) {
      setPredictionError("Radiant 与 Dire 不能是同一支队伍");
      return;
    }
    if (radiantHeroes.some((hero) => hero === null) || direHeroes.some((hero) => hero === null)) {
      setPredictionError("两边都需要完整的五名英雄");
      return;
    }
    const heroIds = [...radiantHeroes, ...direHeroes].map((hero) => hero!.hero_id);
    if (new Set(heroIds).size !== 10) {
      setPredictionError("两边阵容不能选择重复英雄");
      return;
    }

    setPredicting(true);
    setPredictionError(null);
    try {
      if (sourceMatchId && !sourceDateTime) {
        throw new Error("来源比赛缺少时间，无法建立官方 Rosh 请求身份");
      }
      const dateTime = sourceDateTime || Math.floor(Date.now() / 1_000);
      if (sourceMatchId) {
        const records = await fetchRoshAnalysisRecords(
          "opendota",
          String(sourceMatchId),
        );
        const existing = records.records.find(({ run }) => (
          run.status === "succeeded"
          && run.match_id === sourceMatchId
          && run.rosh_profile_id === "stratz-rosh-web-2026-07-28-v2"
        ));
        if (existing) {
          setPrediction(existing.run);
          return;
        }
      }
      const result = await createRoshAnalysis(sourceMatchId ? {
        mode: "historical_match",
        match_id: sourceMatchId,
        date_time: dateTime,
        bracket_ids: ["IMMORTAL"],
        rosh_profile_id: "stratz-rosh-web-2026-07-28-v2",
        match_links: [{
          source: "opendota",
          source_match_id: String(sourceMatchId),
        }],
      } : {
        mode: "explicit_draft",
        date_time: dateTime,
        bracket_ids: ["IMMORTAL"],
        rosh_profile_id: "stratz-rosh-web-2026-07-28-v2",
        radiant: radiantHeroes.map((hero, index) => ({
          hero_id: hero!.hero_id,
          position_id: index + 1,
        })),
        dire: direHeroes.map((hero, index) => ({
          hero_id: hero!.hero_id,
          position_id: index + 1,
        })),
      });
      setPrediction(result);
    } catch (reason) {
      setPredictionError(errorText(reason, "生成预测失败"));
    } finally {
      setPredicting(false);
    }
  };

  const reset = () => {
    setMatchId("");
    setRadiantTeam("");
    setDireTeam("");
    setLeague("");
    setRadiantHeroes([...EMPTY_LINEUP]);
    setDireHeroes([...EMPTY_LINEUP]);
    setSourceMatchId(null);
    setSourceDateTime(null);
    setSourceStatus(null);
    setPrediction(null);
    setPredictionError(null);
  };

  return (
    <main className="prematch-workspace">
      <header className="prematch-heading">
        <div>
          <span>STRATZ Rosh</span>
          <h1>赛前阵容分析</h1>
        </div>
        <div className="prematch-heading-state">
          <ShieldCheck size={18} aria-hidden="true" />
          <span>{sourceMatchId ? `可信比赛 ${sourceMatchId}` : "手工阵容"}</span>
          <strong>官方有符号分</strong>
        </div>
      </header>

      <PrematchPredictionView />

      <section className="prematch-source" aria-label="比赛来源">
        <Input
          aria-label="比赛 ID"
          contentBefore={<MagnifyingGlass size={16} aria-hidden="true" />}
          inputMode="numeric"
          placeholder="比赛 ID"
          value={matchId}
          onChange={(_, data) => setMatchId(data.value.replace(/\D/g, ""))}
        />
        <Button
          appearance="primary"
          disabled={sourceBusy}
          icon={<MagicWand size={17} />}
          onClick={() => void loadMatch()}
        >
          自动填充
        </Button>
        <Select
          aria-label="最近比赛"
          value=""
          onChange={(_, data) => {
            const value = Number(data.value);
            if (value) void loadMatch(value);
          }}
        >
          <option value="">最近比赛</option>
          {recentMatches.map((match) => (
            <option key={match.match_id} value={match.match_id}>
              {recentMatchLabel(match)}
            </option>
          ))}
        </Select>
        {sourceBusy && <Spinner size="tiny" label="处理中" />}
        {sourceStatus && <span className="prematch-source-status">{sourceStatus}</span>}
      </section>

      {catalogError && <div className="global-error" role="alert">{catalogError}</div>}
      {catalogLoading ? (
        <div className="view-loading" role="status"><Spinner label="正在加载赛前基础数据" /></div>
      ) : (
        <>
          <div className="prematch-lineups">
            <TeamLineup
              heroes={radiantHeroes}
              side="radiant"
              teamId={radiantTeam}
              teams={teams}
              onClearHero={clearHero}
              onOpenPicker={(position) => setPicker({ side: "radiant", position })}
              onTeamChange={(value) => setTeam("radiant", value)}
            />
            <div className="prematch-versus" aria-hidden="true">VS</div>
            <TeamLineup
              heroes={direHeroes}
              side="dire"
              teamId={direTeam}
              teams={teams}
              onClearHero={clearHero}
              onOpenPicker={(position) => setPicker({ side: "dire", position })}
              onTeamChange={(value) => setTeam("dire", value)}
            />
          </div>

          <section className="prematch-actions" aria-label="预测操作">
            <label>
              <span>联赛</span>
              <Select value={league} onChange={(_, data) => setLeague(data.value)}>
                <option value="">全部联赛</option>
                {leagues.map((item) => (
                  <option key={item.leagueid} value={item.leagueid}>
                    {item.name || `League ${item.leagueid}`} ({item.match_count} 场)
                  </option>
                ))}
              </Select>
            </label>
            <div>
              <Button onClick={reset}>重置</Button>
              <Button
                appearance="primary"
                disabled={predicting}
                icon={<ChartLineUp size={18} />}
                onClick={() => void submit()}
              >
                {predicting ? "正在计算" : "分析阵容"}
              </Button>
            </div>
          </section>
        </>
      )}

      {predictionError && (
        <div className="prematch-error" role="alert">{predictionError}</div>
      )}
      {prediction && <PredictionResult heroNames={heroNames} result={prediction} />}

      {picker && (
        <Dialog
          modalType="modal"
          open
          onOpenChange={(_, data) => {
            if (!data.open) closeHeroPicker();
          }}
        >
          <DialogSurface aria-label="英雄选择器" className="hero-picker-dialog">
            <DialogBody className="hero-picker-body">
              <DialogTitle
                action={(
                  <Button
                    appearance="subtle"
                    aria-label="关闭英雄选择器"
                    icon={<X size={18} />}
                    onClick={closeHeroPicker}
                  />
                )}
              >
                <span className="hero-picker-title">
                  <strong>{picker.side === "radiant" ? "Radiant" : "Dire"}</strong>
                  <small>{POSITIONS[picker.position]}</small>
                </span>
              </DialogTitle>
              <DialogContent className="hero-picker-content">
                <div className="hero-picker-controls">
                  <div role="tablist" aria-label="英雄属性">
                    {(Object.keys(ATTRIBUTE_LABELS) as Attribute[]).map((key) => (
                      <Button
                        key={key}
                        appearance={attribute === key ? "primary" : "subtle"}
                        aria-selected={attribute === key}
                        role="tab"
                        onClick={() => setAttribute(key)}
                      >
                        {ATTRIBUTE_LABELS[key]}
                      </Button>
                    ))}
                  </div>
                  <Input
                    aria-label="搜索英雄"
                    contentBefore={<MagnifyingGlass size={16} />}
                    placeholder="搜索英雄"
                    ref={heroSearchRef}
                    value={heroSearch}
                    onChange={(_, data) => setHeroSearch(data.value)}
                  />
                </div>
                <div className="hero-picker-grid">
                  {pickerHeroes.map((hero) => (
                    <button key={hero.hero_id} type="button" onClick={() => setHero(hero)}>
                      <HeroImage hero={hero} />
                      <span>{hero.localized_name}</span>
                    </button>
                  ))}
                  {pickerHeroes.length === 0 && <p>没有匹配的英雄</p>}
                </div>
              </DialogContent>
            </DialogBody>
          </DialogSurface>
        </Dialog>
      )}
    </main>
  );
}

function TeamLineup({
  heroes,
  onClearHero,
  onOpenPicker,
  onTeamChange,
  side,
  teamId,
  teams,
}: {
  heroes: Array<PrematchHero | null>;
  onClearHero: (side: Side, position: number) => void;
  onOpenPicker: (position: number) => void;
  onTeamChange: (value: string) => void;
  side: Side;
  teamId: string;
  teams: PrematchTeam[];
}) {
  const label = side === "radiant" ? "Radiant" : "Dire";
  return (
    <section className={`prematch-team ${side}`} aria-label={`${label} 阵容`}>
      <header>
        <h2>{label}</h2>
        <span>{heroes.filter(Boolean).length}/5</span>
      </header>
      <label className="prematch-team-select">
        <span>队伍</span>
        <Select value={teamId} onChange={(_, data) => onTeamChange(data.value)}>
          <option value="">选择队伍</option>
          {teams.map((team) => (
            <option key={team.team_id} value={team.team_id}>
              {team.name || "未命名队伍"} · #{team.team_id} ({team.match_count} 场)
            </option>
          ))}
        </Select>
      </label>
      <div className="prematch-hero-slots">
        {heroes.map((hero, position) => (
          <div className={hero ? "filled" : ""} key={position}>
            <button
              aria-label={`选择 ${label} ${position + 1} 号位英雄`}
              type="button"
              onClick={() => onOpenPicker(position)}
            >
              <span className="position-number">{position + 1}</span>
              {hero ? <HeroImage hero={hero} /> : <span className="hero-placeholder" />}
              <span className="hero-slot-copy">
                <strong>{hero?.localized_name || POSITIONS[position]}</strong>
                <small>{POSITIONS[position]}</small>
              </span>
            </button>
            {hero && (
              <Button
                appearance="subtle"
                aria-label={`清除 ${label} ${position + 1} 号位英雄`}
                icon={<X size={15} />}
                onClick={() => onClearHero(side, position)}
              />
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

export function PredictionResult({
  heroNames = new Map<number, string>(),
  result,
}: {
  heroNames?: ReadonlyMap<number, string>;
  result: RoshAnalysisRunResponse;
}) {
  const minutes = result.minute_points;
  const sparklinePoints = minutes.map((m) => ({
    minute: m.minute,
    win_rate_graph: m.display_score,
  }));
  const direction = scoreDirection(result.relative_advantage);
  const directionTitle = direction === "均势"
    ? "两边阵容接近均势"
    : `阵容更偏向 ${direction}`;

  return (
    <section className="prematch-result" aria-label="STRATZ Rosh 官方阵容分析" aria-live="polite">
      <header className="prematch-result-summary">
        <div className="prematch-result-copy">
          <span>官方 Rosh 阵容方向</span>
          <h2>{directionTitle}</h2>
          <p>这是 Rosh 阵容方向评分，不等同于比赛胜率。分差程度只描述当前相对分数。</p>
        </div>
        <div className={`prematch-result-score ${scoreTone(result.relative_advantage)}`}>
          <span>相对阵容分差</span>
          <strong>{signedNullable(result.relative_advantage)}</strong>
          <small>{result.status === "succeeded" ? "分析完整" : "分析失败"}</small>
        </div>
      </header>

      <dl className="prematch-result-insights">
        <div><dt>分差程度</dt><dd>{scoreStrength(result.relative_advantage)}</dd></div>
        <div><dt>主要优势阶段</dt><dd>{strongestMinuteLabel(minutes)}</dd></div>
        <div><dt>主要分差来源</dt><dd>{primaryScoreReason(result.hero_components)}</dd></div>
      </dl>

      {sparklinePoints.length > 0 && (
        <div style={{ marginTop: "12px" }}>
          <AdvantageSparkline
            points={sparklinePoints}
            radiantName="Radiant"
            direName="Dire"
            metricLabel="Rosh 阵容优势分"
            unit=""
          />
        </div>
      )}

      <details className="prematch-result-details">
        <summary>查看详细评分与证据</summary>
        <div className="prematch-result-details-content">
          <dl className="prematch-rosh-metrics">
            <div><dt>Radiant 队伍分</dt><dd>{signedNullable(result.radiant_team_score)}</dd></div>
            <div><dt>Dire 队伍分</dt><dd>{signedNullable(result.dire_team_score)}</dd></div>
            <div><dt>相对分差</dt><dd>{signedNullable(result.relative_advantage)}</dd></div>
            <div><dt>证据</dt><dd><code>{result.evidence_hash.slice(0, 12)}</code></dd></div>
          </dl>

          <div className="prematch-minute-table">
            <header><h2>英雄分解</h2><span>服务端官方 scorer 输出</span></header>
            <div>
              <table>
                <thead><tr><th>阵营 / 位置</th><th>英雄</th><th>基础</th><th>同队协同</th><th>对手克制</th><th>合计</th></tr></thead>
                <tbody>
                  {result.hero_components.map((row) => (
                    <tr key={`${row.team_side}-${row.position_id}`}>
                      <td>{row.team_side === "RADIANT" ? "Radiant" : "Dire"} {row.position_id}</td>
                      <td className="prematch-hero-name">
                        <strong>{heroNames.get(row.hero_id) || `英雄 ${row.hero_id}`}</strong>
                        <small>#{row.hero_id}</small>
                      </td>
                      <td>{signed(row.position_base_diff)}</td>
                      <td>{signed(row.same_team_synergy)}</td>
                      <td>{signed(row.opponent_matchup_synergy)}</td>
                      <td>{signed(row.display_score)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {minutes.length > 0 && (
            <div className="prematch-minute-table">
              <header>
                <h2>分钟评分</h2>
                <span>{minuteRangeLabel(minutes)}</span>
              </header>
              <div>
                <table>
                  <thead><tr><th>分钟</th><th>方向</th><th>优势分</th><th>Radiant 时间</th><th>Dire 时间</th><th>协同</th><th>高分段 / 回退</th></tr></thead>
                  <tbody>
                    {minutes.map((row) => (
                      <tr key={row.minute}>
                        <td>{row.minute}</td>
                        <td>{scoreDirection(row.display_score)}</td>
                        <td>{signed(row.display_score)}</td>
                        <td>{signed(row.radiant_time_delta)}</td>
                        <td>{signed(row.dire_time_delta)}</td>
                        <td>{signed(row.synergy_delta)}</td>
                        <td>{row.rank_source_counts.DIVINE_IMMORTAL || 0} / {row.rank_source_counts.ALL_RANK_FALLBACK || 0}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          <footer>
            <span>{result.rosh_profile_id}</span>
            <code>{result.formula_version}</code>
            <time dateTime={result.collected_at}>{new Date(result.collected_at).toLocaleString("zh-CN", { hour12: false })}</time>
          </footer>
        </div>
      </details>
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

function draftHero(hero: PrematchDraftHero): PrematchHero {
  return {
    hero_id: hero.hero_id,
    localized_name: hero.name,
    hero_key: "",
    image_url: hero.image_url,
  };
}

function paddedLineup(heroes: PrematchHero[]): Array<PrematchHero | null> {
  return [...heroes.slice(0, 5), ...EMPTY_LINEUP].slice(0, 5);
}

function recentMatchLabel(match: PrematchRecentMatch): string {
  const date = new Date(match.start_time * 1_000).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  return `${date} · #${match.match_id} · ${match.radiant_name || match.radiant_team_id} vs ${match.dire_name || match.dire_team_id}`;
}

function signed(value: number): string {
  return `${value > 0 ? "+" : ""}${value.toFixed(1)}`;
}

function signedNullable(value: number | null): string {
  return value == null ? "不可用" : signed(value);
}

function minuteRangeLabel(minutes: RoshAnalysisRunResponse["minute_points"]): string {
  const first = minutes[0]?.minute;
  const last = minutes[minutes.length - 1]?.minute;
  return first === last
    ? `共 1 个时间点 · ${first} 分钟`
    : `共 ${minutes.length} 个时间点 · ${first}-${last} 分钟`;
}

function scoreDirection(score: number | null): string {
  if (score == null || score === 0) return "均势";
  return score > 0 ? "Radiant" : "Dire";
}

function scoreTone(score: number | null): "radiant" | "dire" | "neutral" {
  if (score == null || score === 0) return "neutral";
  return score > 0 ? "radiant" : "dire";
}

function scoreStrength(score: number | null): string {
  if (score == null) return "不可用";
  const distance = Math.abs(score);
  if (distance < 3) return "轻微";
  if (distance < 8) return "中等";
  return "明显";
}

function strongestMinuteLabel(minutes: RoshAnalysisRunResponse["minute_points"]): string {
  if (!minutes.length) return "不可用";
  const strongest = minutes.reduce((current, point) => (
    Math.abs(point.display_score) > Math.abs(current.display_score) ? point : current
  ));
  return `约 ${strongest.minute} 分钟`;
}

function primaryScoreReason(
  components: RoshAnalysisRunResponse["hero_components"],
): string {
  if (!components.length) return "等待英雄分解";
  const totals = {
    position: components.reduce((sum, row) => sum + Math.abs(row.position_base_diff), 0),
    synergy: components.reduce((sum, row) => sum + Math.abs(row.same_team_synergy), 0),
    matchup: components.reduce((sum, row) => sum + Math.abs(row.opponent_matchup_synergy), 0),
  };
  const strongest = (Object.entries(totals) as Array<[keyof typeof totals, number]>)
    .sort((left, right) => right[1] - left[1])[0]?.[0];
  return {
    position: "位置对位贡献最大",
    synergy: "同队英雄协同贡献最大",
    matchup: "对手克制关系贡献最大",
  }[strongest || "position"];
}

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}
