import { Button } from "@fluentui/react-components";
import {
  CheckCircle,
  Database,
  Eye,
  LockSimple,
  Pause,
  Play,
  Pulse,
  WarningCircle,
} from "@phosphor-icons/react";

import { formatDateTime } from "../format";
import type {
  AlertIncident,
  ControlComponent,
  ControlResult,
  HealthItem,
  MappingRecord,
  MonitorMatch,
} from "../types";
import { RelativeAge } from "./RelativeAge";


const RETAINED_HEALTH_COMPONENTS = new Set([
  "map_decision_worker",
  "postmatch_worker",
  "raybet_full_odds_worker",
  "raybet_priority_odds_worker",
  "raybet_worker",
  "strict_ingest_worker",
  "vision_worker",
]);

const CONTROL_COPY: Record<ControlComponent["component"], {
  description: string;
  healthComponent: string;
  label: string;
}> = {
  raybet_collector: {
    description: "发现赛事并协调赛前、实时和赛后赔率采集。",
    healthComponent: "raybet_worker",
    label: "RayBet 采集服务",
  },
  vision_supervisor: {
    description: "管理直播画面抓取、HUD 识别和证据留存。",
    healthComponent: "vision_worker",
    label: "Stable Vision",
  },
};

const DOWNSTREAM_COPY: Record<string, { description: string; label: string }> = {
  map_decision_worker: {
    description: "在 BP 锁定及每个五分钟节点生成可追溯的 shadow 决策或明确 skip。",
    label: "Map 决策检查点",
  },
  postmatch_worker: {
    description: "精确绑定官方 Match ID，并同步 OpenDota 赛后详情。",
    label: "赛后数据同步",
  },
  strict_ingest_worker: {
    description: "同步正式赛果和官方比赛身份。",
    label: "正式赛果入库",
  },
};

type SystemTone = "healthy" | "warning" | "critical" | "neutral";


interface OperationsPanelProps {
  alerts: AlertIncident[];
  busyKey: string | null;
  components: ControlComponent[];
  controlMessage: string | null;
  health: HealthItem[];
  mappings: MappingRecord[];
  match: MonitorMatch | null;
  controlsEnabled?: boolean;
  onAcknowledge: (incidentId: number) => void;
  onApproveMapping: (mappingId: number) => void;
  onControl: (component: ControlComponent["component"], action: ControlResult["action"]) => void;
  onCreateAutomaticMap: (sourceMappingId: number, mapNumber: number) => void;
  onInvalidateMapping: (mappingId: number) => void;
}


export function OperationsPanel({
  alerts,
  busyKey,
  components,
  controlMessage,
  health,
  mappings,
  match,
  controlsEnabled = false,
  onAcknowledge,
  onApproveMapping,
  onControl,
  onCreateAutomaticMap,
  onInvalidateMapping,
}: OperationsPanelProps) {
  const activeMappings = mappings.filter((mapping) => !mapping.invalidation);
  const retainedHealth = health.filter((item) => RETAINED_HEALTH_COMPONENTS.has(item.component));
  const healthByComponent = new Map(retainedHealth.map((item) => [item.component, item]));
  const priorityHealth = healthByComponent.get("raybet_priority_odds_worker") || null;
  const regularHealth = healthByComponent.get("raybet_full_odds_worker") || null;
  const coordinatorHealth = healthByComponent.get("raybet_worker") || null;
  const childChannelIssues = [priorityHealth, regularHealth].filter(isHealthIssue).length;
  const rootHealthIssues = retainedHealth.filter((item) => (
    isHealthIssue(item)
    && !(item.component === "raybet_worker" && childChannelIssues > 0)
  ));
  const controlIssues = components.filter((component) => component.status !== "running");
  const attentionCount = rootHealthIssues.length + controlIssues.length;
  const controllableCount = components.filter((component) => component.control_allowed).length;
  const healthyHeartbeatCount = retainedHealth.filter((item) => !isHealthIssue(item)).length;
  const operationalAlerts = alerts.filter((alert) => alert.category === "operational");
  const manualSource = activeMappings.find((mapping) => (
    mapping.acceptance_mode === "manual_exact" && mapping.evidence_approval_id
  ));
  const nextMap = manualSource && match?.best_of
    ? Array.from({ length: match.best_of }, (_, index) => index + 1)
      .find((map) => !activeMappings.some((mapping) => mapping.map_number === map))
    : undefined;

  return (
    <main className="operations-page system-control-page">
      <header className="system-control-overview">
        <div className="system-control-title">
          <h1>系统控制</h1>
          <p>控制顶层服务，定位内部采集通道，并查看数据链路与告警。</p>
        </div>
        <div className={`system-overview-state ${attentionCount ? "warning" : "healthy"}`}>
          {attentionCount
            ? <WarningCircle size={22} aria-hidden="true" />
            : <CheckCircle size={22} aria-hidden="true" />}
          <div>
            <span>{attentionCount ? "系统需要处理" : "系统运行正常"}</span>
            <strong>{attentionCount ? `${attentionCount} 项状态需关注` : "没有待处理状态"}</strong>
          </div>
        </div>
        <dl className="system-control-summary">
          <div><dt>顶层服务</dt><dd>{components.length}</dd></div>
          <div><dt>可直接控制</dt><dd>{controllableCount}</dd></div>
          <div><dt>健康心跳</dt><dd>{healthyHeartbeatCount}/{retainedHealth.length}</dd></div>
          <div><dt>未确认告警</dt><dd>{operationalAlerts.filter((item) => !item.acknowledged_at).length}</dd></div>
        </dl>
      </header>

      <section className="workspace-section control-section process-control-section" aria-label="顶层服务控制">
        <ControlSectionHeading
          description="只有这里的服务可以启动或停止。内部采集通道由 RayBet 服务自动管理。"
          title="顶层服务控制"
        />
        <div className="managed-service-grid">
          {components.map((component) => (
            <ManagedService
              busyKey={busyKey}
              component={component}
              controlsEnabled={controlsEnabled}
              health={healthByComponent.get(CONTROL_COPY[component.component].healthComponent) || null}
              key={component.component}
              onControl={onControl}
            />
          ))}
          {!components.length && (
            <div className="control-empty-state">控制会话未建立，当前页面保持只读。</div>
          )}
        </div>
        {controlMessage && <p className="control-result-message" role="status">{controlMessage}</p>}
      </section>

      <section className="workspace-section control-section raybet-pipeline-section" aria-label="RayBet 采集链路">
        <ControlSectionHeading
          description="一个采集服务内部包含赛事调度和两条互斥赔率通道，不是三套独立采集器。"
          title="RayBet 采集链路"
        />
        <div className="raybet-pipeline">
          <article className={`pipeline-coordinator ${healthTone(coordinatorHealth)}`}>
            <div className="pipeline-node-heading">
              <Pulse size={20} aria-hidden="true" />
              <div>
                <h3>赛事发现与调度</h3>
                <p>刷新直播、赛前和已结束赛事，并协调赔率通道。</p>
                <code>raybet_worker</code>
              </div>
              <HealthStatus item={coordinatorHealth} />
            </div>
            <dl className="pipeline-metrics">
              <Metric label="赛事列表" value={nestedDetailText(coordinatorHealth, "live_list_cache", "state") || "-"} />
              <Metric label="本轮委派" value={numberDetail(coordinatorHealth, "delegated")} />
              <Metric label="最近心跳" value={<RelativeAge observedAt={coordinatorHealth?.last_heartbeat_at} />} />
            </dl>
            <p className="pipeline-explanation">
              {coordinatorExplanation(coordinatorHealth, childChannelIssues)}
            </p>
          </article>

          <div className="pipeline-channels" aria-label="赔率采集通道">
            <OddsChannel
              description="已完成 Strict mapping 且正在直播的比赛，使用短周期刷新。"
              item={priorityHealth}
              label="优先赔率通道"
              technicalName="raybet_priority_odds_worker"
              type="priority"
            />
            <OddsChannel
              description="采集其余比赛。优先通道不可用时自动接管全部比赛。"
              item={regularHealth}
              label="常规赔率通道"
              technicalName="raybet_full_odds_worker"
              type="regular"
            />
          </div>
        </div>
      </section>

      <section className="workspace-section control-section mapping-control-section">
        <ControlSectionHeading
          description={match
            ? match.display_name || `RayBet ${match.raybet_match_id}`
            : "请先从左侧选择一场赛事。"}
          title="比赛身份映射"
        />
        <div className="mapping-control-list">
          {activeMappings.map((mapping) => (
            <div className="mapping-row" key={mapping.mapping_id}>
              <div>
                <strong>Map {mapping.map_number}</strong>
                <span>{mapping.canonical_teams.map((team) => team.name).join(" vs ")}</span>
                <small>{mapping.acceptance_mode === "manual_exact" ? "人工精确确认" : "自动精确匹配"}</small>
              </div>
              <div className="mapping-actions">
                {mapping.acceptance_mode === "automatic_exact" && !mapping.evidence_approval_id && (
                  <Button onClick={() => onApproveMapping(mapping.mapping_id)}>确认</Button>
                )}
                <Button onClick={() => onInvalidateMapping(mapping.mapping_id)}>作废</Button>
              </div>
            </div>
          ))}
          {manualSource && nextMap && (
            <Button onClick={() => onCreateAutomaticMap(manualSource.mapping_id, nextMap)}>
              为 Map {nextMap} 复用已确认队伍身份
            </Button>
          )}
          {!activeMappings.length && <div className="control-empty-state">尚无已确认的比赛身份映射。</div>}
        </div>
      </section>

      <section className="workspace-section control-section downstream-section" aria-label="下游数据链路">
        <ControlSectionHeading
          description="这些服务不在本页直接启停，只显示最新心跳与根因状态。"
          title="下游数据链路"
        />
        <div className="downstream-list">
          {Object.entries(DOWNSTREAM_COPY).map(([component, copy]) => (
            <DownstreamService
              copy={copy}
              item={healthByComponent.get(component) || null}
              key={component}
              technicalName={component}
            />
          ))}
        </div>
      </section>

      <section className="workspace-section control-section alerts-section" aria-label="运行告警">
        <ControlSectionHeading
          description="告警只描述当前运行链问题，确认操作不会停止服务。"
          title="运行告警"
        />
        <div className="system-alert-list">
          {operationalAlerts.map((alert) => (
            <div className={`system-alert ${alert.severity}`} key={alert.incident_id}>
              <WarningCircle size={19} aria-hidden="true" />
              <div>
                <strong>{alert.title}</strong>
                <span>{alert.body}</span>
              </div>
              {alert.acknowledged_at ? (
                <span className="alert-acknowledged">已确认</span>
              ) : (
                <Button onClick={() => onAcknowledge(alert.incident_id)}>确认告警</Button>
              )}
            </div>
          ))}
          {!operationalAlerts.length && (
            <div className="control-empty-state healthy">
              <CheckCircle size={18} aria-hidden="true" />
              当前没有运行告警。
            </div>
          )}
        </div>
      </section>
    </main>
  );
}


function ControlSectionHeading({ description, title }: { description: string; title: string }) {
  return (
    <div className="control-section-heading">
      <h2>{title}</h2>
      <p>{description}</p>
    </div>
  );
}


function ManagedService({
  busyKey,
  component,
  controlsEnabled,
  health,
  onControl,
}: {
  busyKey: string | null;
  component: ControlComponent;
  controlsEnabled: boolean;
  health: HealthItem | null;
  onControl: OperationsPanelProps["onControl"];
}) {
  const copy = CONTROL_COPY[component.component];
  const unavailableReason = controlUnavailableReason(component, controlsEnabled);
  const startDisabled = Boolean(unavailableReason)
    || component.status === "running"
    || busyKey !== null;
  const stopDisabled = Boolean(unavailableReason)
    || component.status !== "running"
    || busyKey !== null;
  const tone = controlTone(component, health);

  return (
    <article className={`managed-service ${tone}`}>
      <header>
        <div className="managed-service-identity">
          {component.component === "raybet_collector"
            ? <Database size={22} aria-hidden="true" />
            : <Eye size={22} aria-hidden="true" />}
          <div><h3>{copy.label}</h3><p>{copy.description}</p></div>
        </div>
        <ControlStatus component={component} health={health} />
      </header>
      <dl className="managed-service-facts">
        <div><dt>进程</dt><dd>{processIdentityText(component)}</dd></div>
        <div><dt>启动记录</dt><dd>{formatDateTime(component.started_at)}</dd></div>
        <div><dt>工作心跳</dt><dd><RelativeAge observedAt={health?.last_heartbeat_at} /></dd></div>
      </dl>
      {unavailableReason && (
        <div className="control-lock-note" role="note">
          <LockSimple size={16} aria-hidden="true" />
          <span>{unavailableReason}</span>
        </div>
      )}
      <footer className="managed-service-actions">
        <Button
          aria-label={`启动 ${copy.label}`}
          disabled={startDisabled}
          icon={<Play size={15} />}
          onClick={() => onControl(component.component, "start")}
          title={unavailableReason || undefined}
        >启动</Button>
        <Button
          aria-label={`停止 ${copy.label}`}
          disabled={stopDisabled}
          icon={<Pause size={15} />}
          onClick={() => onControl(component.component, "stop")}
          title={unavailableReason || undefined}
        >停止</Button>
      </footer>
    </article>
  );
}


function ControlStatus({ component, health }: { component: ControlComponent; health: HealthItem | null }) {
  const tone = controlTone(component, health);
  const label = component.status === "running" && isHealthIssue(health)
    ? "运行中，健康降级"
    : controlStatusLabel(component.status);
  return <SystemStatus label={label} tone={tone} />;
}


function OddsChannel({
  description,
  item,
  label,
  technicalName,
  type,
}: {
  description: string;
  item: HealthItem | null;
  label: string;
  technicalName: string;
  type: "priority" | "regular";
}) {
  return (
    <article className={`odds-channel ${healthTone(item)}`}>
      <div className="pipeline-node-heading">
        <Database size={19} aria-hidden="true" />
        <div><h3>{label}</h3><p>{description}</p><code>{technicalName}</code></div>
        <HealthStatus item={item} />
      </div>
      <dl className="pipeline-metrics">
        <Metric label="轮询间隔" value={formatInterval(numberDetail(item, "interval_seconds"))} />
        <Metric label="本轮分配" value={numberDetail(item, "listed")} />
        <Metric label="本轮成功" value={numberDetail(item, "matches")} />
      </dl>
      <p className="pipeline-explanation">{channelExplanation(item, type)}</p>
    </article>
  );
}


function DownstreamService({
  copy,
  item,
  technicalName,
}: {
  copy: { description: string; label: string };
  item: HealthItem | null;
  technicalName: string;
}) {
  return (
    <article className={`downstream-service ${healthTone(item)}`}>
      <div className="health-identity">
        {healthTone(item) === "healthy"
          ? <CheckCircle size={19} aria-hidden="true" />
          : <WarningCircle size={19} aria-hidden="true" />}
        <div><h3>{copy.label}</h3><p>{copy.description}</p><code>{technicalName}</code></div>
      </div>
      <div className="downstream-state">
        <HealthStatus item={item} />
        <RelativeAge observedAt={item?.last_heartbeat_at} />
      </div>
    </article>
  );
}


function HealthStatus({ item }: { item: HealthItem | null }) {
  return <SystemStatus label={healthStatusLabel(item)} tone={healthTone(item)} />;
}


function SystemStatus({ label, tone }: { label: string; tone: SystemTone }) {
  return (
    <span className={`system-status ${tone}`}>
      {tone === "healthy"
        ? <CheckCircle size={14} aria-hidden="true" />
        : tone === "neutral" ? <Pulse size={14} aria-hidden="true" /> : <WarningCircle size={14} aria-hidden="true" />}
      {label}
    </span>
  );
}


function Metric({ label, value }: { label: string; value: React.ReactNode }) {
  return <div><dt>{label}</dt><dd>{value == null ? "-" : value}</dd></div>;
}


function controlUnavailableReason(component: ControlComponent, controlsEnabled: boolean): string | null {
  if (!controlsEnabled) return "控制会话未建立，当前页面只能查看状态。";
  if (component.status === "identity_mismatch") {
    return "检测到现存进程，但命令身份与当前 master 不一致。为避免停止错误进程，控制已锁定。";
  }
  if (component.status === "identity_unverifiable") {
    return "无法验证现存进程身份。为避免误操作，控制已锁定。";
  }
  if (!component.control_allowed) return "当前服务不允许从页面控制。";
  return null;
}


function controlStatusLabel(status: ControlComponent["status"]): string {
  return {
    identity_mismatch: "进程身份不匹配",
    identity_unverifiable: "进程身份无法验证",
    running: "运行中",
    stopped: "已停止",
  }[status];
}


function processIdentityText(component: ControlComponent): string {
  if (component.pid) return `PID ${component.pid}`;
  if (component.status === "identity_mismatch") return "PID 无法匹配";
  if (component.status === "identity_unverifiable") return "PID 无法验证";
  return "未发现进程";
}


function controlTone(component: ControlComponent, health: HealthItem | null): SystemTone {
  if (component.status === "identity_mismatch" || component.status === "identity_unverifiable") return "critical";
  if (component.status === "stopped") return "neutral";
  return healthTone(health);
}


function healthTone(item: HealthItem | null): SystemTone {
  if (!item) return "neutral";
  if (item.freshness === "missing" || item.status === "unhealthy" || item.status === "invalid") return "critical";
  if (item.freshness !== "fresh" || item.status !== "healthy") return "warning";
  return "healthy";
}


function healthStatusLabel(item: HealthItem | null): string {
  if (!item || item.freshness === "missing") return "无心跳";
  if (item.freshness === "stale") return "心跳过期";
  if (item.freshness === "delayed") return "心跳延迟";
  return {
    degraded: "降级",
    delayed: "延迟",
    healthy: "正常",
    invalid: "无效",
    missing: "无数据",
    ready: "就绪",
    stale: "已过期",
    starting: "启动中",
    stopped: "已停止",
    unhealthy: "异常",
    unconfirmed: "待确认",
  }[item.status] || item.status;
}


function isHealthIssue(item: HealthItem | null): boolean {
  return Boolean(item && (item.freshness !== "fresh" || item.status !== "healthy"));
}


function coordinatorExplanation(item: HealthItem | null, childChannelIssues: number): string {
  if (!item) return "尚未收到赛事调度心跳。";
  if (item.freshness !== "fresh") return "赛事调度心跳不新鲜，请检查采集进程。";
  if (childChannelIssues > 0) return `主循环心跳正常，${childChannelIssues} 条子通道异常已在下方定位。`;
  if (item.status !== "healthy") return "赛事发现或调度发生异常，请检查服务日志。";
  return "赛事列表刷新与通道调度正常。";
}


function channelExplanation(item: HealthItem | null, type: "priority" | "regular"): string {
  if (!item) return "尚未收到通道心跳。";
  if (item.freshness !== "fresh") return "通道心跳不新鲜，请检查 RayBet 采集进程。";
  const errors = numberDetail(item, "errors") || 0;
  if (errors > 0) {
    const failedMatchIds = stringListDetail(item, "failed_match_ids");
    const failedMatches = failedMatchIds.length
      ? `：${failedMatchIds.join("、")}`
      : "";
    return `${errors} 场比赛在本轮采集失败${failedMatches}，下一轮将自动重试。`;
  }
  const listed = numberDetail(item, "listed") || 0;
  if (type === "priority" && listed === 0) {
    return "当前没有已锁定且正在直播的比赛，通道处于正常空闲状态。";
  }
  if (item.status === "healthy") return "本轮采集正常完成。";
  return "通道状态异常，请检查 RayBet 响应和退避记录。";
}


function numberDetail(item: HealthItem | null, key: string): number | null {
  const value = item?.details[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}


function stringListDetail(item: HealthItem | null, key: string): string[] {
  const value = item?.details[key];
  if (!Array.isArray(value)) return [];
  return value.filter((entry): entry is string => typeof entry === "string" && entry.length > 0);
}


function nestedDetailText(item: HealthItem | null, parent: string, key: string): string | null {
  const value = item?.details[parent];
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const nested = (value as Record<string, unknown>)[key];
  return typeof nested === "string" ? nested : null;
}


function formatInterval(value: number | null): string {
  return value == null ? "-" : `${value.toLocaleString("zh-CN")} 秒`;
}
