import { Button, Spinner } from "@fluentui/react-components";
import {
  CheckCircle,
  Flask,
  ImageSquare,
  LockKey,
  WarningCircle,
} from "@phosphor-icons/react";
import { useEffect, useMemo, useState } from "react";

import {
  buildVisionCalibrationCandidate,
  fetchHeroGrid,
  fetchVisionCalibration,
  runVisionCalibrationEvaluation,
  saveVisionCalibrationLabel,
} from "../api";
import type {
  PrematchHero,
  VisionCalibrationBootstrap,
  VisionCalibrationEvent,
  VisionCalibrationEvaluation,
} from "../types";


interface VisionCalibrationPageProps {
  csrfToken: string | null;
}


export function VisionCalibrationPage({ csrfToken }: VisionCalibrationPageProps) {
  const [data, setData] = useState<VisionCalibrationBootstrap | null>(null);
  const [heroes, setHeroes] = useState<PrematchHero[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [profileId, setProfileId] = useState("all");
  const [truth, setTruth] = useState<Array<number | "">>(Array(10).fill(""));
  const [matchId, setMatchId] = useState("");
  const [mapNumber, setMapNumber] = useState("");
  const [note, setNote] = useState("");
  const [candidateId, setCandidateId] = useState("");
  const [observationFile, setObservationFile] = useState("");
  const [layoutProfile, setLayoutProfile] = useState("");
  const [mode, setMode] = useState<"perception" | "runtime">("perception");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<"label" | "candidate" | "evaluation" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const reload = async (signal?: AbortSignal) => {
    const value = await fetchVisionCalibration(signal);
    setData(value);
    setSelectedId((current) => (
      current && value.events.some((event) => event.event_id === current)
        ? current
        : value.events[0]?.event_id || null
    ));
    setObservationFile((current) => current || value.observation_files[0]?.name || "");
  };

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([reload(controller.signal), fetchHeroGrid(controller.signal)])
      .then(([, grid]) => {
        setHeroes(Object.values(grid).flat().sort((one, two) => (
          one.localized_name.localeCompare(two.localized_name)
        )));
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
  const visibleEvents = useMemo(
    () => data?.events.filter((item) => profileId === "all" || item.profile_id === profileId) || [],
    [data?.events, profileId],
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
    const relatedCandidate = data?.candidates.find((item) => item.label_id === event.event_id);
    setCandidateId(relatedCandidate?.candidate_id || "");
    setMessage(null);
  }, [data?.candidates, data?.layout_profiles, event]);

  const truthReady = truth.every((value) => typeof value === "number")
    && new Set(truth).size === 10;
  const relatedCandidates = data?.candidates.filter((item) => item.profile_id === event?.profile_id) || [];
  const selectedCandidate = relatedCandidates.find((item) => item.candidate_id === candidateId);
  const canEvaluate = Boolean(
    csrfToken
    && event?.label
    && selectedCandidate
    && observationFile
    && layoutProfile,
  );

  const saveLabel = async () => {
    if (!csrfToken || !event || !truthReady) return;
    setBusy("label");
    setError(null);
    try {
      await saveVisionCalibrationLabel(event.event_id, {
        hero_ids: truth as number[],
        raybet_match_id: matchId.trim() || null,
        map_number: mapNumber ? Number(mapNumber) : null,
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
    if (!csrfToken || !event?.label) return;
    setBusy("candidate");
    setError(null);
    try {
      const candidate = await buildVisionCalibrationCandidate(event.label.label_id, csrfToken);
      await reload();
      setCandidateId(candidate.candidate_id);
      setMessage("已生成隔离候选包；需要留出评估后才能讨论推广。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "构建候选包失败");
    } finally {
      setBusy(null);
    }
  };

  const runEvaluation = async () => {
    if (!csrfToken || !event?.label || !canEvaluate) return;
    setBusy("evaluation");
    setError(null);
    try {
      const result = await runVisionCalibrationEvaluation({
        label_id: event.label.label_id,
        candidate_id: candidateId,
        observation_file: observationFile,
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
    return <main className="vision-calibration-loading"><Spinner label="正在读取 Vision corpus" /></main>;
  }

  return (
    <main className="vision-calibration-page">
      <header className="vision-calibration-header">
        <div>
          <span className="vision-kicker"><Flask size={16} /> REAL-FRAME CALIBRATION</span>
          <h1>Vision 训练与校正</h1>
          <p>先选择赛事 UI profile，再校正其中一场比赛；同 profile 的后续比赛可以复用候选并继续做留出验证。</p>
        </div>
        <div className="vision-boundary" role="note">
          <LockKey size={18} />
          <div><strong>生产边界锁定</strong><span>{data?.candidate_boundary}</span></div>
        </div>
      </header>

      {error && <div className="vision-feedback error" role="alert"><WarningCircle size={18} />{error}</div>}
      {message && <div className="vision-feedback success" role="status"><CheckCircle size={18} />{message}</div>}

      {!data?.events.length ? (
        <section className="vision-empty-state">
          <ImageSquare size={32} />
          <h2>还没有可校正的真实帧</h2>
          <p>Stable Vision 已随项目启动。出现直播且产生失败/边界诊断后，事件会自动进入这里。</p>
          <code>data/live_betting/vision_debug</code>
        </section>
      ) : (
        <div className="vision-calibration-grid">
          <section className="vision-event-queue" aria-label="Vision debug events">
            <header>
              <div><h2>待校正比赛</h2><span>{visibleEvents.length} retained</span></div>
              <label className="vision-profile-selector">
                <span>赛事 UI profile</span>
                <select
                  aria-label="赛事 UI profile"
                  onChange={(change) => setProfileId(change.target.value)}
                  value={profileId}
                >
                  <option value="all">全部赛事 UI</option>
                  {profiles.map((profile) => (
                    <option key={profile.profile_id} value={profile.profile_id}>
                      {profile.layout || profile.profile_id} · {profile.event_count} 场
                    </option>
                  ))}
                </select>
              </label>
              <p className="vision-profile-summary">
                {selectedProfile
                  ? `${selectedProfile.layout || selectedProfile.profile_id}：${selectedProfile.labeled_event_count} 场已标注，${selectedProfile.candidate_count} 个候选可复用`
                  : "先选择赛事 UI；同 profile 后续比赛可复用同一候选，不必重复构建。"}
              </p>
            </header>
            <div className="vision-event-list">
              {visibleEvents.map((item) => (
                <button
                  className={item.event_id === selectedId ? "vision-event selected" : "vision-event"}
                  key={item.event_id}
                  onClick={() => setSelectedId(item.event_id)}
                  type="button"
                >
                  <span className="vision-event-time">{formatTime(item.captured_at)}</span>
                  <strong>{item.reason}</strong>
                  <span>{item.layout || "unsupported layout"}</span>
                  <small>{item.label ? "已标注" : `${item.crop_count}/10 crops`} · {item.replay_gate_status || "gate unknown"}</small>
                </button>
              ))}
            </div>
          </section>

          {event && (
            <section className="vision-inspector">
              <header className="vision-section-heading">
                <div><h2>帧与槽位检查</h2><p>{event.relative_path} · profile: {event.profile_id}</p></div>
                <dl>
                  <div><dt>screen</dt><dd>{event.screen_state || "—"}</dd></div>
                  <div><dt>layout</dt><dd>{event.layout_state || "—"}</dd></div>
                  <div><dt>blocker</dt><dd>{event.blocker_code || event.reason}</dd></div>
                </dl>
              </header>
              <img className="vision-full-frame" src={event.frame_url} alt="选中 Vision debug event 的完整直播帧" />
              <div className="vision-crop-grid">
                {event.crop_urls.map((url, index) => {
                  const diagnostic = event.slot_diagnostics[index];
                  return (
                    <figure key={url}>
                      <img src={url} alt={`${index < 5 ? "Radiant" : "Dire"} ${index % 5 + 1} 英雄 crop`} />
                      <figcaption>
                        <strong>{index < 5 ? "R" : "D"}{index % 5 + 1}</strong>
                        <span>#{diagnostic?.best_hero_id || "—"}</span>
                        <small>{diagnostic ? `${diagnostic.best_score.toFixed(3)} / Δ${diagnostic.margin.toFixed(3)}` : "no diagnostic"}</small>
                      </figcaption>
                    </figure>
                  );
                })}
              </div>
            </section>
          )}

          {event && (
            <aside className="vision-calibration-form">
              <section>
                <div className="vision-section-heading compact"><div><h2>HUD 真值</h2><p>从左到右：Radiant 1–5，Dire 1–5。</p></div></div>
                <div className="vision-truth-grid">
                  {truth.map((value, index) => (
                    <label key={`${event.event_id}-${index}`}>
                      <span>{index < 5 ? "Radiant" : "Dire"} {index % 5 + 1}</span>
                      <select
                        aria-label={`${index < 5 ? "Radiant" : "Dire"} slot ${index % 5 + 1}`}
                        onChange={(change) => setTruth((current) => current.map((item, itemIndex) => (
                          itemIndex === index ? Number(change.target.value) || "" : item
                        )))}
                        value={value}
                      >
                        <option value="">选择英雄</option>
                        {heroes.map((hero) => <option key={hero.hero_id} value={hero.hero_id}>{hero.localized_name} · #{hero.hero_id}</option>)}
                      </select>
                    </label>
                  ))}
                </div>
                <div className="vision-context-fields">
                  <label><span>RayBet match ID</span><input onChange={(change) => setMatchId(change.target.value)} value={matchId} /></label>
                  <label><span>Map</span><input max="5" min="1" onChange={(change) => setMapNumber(change.target.value)} type="number" value={mapNumber} /></label>
                </div>
                <label className="vision-note"><span>标注说明</span><textarea onChange={(change) => setNote(change.target.value)} rows={2} value={note} /></label>
                {!truthReady && <p className="vision-disabled-reason">需要十个互不重复的英雄真值。</p>}
                <Button disabled={!csrfToken || !truthReady || busy !== null} onClick={() => void saveLabel()}>
                  {busy === "label" ? "正在保存…" : "保存真值标签"}
                </Button>
              </section>

              <section>
                <div className="vision-section-heading compact"><div><h2>候选模板</h2><p>仅替换隔离候选中的十个 base portrait。</p></div></div>
                <Button disabled={!csrfToken || !event.label || event.crop_count !== 10 || busy !== null} onClick={() => void buildCandidate()}>
                  {busy === "candidate" ? "正在构建…" : "从当前标签构建候选"}
                </Button>
                {!event.label && <p className="vision-disabled-reason">先保存当前事件的真值标签。</p>}
              </section>

              <section>
                <div className="vision-section-heading compact"><div><h2>留出评估</h2><p>默认 perception；runtime 会执行 OCR 与 trust gates。</p></div></div>
                <label><span>候选包（可复用同 UI profile）</span><select onChange={(change) => setCandidateId(change.target.value)} value={candidateId}><option value="">选择当前 profile 的候选</option>{relatedCandidates.map((item) => <option key={item.candidate_id} value={item.candidate_id}>{item.layout} · {formatTime(item.created_at)}</option>)}</select></label>
                <label>
                  <span>Observation JSONL</span>
                  <select
                    aria-label="Observation JSONL"
                    aria-describedby="vision-observation-help"
                    disabled={!data.observation_files.length}
                    onChange={(change) => setObservationFile(change.target.value)}
                    value={observationFile}
                  >
                    <option value="">
                      {data.observation_files.length ? "选择序列" : "没有可用序列"}
                    </option>
                    {data.observation_files.map((item) => (
                      <option key={item.name} value={item.name}>{item.name}</option>
                    ))}
                  </select>
                  <small className="vision-field-help" id="vision-observation-help">
                    {data.observation_files.length
                      ? `正在读取 ${data.observation_root}`
                      : `目录中还没有 JSONL：${data.observation_root}。Stable watcher 采集到直播帧后刷新页面。`}
                  </small>
                </label>
                <label><span>Layout profile</span><select onChange={(change) => setLayoutProfile(change.target.value)} value={layoutProfile}>{data.layout_profiles.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
                <label><span>评估模式</span><select onChange={(change) => setMode(change.target.value as "perception" | "runtime")} value={mode}><option value="perception">Perception only</option><option value="runtime">Runtime faithful</option></select></label>
                <Button disabled={!canEvaluate || busy !== null} onClick={() => void runEvaluation()}>
                  {busy === "evaluation" ? "正在评估…" : "运行留出评估"}
                </Button>
              </section>

              <EvaluationResults rows={data.evaluations} />
            </aside>
          )}
        </div>
      )}
    </main>
  );
}


function EvaluationResults({ rows }: { rows: VisionCalibrationEvaluation[] }) {
  return (
    <section>
      <div className="vision-section-heading compact"><div><h2>最近结果</h2><p>错误锁优先于平均分。</p></div></div>
      {!rows.length && <p className="vision-disabled-reason">尚无评估结果。</p>}
      <div className="vision-evaluation-list">
        {rows.slice(0, 6).map((row) => (
          <article className={row.wrong_lock_count ? "failed" : "passed"} key={row.evaluation_id}>
            <header><strong>{row.final_correct_locked_slots}/10 locked</strong><span>{row.mode}</span></header>
            <dl>
              <div><dt>wrong locks</dt><dd>{row.wrong_lock_count}</dd></div>
              <div><dt>precision</dt><dd>{formatPercent(row.accepted_precision)}</dd></div>
              <div><dt>post-lock</dt><dd>{formatPercent(row.exact_post_lock_rate)}</dd></div>
            </dl>
            <small>{row.observation_file}</small>
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
