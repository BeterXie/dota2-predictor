import { Button } from "@fluentui/react-components";
import {
  ArrowClockwise,
  Bell,
  CheckCircle,
  CloudArrowDown,
  EnvelopeSimple,
  GitMerge,
  Pause,
  Play,
  PlusCircle,
  SealCheck,
  WarningCircle,
  XCircle,
} from "@phosphor-icons/react";
import type { Icon } from "@phosphor-icons/react";

import { formatAge } from "../format";
import type {
  AlertIncident,
  ControlComponent,
  ControlResult,
  FreshnessState,
  HealthItem,
  MappingRecord,
  MonitorMatch,
  ReadinessStatus,
} from "../types";
import { ReadinessBadge } from "./StatusBadge";
import { PipelineTopology } from "./operations/PipelineTopology";

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
  onControl: (
    component: ControlComponent["component"],
    action: ControlResult["action"],
  ) => void;
  onCreateAutomaticMap: (sourceMappingId: number, mapNumber: number) => void;
  onInvalidateMapping: (mappingId: number) => void;
}

const readinessRows: Array<{
  key: keyof MonitorMatch["readiness"];
  label: string;
  icon: Icon;
}> = [
  { key: "odds", label: "赔率采集", icon: CloudArrowDown },
  { key: "mapping", label: "赛事映射", icon: GitMerge },
];

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
  const activeAlertCount = alerts.filter((alert) => !alert.acknowledged_at).length;
  const readinessStates = match
    ? readinessRows.map(({ key }) => match.readiness[key])
    : [];
  const readyCount = readinessStates.filter((item) => item.status === "ready").length;
  const chainReady = Boolean(match) && readyCount === readinessRows.length;
  const visibleAlerts = [...alerts]
    .sort((left, right) => Number(Boolean(left.acknowledged_at))
      - Number(Boolean(right.acknowledged_at)))
    .slice(0, 8);
  const approvedSource = activeMappings.find(
    (mapping) => mapping.acceptance_mode === "manual_exact" && mapping.evidence_approval_id,
  );
  const nextMap = approvedSource && match?.best_of
    ? Array.from({ length: match.best_of }, (_, index) => index + 1)
      .find((number) => !activeMappings.some((mapping) => mapping.map_number === number))
    : undefined;
  return (
    <aside className="operations-panel" aria-label="就绪状态与操作">
      <PipelineTopology match={match} />
      <section className={`operations-overview ${chainReady ? "ready" : "degraded"}`} aria-label="数据链路与活动告警结论">
        <div>
          {chainReady
            ? <CheckCircle size={21} weight="fill" aria-hidden="true" />
            : <WarningCircle size={21} weight="fill" aria-hidden="true" />}
          <div>
            <strong>{chainReady ? "数据链路可用于决策" : "数据链路尚不可用于决策"}</strong>
            <span>{match
              ? chainReady ? "当前赛事的关键数据链路均已就绪。" : "进程运行不代表数据可用于决策。"
              : "未选择赛事，无法判断数据链路可用性。"}</span>
          </div>
        </div>
        <dl>
          <div><dt>链路就绪</dt><dd>{readyCount}/{readinessRows.length}</dd></div>
          <div>
            <dt>活动告警</dt>
            <dd>{activeAlertCount}<small>仅表示当前已触发且未确认的告警</small></dd>
          </div>
        </dl>
      </section>

      <section className="operations-section">
        <div className="operations-heading">
          <div>
            <h2>就绪链路</h2>
            <span>{match ? match.raybet_match_id : "未选择赛事"}</span>
          </div>
          {match && Object.values(match.readiness).every((item) => item.status === "ready")
            ? <CheckCircle size={21} className="healthy-icon" aria-hidden="true" />
            : <WarningCircle size={21} className="warning-icon" aria-hidden="true" />}
        </div>
        <div className="readiness-list">
          {readinessRows.map(({ key, label, icon: Icon }) => {
            const state = match?.readiness[key] || { status: "missing" as const };
            return (
              <div className="readiness-row" key={key}>
                <Icon size={18} aria-hidden="true" />
                <div>
                  <strong>{label}</strong>
                  <span>{freshnessText(state)}</span>
                </div>
                <ReadinessBadge status={state.status} />
              </div>
            );
          })}
        </div>
      </section>

      <section className="operations-section mapping-section">
        <div className="operations-heading">
          <div>
            <h2>Exact 映射</h2>
            <span>{activeMappings.length} 条有效证据</span>
          </div>
          <GitMerge size={19} aria-hidden="true" />
        </div>
        <div className="mapping-list">
          {mappings.map((mapping) => (
            <details
              className={`mapping-row ${mapping.invalidation ? "invalidated" : ""}`}
              key={mapping.mapping_id}
            >
              <summary>
                <span>第 {mapping.map_number} 局</span>
                <code>{mapping.acceptance_mode}</code>
                <ReadinessBadge status={mapping.invalidation ? "invalid" : "ready"} />
              </summary>
              <dl>
                <div><dt>赛事</dt><dd>{mapping.event_id}</dd></div>
                <div>
                  <dt>队伍</dt>
                  <dd>{mapping.canonical_teams.map((team) => team.name).join(" / ")}</dd>
                </div>
                <div><dt>证据</dt><dd>{mapping.evidence_hash.slice(0, 12)}</dd></div>
              </dl>
              {mapping.invalidation && (
                <p className="mapping-invalid-reason">{mapping.invalidation.reason}</p>
              )}
              {!mapping.invalidation && (
                <div className="mapping-actions">
                  {mapping.acceptance_mode === "manual_exact" && !mapping.evidence_approval_id && (
                    <Button
                      appearance="subtle"
                      aria-label="批准自动 exact 证据"
                      disabled={!controlsEnabled || busyKey !== null}
                      icon={<SealCheck size={16} />}
                      onClick={() => onApproveMapping(mapping.mapping_id)}
                      size="small"
                      title="批准同系列后续局使用该 exact 证据"
                    />
                  )}
                  <Button
                    appearance="subtle"
                    aria-label="使映射失效"
                    disabled={!controlsEnabled || busyKey !== null}
                    icon={<XCircle size={16} />}
                    onClick={() => onInvalidateMapping(mapping.mapping_id)}
                    size="small"
                    title="追加失效记录"
                  />
                </div>
              )}
            </details>
          ))}
          {!mappings.length && <div className="subtle-empty compact">没有 exact 映射</div>}
        </div>
        {approvedSource && nextMap && (
          <Button
            appearance="subtle"
            className="automatic-map-button"
            disabled={!controlsEnabled || busyKey !== null}
            icon={<PlusCircle size={16} />}
            onClick={() => onCreateAutomaticMap(approvedSource.mapping_id, nextMap)}
            size="small"
          >
            登记第 {nextMap} 局
          </Button>
        )}
      </section>

      <section className="operations-section">
        <div className="operations-heading">
          <div>
            <h2>工作进程</h2>
            <span>状态按心跳重新计算</span>
          </div>
        </div>
        <div className="worker-list managed-workers">
          {components.map((component) => (
            <div className="worker-row managed-worker" key={component.component}>
              <div>
                <strong>{componentName(component.component)}</strong>
                <span>{component.pid ? `PID ${component.pid}` : component.detail || "未运行"}</span>
              </div>
              <ReadinessBadge status={controlStatus(component.status)} />
              <div className="worker-actions">
                <Button
                  appearance="subtle"
                  aria-label={`启动${componentName(component.component)}`}
                  disabled={!controlsEnabled || !component.control_allowed || busyKey !== null || component.status === "running"}
                  icon={<Play size={15} />}
                  onClick={() => onControl(component.component, "start")}
                  size="small"
                  title="启动"
                />
                <Button
                  appearance="subtle"
                  aria-label={`停止${componentName(component.component)}`}
                  disabled={!controlsEnabled || !component.control_allowed || busyKey !== null || component.status !== "running"}
                  icon={<Pause size={15} />}
                  onClick={() => onControl(component.component, "stop")}
                  size="small"
                  title="停止"
                />
                <Button
                  appearance="subtle"
                  aria-label={`重启${componentName(component.component)}`}
                  disabled={!controlsEnabled || !component.control_allowed || busyKey !== null || component.status !== "running"}
                  icon={<ArrowClockwise size={15} />}
                  onClick={() => onControl(component.component, "restart")}
                  size="small"
                  title="重启"
                />
              </div>
            </div>
          ))}
          {!components.length && <div className="subtle-empty compact">控制会话未建立</div>}
        </div>
        {controlMessage && <div className="control-message" role="status">{controlMessage}</div>}
      </section>

      <section className="operations-section alert-section">
        <div className="operations-heading">
          <div>
            <h2>活动告警</h2>
            <span>{activeAlertCount} 项未确认</span>
          </div>
          <Bell size={20} aria-hidden="true" />
        </div>
        <div className="alert-list">
          {visibleAlerts.map((alert) => (
            <div
              className={`alert-row ${alert.severity} ${alert.acknowledged_at ? "acknowledged" : ""}`}
              key={alert.incident_id}
            >
              <WarningCircle size={17} aria-hidden="true" />
              <div>
                <strong>{alert.title}</strong>
                <span>{alert.body}</span>
              </div>
              <Button
                appearance="subtle"
                aria-label={`确认告警：${alert.title}`}
                disabled={!controlsEnabled || Boolean(alert.acknowledged_at)}
                icon={<CheckCircle size={15} />}
                onClick={() => onAcknowledge(alert.incident_id)}
                size="small"
                title={alert.acknowledged_at ? "已确认" : "确认"}
              />
            </div>
          ))}
          {activeAlertCount === 0 && (
            <div className="all-clear">
              <CheckCircle size={18} aria-hidden="true" />
              <span>当前没有活动告警</span>
              <small>0 仅表示未触发活动告警，请同时检查数据链路。</small>
            </div>
          )}
        </div>
      </section>

      <section className="mail-state">
        <EnvelopeSimple size={17} aria-hidden="true" />
        <div>
          <strong>邮件通知</strong>
          <span>{mailState(health)}</span>
        </div>
      </section>
    </aside>
  );
}

function freshnessText(state: FreshnessState): string {
  if (typeof state.count === "number") return `${state.count} 条有效映射`;
  if (state.age_seconds != null) return formatAge(state.age_seconds);
  return "尚未收到证据";
}

function controlStatus(value: ControlComponent["status"]): ReadinessStatus {
  if (value === "running") return "ready";
  if (value === "identity_mismatch") return "invalid";
  return "stopped";
}

function componentName(component: ControlComponent["component"]): string {
  return {
    raybet_collector: "赔率采集",
    mail_worker: "邮件投递",
  }[component];
}

function mailState(health: HealthItem[]): string {
  const mailComponents = health.filter((item) => item.component.startsWith("mail"));
  const configurationMissing = mailComponents.some((item) => (
    item.last_error === "configuration_missing"
    || item.details.configured === false
    || item.details.smtp_configured === false
  ));
  if (configurationMissing) return "未配置";
  const mail = health.find((item) => item.component === "mail")
    || health.find((item) => item.component === "mail_worker");
  if (!mail) return "未配置";
  if (mail.status === "healthy") return "已连接";
  if (mail.last_error === "configuration_missing" || mail.status === "stopped") return "未配置或未启动";
  return `不可用: ${mail.last_error || mail.status}`;
}
