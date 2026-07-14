import { Button } from "@fluentui/react-components";
import {
  Bell,
  Brain,
  Broadcast,
  CheckCircle,
  CloudArrowDown,
  EnvelopeSimple,
  Eye,
  GitMerge,
  Pause,
  Play,
  WarningCircle,
} from "@phosphor-icons/react";
import type { Icon } from "@phosphor-icons/react";

import { formatAge } from "../format";
import type { FreshnessState, HealthItem, MonitorMatch, ReadinessStatus } from "../types";
import { ReadinessBadge } from "./StatusBadge";

interface OperationsPanelProps {
  health: HealthItem[];
  match: MonitorMatch | null;
  controlsEnabled?: boolean;
  onControl?: (component: string, action: "start" | "stop" | "restart") => void;
}

const readinessRows: Array<{
  key: keyof MonitorMatch["readiness"];
  label: string;
  icon: Icon;
}> = [
  { key: "odds", label: "赔率采集", icon: CloudArrowDown },
  { key: "mapping", label: "赛事映射", icon: GitMerge },
  { key: "vision", label: "视觉观测", icon: Eye },
  { key: "model", label: "模型判断", icon: Brain },
  { key: "strategy", label: "纸面策略", icon: Broadcast },
];

export function OperationsPanel({
  health,
  match,
  controlsEnabled = false,
  onControl,
}: OperationsPanelProps) {
  const workers = health.filter((item) => item.component.endsWith("_worker"));
  const alerts = buildAlerts(health, match);

  return (
    <aside className="operations-panel" aria-label="就绪状态与操作">
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

      <section className="operations-section">
        <div className="operations-heading">
          <div>
            <h2>工作进程</h2>
            <span>状态按心跳重新计算</span>
          </div>
        </div>
        <div className="worker-list">
          {workers.map((worker) => (
            <div className="worker-row" key={worker.component}>
              <div>
                <strong>{workerName(worker.component)}</strong>
                <span>{formatAge(worker.age_seconds)}</span>
              </div>
              <ReadinessBadge status={healthStatus(worker.status)} />
            </div>
          ))}
          {!workers.length && <div className="subtle-empty">没有工作进程心跳</div>}
        </div>
        <div className="control-grid">
          <Button
            disabled={!controlsEnabled}
            icon={<Play size={16} />}
            onClick={() => onControl?.("raybet", "start")}
            size="small"
            title={controlsEnabled ? "启动采集进程" : "控制服务尚未启用"}
          >
            启动采集
          </Button>
          <Button
            disabled={!controlsEnabled}
            icon={<Pause size={16} />}
            onClick={() => onControl?.("raybet", "stop")}
            size="small"
            title={controlsEnabled ? "停止采集进程" : "控制服务尚未启用"}
          >
            停止采集
          </Button>
        </div>
      </section>

      <section className="operations-section alert-section">
        <div className="operations-heading">
          <div>
            <h2>当前告警</h2>
            <span>{alerts.length} 项需要关注</span>
          </div>
          <Bell size={20} aria-hidden="true" />
        </div>
        <div className="alert-list">
          {alerts.slice(0, 8).map((alert) => (
            <div className={`alert-row ${alert.severity}`} key={alert.key}>
              <WarningCircle size={17} aria-hidden="true" />
              <div>
                <strong>{alert.title}</strong>
                <span>{alert.body}</span>
              </div>
            </div>
          ))}
          {!alerts.length && (
            <div className="all-clear">
              <CheckCircle size={18} aria-hidden="true" />
              当前没有活动告警
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

function healthStatus(value: HealthItem["status"]): ReadinessStatus {
  if (value === "healthy") return "ready";
  if (value === "starting") return "delayed";
  return value;
}

function workerName(component: string): string {
  return {
    raybet_worker: "赔率采集",
    shadow_worker: "纸面策略",
    mail_worker: "邮件投递",
  }[component] || component;
}

function mailState(health: HealthItem[]): string {
  const mail = health.find((item) => item.component === "mail_worker")
    || health.find((item) => item.component === "mail");
  if (!mail) return "未配置";
  if (mail.status === "healthy") return "已连接";
  if (mail.last_error === "configuration_missing" || mail.status === "stopped") return "未配置或未启动";
  return `不可用: ${mail.last_error || mail.status}`;
}

function buildAlerts(health: HealthItem[], match: MonitorMatch | null) {
  const alerts: Array<{ key: string; severity: "critical" | "warning"; title: string; body: string }> = [];
  for (const worker of health.filter((item) => item.component.endsWith("_worker"))) {
    if (worker.status === "healthy") continue;
    alerts.push({
      key: `worker-${worker.component}`,
      severity: worker.status === "unhealthy" ? "critical" : "warning",
      title: `${workerName(worker.component)}${worker.status === "stopped" ? "已停止" : "心跳异常"}`,
      body: worker.age_seconds == null ? "未收到心跳" : `最后心跳 ${formatAge(worker.age_seconds)}`,
    });
  }
  if (match) {
    for (const [key, state] of Object.entries(match.readiness)) {
      if (state.status === "ready") continue;
      alerts.push({
        key: `match-${match.raybet_match_id}-${key}`,
        severity: state.status === "stale" || state.status === "unhealthy" ? "critical" : "warning",
        title: `${readinessRows.find((item) => item.key === key)?.label || key}未就绪`,
        body: freshnessText(state),
      });
    }
  }
  return alerts;
}
