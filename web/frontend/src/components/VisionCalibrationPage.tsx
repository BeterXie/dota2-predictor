import {
  Button,
  Skeleton,
  SkeletonItem,
  Tab,
  TabList,
  Tooltip,
} from "@fluentui/react-components";
import {
  ArrowCounterClockwise,
  ArrowLeft,
  ArrowRight,
  ArrowsOutSimple,
  CheckCircle,
  ImageSquare,
  LockKey,
  MagnifyingGlass,
  MagnifyingGlassMinus,
  MagnifyingGlassPlus,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import { useEffect, useMemo, useRef, useState } from "react";

import {
  buildVisionCalibrationCandidate,
  fetchHeroGrid,
  fetchVisionCalibration,
  runVisionCalibrationEvaluation,
  saveVisionCalibrationLabel,
} from "../api";
import type {
  PrematchHero,
  PrematchHeroGrid,
  VisionCalibrationBootstrap,
  VisionCalibrationEvent,
  VisionCalibrationEvaluation,
  VisionMatchSummary,
} from "../types";


interface VisionCalibrationPageProps {
  csrfToken: string | null;
}


type CalibrationPanel = "truth" | "candidate" | "evaluation";
type HeroAttribute = keyof PrematchHeroGrid;

const HERO_ATTRIBUTE_LABELS: Record<HeroAttribute, string> = {
  str: "力量",
  agi: "敏捷",
  int: "智力",
  all: "全才",
};


export function VisionCalibrationPage({ csrfToken }: VisionCalibrationPageProps) {
  const [data, setData] = useState<VisionCalibrationBootstrap | null>(null);
  const [heroes, setHeroes] = useState<PrematchHero[]>([]);
  const [heroGrid, setHeroGrid] = useState<PrematchHeroGrid>({ str: [], agi: [], int: [], all: [] });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedMatchId, setSelectedMatchId] = useState<string | null>(null);
  const [profileId, setProfileId] = useState("all");
  const [truth, setTruth] = useState<Array<number | "">>(Array(10).fill(""));
  const [matchId, setMatchId] = useState("");
  const [mapNumber, setMapNumber] = useState("");
  const [note, setNote] = useState("");
  const [candidateId, setCandidateId] = useState("");
  const [observationFile, setObservationFile] = useState("");
  const [layoutProfile, setLayoutProfile] = useState("");
  const [mode, setMode] = useState<"perception" | "runtime">("perception");
  const [activePanel, setActivePanel] = useState<CalibrationPanel>("truth");
  const [frameScale, setFrameScale] = useState(1);
  const [focusedSlot, setFocusedSlot] = useState<number | null>(null);
  const [heroPickerSlot, setHeroPickerSlot] = useState<number | null>(null);
  const [heroAttribute, setHeroAttribute] = useState<HeroAttribute>("str");
  const [heroSearch, setHeroSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<"label" | "candidate" | "evaluation" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const frameStageRef = useRef<HTMLDivElement>(null);
  const calibrationGridRef = useRef<HTMLDivElement>(null);
  const heroSearchRef = useRef<HTMLInputElement>(null);

  const reload = async (signal?: AbortSignal) => {
    const value = await fetchVisionCalibration(signal);
    setData(value);
    setSelectedId((current) => (
      current && value.events.some((event) => event.event_id === current)
        ? current
        : value.events[0]?.event_id || null
    ));
  };

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([reload(controller.signal), fetchHeroGrid(controller.signal)])
      .then(([, grid]) => {
        const catalog = Array.from(
          new Map(Object.values(grid).flat().map((hero) => [hero.hero_id, hero])).values(),
        ).sort((one, two) => one.localized_name.localeCompare(two.localized_name));
        setHeroGrid(grid);
        setHeroes(catalog);
        setError(null);
      })
      .catch((reason: Error) => {
        if (reason.name !== "AbortError") setError(reason.message || "无法加载 Vision 校正工作台");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, []);

  const profiles = data?.profiles || [];
  const matchSummaries = data?.match_summaries || [];
  const heroesById = useMemo(
    () => new Map(heroes.map((hero) => [hero.hero_id, hero])),
    [heroes],
  );
  const visibleEvents = useMemo(
    () => data?.events.filter((item) => (
      (profileId === "all" || item.profile_id === profileId)
      && (
        selectedMatchId === null
        || item.label?.raybet_match_id?.trim() === selectedMatchId
      )
    )) || [],
    [data?.events, profileId, selectedMatchId],
  );
  const selectedProfile = profiles.find((item) => item.profile_id === profileId) || null;

  useEffect(() => {
    if (profileId !== "all" && !profiles.some((item) => item.profile_id === profileId)) {
      setProfileId("all");
      return;
    }
    if (!selectedId || !visibleEvents.some((item) => item.event_id === selectedId)) {
      setSelectedId(visibleEvents[0]?.event_id || null);
    }
  }, [profileId, profiles, selectedId, visibleEvents]);

  const event = useMemo(
    () => data?.events.find((item) => item.event_id === selectedId) || null,
    [data?.events, selectedId],
  );
  useEffect(() => {
    if (!event) return;
    const suggested = event.label?.hero_ids
      || event.slot_diagnostics.map((slot) => slot.best_hero_id || "");
    setTruth(Array.from({ length: 10 }, (_, index) => suggested[index] || ""));
    setMatchId(event.label?.raybet_match_id || "");
    setMapNumber(event.label?.map_number?.toString() || "");
    setNote(event.label?.note || "");
    setLayoutProfile(event.layout || data?.layout_profiles[0] || "");
    const relatedCandidate = data?.candidates.find((item) => item.label_id === event.event_id)
      || data?.candidates.find((item) => item.profile_id === event.profile_id);
    setCandidateId((current) => (
      current && data?.candidates.some((item) => (
        item.candidate_id === current && item.profile_id === event.profile_id
      ))
        ? current
        : relatedCandidate?.candidate_id || ""
    ));
    setObservationFile("");
    setFrameScale(1);
    setFocusedSlot(null);
    setHeroPickerSlot(null);
    setHeroSearch("");
    setError(null);
    setMessage(null);
  }, [data?.candidates, data?.layout_profiles, event]);

  useEffect(() => {
    if (heroPickerSlot === null) return undefined;
    const timer = window.setTimeout(() => heroSearchRef.current?.focus(), 0);
    return () => window.clearTimeout(timer);
  }, [heroPickerSlot]);

  const selectedTruthHeroIds = useMemo(
    () => new Set(truth.filter((value): value is number => typeof value === "number")),
    [truth],
  );
  const pickerHeroes = useMemo(() => {
    const query = heroSearch.trim().toLocaleLowerCase("zh-CN");
    return heroGrid[heroAttribute].filter((hero) => (
      !query || hero.localized_name.toLocaleLowerCase("zh-CN").includes(query)
    ));
  }, [heroAttribute, heroGrid, heroSearch]);

  const truthReady = truth.every((value) => typeof value === "number")
    && new Set(truth).size === 10;
  const parsedMapNumber = Number(mapNumber);
  const labelContextReady = Boolean(
    matchId.trim()
    && Number.isInteger(parsedMapNumber)
    && parsedMapNumber >= 1
    && parsedMapNumber <= 5,
  );
  const labelInputsSaved = Boolean(
    event?.label
    && labelContextReady
    && event.label.raybet_match_id === matchId.trim()
    && event.label.map_number === parsedMapNumber
    && event.label.hero_ids.length === truth.length
    && truth.every((heroId, index) => heroId === event.label?.hero_ids[index]),
  );
  const relatedCandidates = data?.candidates.filter((item) => item.profile_id === event?.profile_id) || [];
  const selectedCandidate = relatedCandidates.find((item) => item.candidate_id === candidateId);
  const matchingObservationFiles = useMemo(() => {
    const savedMatchId = event?.label?.raybet_match_id?.trim();
    if (!savedMatchId) return [];
    return data?.observation_files.filter((item) => (
      observationMatchId(item.name, item.raybet_match_id) === savedMatchId
    )) || [];
  }, [data?.observation_files, event?.label?.raybet_match_id]);
  const selectedObservation = matchingObservationFiles.find((item) => item.name === observationFile);
  const layoutMatchesLabel = Boolean(
    event?.label?.profile_id && layoutProfile === event.label.profile_id,
  );
  const canEvaluate = Boolean(
    csrfToken
    && labelInputsSaved
    && selectedCandidate
    && selectedObservation
    && layoutMatchesLabel,
  );
  const currentEvaluations = useMemo(() => {
    if (!event?.label || !selectedCandidate || !labelInputsSaved) return [];
    const savedMatchId = event.label.raybet_match_id;
    const savedMapNumber = event.label.map_number;
    return data?.evaluations.filter((row) => (
      row.label_id === event.label?.label_id
      && row.candidate_id === selectedCandidate.candidate_id
      && observationMatchId(row.observation_file, row.raybet_match_id) === savedMatchId
      && row.map_number === savedMapNumber
    )) || [];
  }, [data?.evaluations, event?.label, labelInputsSaved, selectedCandidate]);

  useEffect(() => {
    if (observationFile && !matchingObservationFiles.some((item) => item.name === observationFile)) {
      setObservationFile("");
    }
  }, [matchingObservationFiles, observationFile]);

  const clearFeedback = () => {
    setError(null);
    setMessage(null);
  };

  const openHeroPicker = (slot: number) => {
    clearFeedback();
    setFocusedSlot(slot);
    setHeroPickerSlot(slot);
  };

  const closeHeroPicker = () => {
    setHeroPickerSlot(null);
    setHeroSearch("");
  };

  const selectTruthHero = (heroId: number) => {
    if (heroPickerSlot === null) return;
    setTruth((current) => current.map((value, index) => (
      index === heroPickerSlot ? heroId : value
    )));
    closeHeroPicker();
  };

  const clearTruthHero = (slot: number) => {
    clearFeedback();
    setTruth((current) => current.map((value, index) => (index === slot ? "" : value)));
  };

  let evaluationDisabledReason: string | null = null;
  if (!event?.label) evaluationDisabledReason = "先保存当前事件的真值标签。";
  else if (!labelInputsSaved) evaluationDisabledReason = "比赛、Map 或英雄真值有未保存的修改。";
  else if (!matchingObservationFiles.length) evaluationDisabledReason = "当前比赛没有可用的 Observation JSONL。";
  else if (!layoutMatchesLabel) evaluationDisabledReason = "Layout profile 必须与真值标签一致。";

  const selectedEventIndex = visibleEvents.findIndex((item) => item.event_id === selectedId);
  const currentProfileLabel = event?.profile_id
    || selectedProfile?.profile_id
    || (profileId === "all" ? "全部赛事 UI" : profileId);
  const pendingCount = visibleEvents.filter((item) => !item.label).length;

  const selectRelativeSample = (offset: number) => {
    const next = visibleEvents[selectedEventIndex + offset];
    if (next) setSelectedId(next.event_id);
  };

  const clearMatchSelection = () => {
    setSelectedMatchId(null);
    setMessage(null);
  };

  const selectMatchSummary = (summary: VisionMatchSummary) => {
    clearFeedback();
    const summaryMatchId = String(summary.raybet_match_id || summary.match_id).trim();
    const relatedEvent = data?.events.find((item) => (
      item.label?.raybet_match_id?.trim() === summaryMatchId
    ));
    setSelectedMatchId(summaryMatchId);
    setSelectedId(relatedEvent?.event_id || null);
    if (relatedEvent) {
      setProfileId(relatedEvent.profile_id);
      setActivePanel("evaluation");
    } else {
      const summaryProfile = summary.layout_profile || "";
      setProfileId(profiles.some((item) => item.profile_id === summaryProfile) ? summaryProfile : "all");
      setActivePanel("truth");
      setMessage("该局已有采集汇总，但还没有关联到校正样本。");
    }
    window.setTimeout(() => {
      calibrationGridRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 0);
  };

  const openFrameFullscreen = async () => {
    try {
      await frameStageRef.current?.requestFullscreen();
    } catch {
      setError("浏览器无法打开全屏预览");
    }
  };

  const saveLabel = async () => {
    if (!csrfToken || !event || !truthReady || !labelContextReady) return;
    setBusy("label");
    setError(null);
    try {
      await saveVisionCalibrationLabel(event.event_id, {
        hero_ids: truth as number[],
        raybet_match_id: matchId.trim(),
        map_number: parsedMapNumber,
        note: note.trim() || null,
      }, csrfToken);
      await reload();
      setMessage("真值标签已保存；生产特征包未变化。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存标签失败");
    } finally {
      setBusy(null);
    }
  };

  const buildCandidate = async () => {
    if (!csrfToken || !event?.label || !labelInputsSaved) return;
    setBusy("candidate");
    setError(null);
    try {
      const candidate = await buildVisionCalibrationCandidate(
        event.label.label_id,
        candidateId || null,
        csrfToken,
      );
      await reload();
      setCandidateId(candidate.candidate_id);
      setMessage(`已生成隔离候选包，新增 ${candidate.added_variant_count} 个外观模板；需要留出评估后才能讨论推广。`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "构建候选包失败");
    } finally {
      setBusy(null);
    }
  };

  const runEvaluation = async () => {
    if (!csrfToken || !event?.label || !selectedObservation || !canEvaluate) return;
    setBusy("evaluation");
    setError(null);
    try {
      const result = await runVisionCalibrationEvaluation({
        label_id: event.label.label_id,
        candidate_id: candidateId,
        observation_file: selectedObservation.name,
        layout_profile: layoutProfile,
        mode,
        captured_after: null,
        captured_before: null,
      }, csrfToken);
      await reload();
      setMessage(
        result.wrong_lock_count === 0
          ? `评估完成：${result.final_correct_locked_slots}/10 正确锁，0 错锁。`
          : `评估完成，但检测到 ${result.wrong_lock_count} 个错误锁。`,
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "运行评估失败");
    } finally {
      setBusy(null);
    }
  };

  if (loading) {
    return <CalibrationSkeleton />;
  }

  return (
    <main className="vision-calibration-page">
      <header className="vision-workbench-header">
        <div className="vision-page-title">
          <h1>Vision 校正</h1>
          <p>训练 HUD 识别模板并验证真实直播帧</p>
        </div>
        <dl className="vision-page-status">
          <div>
            <dt>当前 Profile</dt>
            <dd title={currentProfileLabel}>{currentProfileLabel}</dd>
          </div>
          <div>
            <dt>待处理</dt>
            <dd>{pendingCount}</dd>
          </div>
        </dl>
        <div className="vision-boundary-badge" role="note" title={data?.candidate_boundary}>
          <LockKey size={16} aria-hidden="true" />
          <span>生产边界已锁定</span>
        </div>
      </header>

      {error && <div className="vision-feedback error" role="alert"><WarningCircle size={18} />{error}</div>}
      {message && <div className="vision-feedback success" role="status"><CheckCircle size={18} />{message}</div>}

      {matchSummaries.length > 0 && (
        <section className="vision-match-overview" aria-label="比赛汇总">
          <header className="vision-match-overview-header">
            <div>
              <h2>比赛汇总</h2>
              <p>按一局比赛查看采集完整度，再从下方帧级队列进行真值校正。</p>
            </div>
            <span>{matchSummaries.length} 局</span>
          </header>
          <div className="vision-match-summary-grid">
            {matchSummaries.map((summary) => (
              <MatchSummaryCard
                key={summary.match_id}
                onSelect={selectMatchSummary}
                selected={selectedMatchId === String(summary.raybet_match_id || summary.match_id).trim()}
                summary={summary}
              />
            ))}
          </div>
        </section>
      )}

      {!data?.events.length ? (
        <section className="vision-empty-state">
          <ImageSquare size={32} />
          <h2>还没有可校正的真实帧</h2>
          <p>Stable Vision 已随项目启动。出现直播且产生失败/边界诊断后，事件会自动进入这里。</p>
          <code>data/live_betting/vision_debug</code>
        </section>
      ) : (
        <div className="vision-calibration-grid" ref={calibrationGridRef}>
          <section className="vision-event-queue" aria-label="Vision 校正样本">
            <header className="vision-queue-header">
              <div className="vision-queue-title">
                <div><h2>校正队列</h2><p>真实帧样本</p><small>逐帧选择，标注十英雄 HUD 真值</small></div>
                <span>{visibleEvents.length}</span>
              </div>
              <label className="vision-profile-selector">
                <span>赛事 UI profile</span>
                <select
                  aria-label="赛事 UI profile"
                  onChange={(change) => {
                    setSelectedMatchId(null);
                    setProfileId(change.target.value);
                  }}
                  value={profileId}
                >
                  <option value="all">全部赛事 UI</option>
                  {profiles.map((profile) => (
                    <option key={profile.profile_id} value={profile.profile_id}>
                      {profile.layout || profile.profile_id} · {profile.event_count} 个样本
                    </option>
                  ))}
                </select>
              </label>
              <p className="vision-profile-summary">
                {selectedProfile
                  ? `${selectedProfile.layout || selectedProfile.profile_id}：${selectedProfile.labeled_event_count} 个样本已标注，${selectedProfile.candidate_count} 个候选可复用`
                  : "同一 UI profile 可以复用候选模板。"}
              </p>
              {selectedMatchId && (
                <div className="vision-match-filter">
                  <span>RayBet {selectedMatchId}</span>
                  <Tooltip content="清除比赛筛选" relationship="label">
                    <Button
                      appearance="subtle"
                      aria-label="清除比赛筛选"
                      icon={<X size={14} />}
                      onClick={clearMatchSelection}
                      size="small"
                    />
                  </Tooltip>
                </div>
              )}
            </header>
            <div className="vision-event-list">
              {visibleEvents.map((item) => {
                const savedMatchId = item.label?.raybet_match_id?.trim();
                const observation = data.observation_files.find((row) => (
                  savedMatchId
                  && observationMatchId(row.name, row.raybet_match_id) === savedMatchId
                ));
                const matchTitle = observation?.display_name
                  || (savedMatchId ? `RayBet ${savedMatchId}` : "未关联比赛");
                const mapLabel = item.label?.map_number ? `Map ${item.label.map_number}` : "Map 未标注";
                return (
                  <button
                    className={item.event_id === selectedId ? "vision-event selected" : "vision-event"}
                    key={item.event_id}
                    onClick={() => setSelectedId(item.event_id)}
                    title={`${matchTitle} | ${item.reason} | ${item.profile_id}`}
                    type="button"
                  >
                    <span className="vision-event-topline">
                      <time className="vision-event-time">{formatTime(item.captured_at)}</time>
                      <span className={item.label ? "vision-event-state labeled" : "vision-event-state"}>
                        {item.label ? "已校正" : "待校正"}
                      </span>
                    </span>
                    <strong>{matchTitle}</strong>
                    <span className="vision-event-reason">{mapLabel} · {item.reason}</span>
                    <small title={item.profile_id}>{item.profile_id}</small>
                  </button>
                );
              })}
              {!visibleEvents.length && (
                <div className="vision-queue-empty">
                  <ImageSquare size={22} aria-hidden="true" />
                  <strong>该比赛暂无关联校正样本</strong>
                  <span>采集汇总仍然保留，可在产生可校正帧后继续标注。</span>
                </div>
              )}
            </div>
          </section>

          {event && (
            <section className="vision-inspector">
              <header className="vision-inspector-header">
                <div>
                  <h2>视觉样本</h2>
                  <p>{formatTime(event.captured_at)}</p>
                </div>
                <div className="vision-frame-toolbar" aria-label="帧预览工具">
                  <Tooltip content="上一样本" relationship="label">
                    <Button
                      appearance="subtle"
                      aria-label="上一样本"
                      disabled={selectedEventIndex <= 0}
                      icon={<ArrowLeft size={17} />}
                      onClick={() => selectRelativeSample(-1)}
                      size="small"
                    />
                  </Tooltip>
                  <Tooltip content="下一样本" relationship="label">
                    <Button
                      appearance="subtle"
                      aria-label="下一样本"
                      disabled={selectedEventIndex < 0 || selectedEventIndex >= visibleEvents.length - 1}
                      icon={<ArrowRight size={17} />}
                      onClick={() => selectRelativeSample(1)}
                      size="small"
                    />
                  </Tooltip>
                  <span className="vision-toolbar-divider" />
                  <Tooltip content="缩小" relationship="label">
                    <Button
                      appearance="subtle"
                      aria-label="缩小帧"
                      disabled={frameScale <= 0.75}
                      icon={<MagnifyingGlassMinus size={17} />}
                      onClick={() => setFrameScale((current) => Math.max(0.75, current - 0.25))}
                      size="small"
                    />
                  </Tooltip>
                  <span className="vision-zoom-value">{Math.round(frameScale * 100)}%</span>
                  <Tooltip content="放大" relationship="label">
                    <Button
                      appearance="subtle"
                      aria-label="放大帧"
                      disabled={frameScale >= 2}
                      icon={<MagnifyingGlassPlus size={17} />}
                      onClick={() => setFrameScale((current) => Math.min(2, current + 0.25))}
                      size="small"
                    />
                  </Tooltip>
                  <Tooltip content="重置缩放" relationship="label">
                    <Button
                      appearance="subtle"
                      aria-label="重置缩放"
                      icon={<ArrowCounterClockwise size={17} />}
                      onClick={() => setFrameScale(1)}
                      size="small"
                    />
                  </Tooltip>
                  <Tooltip content="全屏预览" relationship="label">
                    <Button
                      appearance="subtle"
                      aria-label="全屏预览"
                      icon={<ArrowsOutSimple size={17} />}
                      onClick={() => void openFrameFullscreen()}
                      size="small"
                    />
                  </Tooltip>
                </div>
              </header>
              <div className="vision-frame-meta" aria-label="帧状态">
                <span>Screen <strong>{event.screen_state || "-"}</strong></span>
                <span>Layout <strong>{event.layout_state || "-"}</strong></span>
                <span>Blocker <strong>{event.blocker_code || event.reason}</strong></span>
              </div>
              <div className="vision-frame-stage" ref={frameStageRef}>
                <div className="vision-frame-canvas" style={{ width: `${frameScale * 100}%` }}>
                  <img className="vision-full-frame" src={event.frame_url} alt="选中 Vision 校正样本的完整直播帧" />
                </div>
              </div>

              <section className="vision-slot-section" aria-label="HUD 槽位识别">
                <header>
                  <div><h3>HUD 槽位识别</h3><p>当前帧每个英雄槽位的最佳识别结果</p></div>
                  <span>{event.crop_count}/10 crops</span>
                </header>
                <div className="vision-team-groups">
                  {(["Radiant", "Dire"] as const).map((side, sideIndex) => {
                    const start = sideIndex * 5;
                    return (
                      <section className={`vision-team-group ${side.toLowerCase()}`} key={side}>
                        <header><h4>{side}</h4><span>5 个槽位</span></header>
                        <div className="vision-crop-grid">
                          {event.crop_urls.slice(start, start + 5).map((url, localIndex) => {
                            const index = start + localIndex;
                            const diagnostic = event.slot_diagnostics[index];
                            const hero = heroes.find((item) => item.hero_id === diagnostic?.best_hero_id);
                            const truthMatch = typeof truth[index] === "number"
                              && truth[index] === diagnostic?.best_hero_id;
                            return (
                              <button
                                aria-label={`${side} slot ${localIndex + 1} 识别结果`}
                                aria-pressed={focusedSlot === index}
                                className={focusedSlot === index ? "vision-crop selected" : "vision-crop"}
                                key={url}
                                onClick={() => setFocusedSlot(index)}
                                type="button"
                              >
                                <img src={url} alt={`${side} ${localIndex + 1} 英雄 crop`} />
                                <span className="vision-crop-body">
                                  <span className="vision-crop-heading">
                                    <span className="vision-slot-id">{side === "Radiant" ? "R" : "D"}{localIndex + 1}</span>
                                    {truthMatch && <CheckCircle size={15} weight="fill" aria-label="与真值一致" />}
                                  </span>
                                  <strong>{hero?.localized_name || `Hero #${diagnostic?.best_hero_id || "-"}`}</strong>
                                  <small>Hero ID #{diagnostic?.best_hero_id || "-"}</small>
                                  <span className="vision-crop-metrics">
                                    <span>置信度 <strong>{diagnostic ? formatPercent(diagnostic.best_score) : "-"}</strong></span>
                                    <span>差值 <strong>{diagnostic ? diagnostic.margin.toFixed(3) : "-"}</strong></span>
                                  </span>
                                </span>
                              </button>
                            );
                          })}
                        </div>
                      </section>
                    );
                  })}
                </div>
              </section>
            </section>
          )}

          {event && (
            <aside className="vision-calibration-form">
              <header className="vision-workflow-header">
                <div><h2>校正流程</h2><p>标注、构建并验证候选模板</p></div>
                <span>{event.label ? "已标注" : "待标注"}</span>
              </header>
              <TabList
                aria-label="校正流程"
                className="vision-workflow-tabs"
                onTabSelect={(_, tabData) => setActivePanel(tabData.value as CalibrationPanel)}
                selectedValue={activePanel}
                size="small"
              >
                <Tab value="truth">真实值</Tab>
                <Tab value="candidate">候选模板</Tab>
                <Tab value="evaluation">留出评估</Tab>
              </TabList>

              {activePanel === "truth" && (
                <section className="vision-workflow-panel" aria-label="真实值">
                  <div className="vision-panel-heading"><h3>HUD 真实值</h3><p>按画面从左到右保存双方阵容。</p></div>
                  <div className="vision-truth-teams">
                    {(["Radiant", "Dire"] as const).map((side, sideIndex) => (
                      <fieldset className={`vision-truth-team ${side.toLowerCase()}`} key={side}>
                        <legend>{side} · 点击头像选择英雄</legend>
                        {truth.slice(sideIndex * 5, sideIndex * 5 + 5).map((value, localIndex) => {
                          const index = sideIndex * 5 + localIndex;
                          const hero = typeof value === "number" ? heroesById.get(value) : null;
                          return (
                            <div className={hero ? "vision-truth-slot filled" : "vision-truth-slot"} key={`${event.event_id}-${index}`}>
                              <button
                                aria-label={`选择 ${side} slot ${localIndex + 1} 英雄`}
                                className="vision-truth-slot-select"
                                onClick={() => openHeroPicker(index)}
                                type="button"
                              >
                                <span className="vision-truth-slot-index">#{localIndex + 1}</span>
                                {hero ? (
                                  <img alt="" loading="lazy" src={hero.image_url} />
                                ) : (
                                  <span className="vision-truth-slot-placeholder"><ImageSquare size={18} /></span>
                                )}
                                <span className="vision-truth-slot-copy">
                                  <strong>{hero?.localized_name || "选择英雄"}</strong>
                                  <small>{hero ? `Hero ID #${hero.hero_id}` : "点击打开英雄头像"}</small>
                                </span>
                              </button>
                              {hero && (
                                <button
                                  aria-label={`清除 ${side} slot ${localIndex + 1} 英雄`}
                                  className="vision-truth-slot-clear"
                                  onClick={() => clearTruthHero(index)}
                                  type="button"
                                >
                                  <X size={14} />
                                </button>
                              )}
                            </div>
                          );
                        })}
                      </fieldset>
                    ))}
                  </div>
                  <div className="vision-context-fields">
                    <label><span>RayBet match ID</span><input onChange={(change) => { clearFeedback(); setMatchId(change.target.value); }} value={matchId} /></label>
                    <label><span>Map</span><input max="5" min="1" onChange={(change) => { clearFeedback(); setMapNumber(change.target.value); }} type="number" value={mapNumber} /></label>
                  </div>
                  <label className="vision-note"><span>标注说明</span><textarea onChange={(change) => setNote(change.target.value)} rows={2} value={note} /></label>
                  {!truthReady && <p className="vision-disabled-reason">需要十个互不重复的英雄真实值。</p>}
                  {truthReady && !labelContextReady && <p className="vision-disabled-reason">需要 RayBet match ID 和 1-5 的 Map 编号。</p>}
                  <Button
                    appearance="primary"
                    className="vision-primary-action"
                    disabled={!csrfToken || !truthReady || !labelContextReady || busy !== null}
                    onClick={() => void saveLabel()}
                  >
                    {busy === "label" ? "正在保存…" : "保存真实值标签"}
                  </Button>
                </section>
              )}

              {activePanel === "candidate" && (
                <section className="vision-workflow-panel" aria-label="候选模板">
                  <div className="vision-panel-heading"><h3>候选模板</h3><p>只更新隔离候选包，不覆盖生产模板。</p></div>
                  <dl className="vision-workflow-summary">
                    <div><dt>当前标签</dt><dd>{event.label ? formatTime(event.label.updated_at) : "尚未保存"}</dd></div>
                    <div><dt>UI Profile</dt><dd title={event.profile_id}>{event.profile_id}</dd></div>
                    <div><dt>可复用候选</dt><dd>{relatedCandidates.length}</dd></div>
                    <div><dt>生产模板</dt><dd>保持不变</dd></div>
                  </dl>
                  <label>
                    <span>候选基线</span>
                    <select
                      aria-label="候选基线"
                      onChange={(change) => { clearFeedback(); setCandidateId(change.target.value); }}
                      value={candidateId}
                    >
                      <option value="">当前已推广 Profile 包</option>
                      {relatedCandidates.map((item) => (
                        <option key={item.candidate_id} value={item.candidate_id}>
                          {formatTime(item.created_at)} · +{item.added_variant_count} 外观 · {item.promoted ? "已推广" : "隔离"}
                        </option>
                      ))}
                    </select>
                  </label>
                  <Button
                    appearance="secondary"
                    className="vision-secondary-action"
                    disabled={!csrfToken || !event.label || !labelInputsSaved || event.crop_count !== 10 || busy !== null}
                    onClick={() => void buildCandidate()}
                  >
                    {busy === "candidate" ? "正在构建…" : "从当前标签构建候选"}
                  </Button>
                  {!event.label && <p className="vision-disabled-reason">先保存当前样本的真实值标签。</p>}
                  {event.label && !labelInputsSaved && <p className="vision-disabled-reason">先保存当前真实值、比赛和 Map 的修改。</p>}
                </section>
              )}

              {activePanel === "evaluation" && (
                <section className="vision-workflow-panel" aria-label="留出评估">
                  <div className="vision-panel-heading"><h3>留出评估</h3><p>使用另一段真实序列验证最终锁定。</p></div>
                  <label><span>候选包</span><select onChange={(change) => { clearFeedback(); setCandidateId(change.target.value); }} value={candidateId}><option value="">选择当前 profile 的候选</option>{relatedCandidates.map((item) => <option key={item.candidate_id} value={item.candidate_id}>{formatTime(item.created_at)} · {item.promoted ? "已推广" : "隔离"}</option>)}</select></label>
                  <label>
                    <span>Observation 序列</span>
                    <select
                      aria-label="Observation JSONL"
                      disabled={!matchingObservationFiles.length}
                      onChange={(change) => { clearFeedback(); setObservationFile(change.target.value); }}
                      value={observationFile}
                    >
                      <option value="">
                        {matchingObservationFiles.length ? "选择序列" : "当前比赛没有可用序列"}
                      </option>
                      {matchingObservationFiles.map((item) => (
                        <option key={item.name} value={item.name}>{item.display_name || item.name}</option>
                      ))}
                    </select>
                  </label>
                  <details className="vision-advanced-settings">
                    <summary>高级信息</summary>
                    <div>
                      <label><span>Layout profile</span><select onChange={(change) => { clearFeedback(); setLayoutProfile(change.target.value); }} value={layoutProfile}>{data.layout_profiles.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
                      <label><span>评估模式</span><select onChange={(change) => { clearFeedback(); setMode(change.target.value as "perception" | "runtime"); }} value={mode}><option value="perception">Perception only</option><option value="runtime">Runtime faithful</option></select></label>
                      <dl>
                        <div><dt>Observation 文件</dt><dd>{observationFile || "未选择"}</dd></div>
                        <div><dt>Observation 路径</dt><dd>{data.observation_root}</dd></div>
                        <div><dt>生产特征包</dt><dd>{data.production_feature_path}</dd></div>
                      </dl>
                    </div>
                  </details>
                  <Button
                    appearance="primary"
                    className="vision-primary-action"
                    disabled={!canEvaluate || busy !== null}
                    onClick={() => void runEvaluation()}
                  >
                    {busy === "evaluation" ? "正在评估…" : "运行留出评估"}
                  </Button>
                  {evaluationDisabledReason && <p className="vision-disabled-reason">{evaluationDisabledReason}</p>}
                  <EvaluationResults rows={currentEvaluations} />
                </section>
              )}
            </aside>
          )}
        </div>
      )}

      {heroPickerSlot !== null && (
        <div className="vision-hero-picker-backdrop" onMouseDown={(mouse) => {
          if (mouse.currentTarget === mouse.target) closeHeroPicker();
        }}>
          <section aria-label="HUD 真实值英雄选择器" aria-modal="true" className="vision-hero-picker" role="dialog">
            <header>
              <div>
                <strong>{heroPickerSlot < 5 ? "Radiant" : "Dire"} · 槽位 {(heroPickerSlot % 5) + 1}</strong>
                <small>点击英雄头像后立即写入当前 HUD 真值槽位</small>
              </div>
              <button aria-label="关闭英雄选择器" onClick={closeHeroPicker} type="button"><X size={18} /></button>
            </header>
            <div className="vision-hero-picker-controls">
              <div aria-label="英雄属性" role="tablist">
                {(Object.keys(HERO_ATTRIBUTE_LABELS) as HeroAttribute[]).map((attribute) => (
                  <button
                    aria-selected={heroAttribute === attribute}
                    className={heroAttribute === attribute ? "active" : ""}
                    key={attribute}
                    onClick={() => setHeroAttribute(attribute)}
                    role="tab"
                    type="button"
                  >
                    {HERO_ATTRIBUTE_LABELS[attribute]}
                  </button>
                ))}
              </div>
              <label className="vision-hero-search">
                <MagnifyingGlass size={16} aria-hidden="true" />
                <input
                  aria-label="搜索英雄"
                  onChange={(change) => setHeroSearch(change.target.value)}
                  placeholder="搜索英雄"
                  ref={heroSearchRef}
                  value={heroSearch}
                />
              </label>
            </div>
            <div className="vision-hero-picker-grid">
              {pickerHeroes.map((hero) => {
                const currentHeroId = truth[heroPickerSlot];
                const unavailable = selectedTruthHeroIds.has(hero.hero_id)
                  && currentHeroId !== hero.hero_id;
                return (
                  <button
                    aria-label={`选择英雄 ${hero.localized_name}`}
                    className={currentHeroId === hero.hero_id ? "selected" : ""}
                    disabled={unavailable}
                    key={hero.hero_id}
                    onClick={() => selectTruthHero(hero.hero_id)}
                    type="button"
                  >
                    <img alt="" loading="lazy" src={hero.image_url} />
                    <span>{hero.localized_name}</span>
                  </button>
                );
              })}
              {!pickerHeroes.length && <p>当前分类没有匹配的英雄</p>}
            </div>
          </section>
        </div>
      )}
    </main>
  );
}


function MatchSummaryCard({
  onSelect,
  selected,
  summary,
}: {
  onSelect: (summary: VisionMatchSummary) => void;
  selected: boolean;
  summary: VisionMatchSummary;
}) {
  const maps = summary.maps.length ? summary.maps.map((map) => `Map ${map}`).join(" · ") : "Map 待识别";
  const title = summary.display_name || `RayBet ${summary.match_id}`;
  const stateClass = ["live", "draft", "preparing", "ending", "ended", "waiting", "archived"]
    .includes(summary.status) ? summary.status : "unknown";
  return (
    <article className={`vision-match-summary ${stateClass}${selected ? " selected" : ""}`}>
      <button
        aria-label={`打开 ${title} 的校正记录`}
        aria-pressed={selected}
        className="vision-match-summary-action"
        onClick={() => onSelect(summary)}
        type="button"
      />
      <header>
        <div>
          <h3 title={title}>{title}</h3>
          <p>RayBet {summary.match_id} · {maps}</p>
        </div>
        <span className="vision-match-state">{summary.status_label}</span>
      </header>
      <dl>
        <div><dt>逐帧观测</dt><dd>{summary.observation_count}</dd></div>
        <div><dt>证据帧</dt><dd>{summary.evidence_frame_count}</dd></div>
        <div><dt>30 秒采样</dt><dd>{summary.periodic_count}</dd></div>
        <div><dt>生命周期事件</dt><dd>{summary.manifest_event_count}</dd></div>
      </dl>
      <div className="vision-match-summary-tags">
        <span>Screen {summary.latest_screen_state || "unknown"}</span>
        <span>{summary.layout_profile || "Layout 待识别"}</span>
        {summary.draft_started && <span className="confirmed">BP 已确认</span>}
        {summary.game_started && <span className="confirmed">开局已确认</span>}
        {summary.ended_final && <span className="confirmed">结束帧已保存</span>}
      </div>
      <footer>
        <time>{formatMatchRange(summary.first_captured_at, summary.last_captured_at)}</time>
        <code>{summary.phase}</code>
      </footer>
    </article>
  );
}


function CalibrationSkeleton() {
  return (
    <main className="vision-calibration-page vision-calibration-skeleton" aria-label="正在加载 Vision 校正工作台">
      <Skeleton className="vision-skeleton-header">
        <SkeletonItem shape="rectangle" size={32} />
        <SkeletonItem shape="rectangle" size={16} />
      </Skeleton>
      <div className="vision-calibration-grid">
        <Skeleton className="vision-skeleton-rail"><SkeletonItem shape="rectangle" size={128} /></Skeleton>
        <Skeleton className="vision-skeleton-frame"><SkeletonItem shape="rectangle" size={128} /></Skeleton>
        <Skeleton className="vision-skeleton-panel"><SkeletonItem shape="rectangle" size={128} /></Skeleton>
      </div>
    </main>
  );
}


function EvaluationResults({ rows }: { rows: VisionCalibrationEvaluation[] }) {
  return (
    <section className="vision-results" aria-label="最近评估">
      <header><h3>最近评估</h3><span>{rows.length ? `${rows.length} 条` : "暂无结果"}</span></header>
      {!rows.length && <p className="vision-result-empty">当前标签与候选还没有评估结果。</p>}
      <div className="vision-evaluation-list">
        {rows.slice(0, 6).map((row, index) => (
          <article className={`${row.wrong_lock_count ? "failed" : "passed"}${index === 0 ? " latest" : ""}`} key={row.evaluation_id}>
            <header>
              <div><span>{index === 0 ? "最新结果" : formatTime(row.created_at)}</span><strong>{row.final_correct_locked_slots} / {row.final_locked_slots}</strong><small>正确锁定 / 总锁定</small></div>
              <span className="vision-result-state">{row.wrong_lock_count ? "需要检查" : "通过"}</span>
            </header>
            <dl className="vision-result-metrics">
              <div><dt>Precision</dt><dd>{formatPercent(row.accepted_precision)}</dd></div>
              <div><dt>Post-lock</dt><dd>{formatPercent(row.exact_post_lock_rate)}</dd></div>
              <div><dt>错误锁定</dt><dd>{row.wrong_lock_count}</dd></div>
            </dl>
            <footer>
              <span>{formatTime(row.created_at)}</span>
              <span>Map {row.map_number}</span>
              <span>锁定延迟 {formatLatency(row.lock_latency_seconds)}</span>
            </footer>
            <details className="vision-result-technical"><summary>技术详情</summary><code>{row.mode} | {row.observation_file}</code></details>
          </article>
        ))}
      </div>
    </section>
  );
}


function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}


function formatPercent(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}


function formatLatency(value: number | null): string {
  return value === null ? "未锁定" : `${value.toFixed(1)} 秒`;
}


function formatMatchRange(first: string | null, last: string | null): string {
  if (!first && !last) return "尚无有效时间戳";
  if (!first || first === last) return formatTime(last || first || "");
  return `${formatTime(first)} — ${formatTime(last || first)}`;
}


function observationMatchId(filename: string, explicitMatchId?: string): string {
  if (explicitMatchId) return String(explicitMatchId).trim();
  return filename.toLowerCase().endsWith(".jsonl") ? filename.slice(0, -6) : "";
}
