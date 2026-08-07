import { Button } from "@fluentui/react-components";
import { CheckCircle, Pause, Play, WarningCircle } from "@phosphor-icons/react";

import type {
  AlertIncident,
  ControlComponent,
  ControlResult,
  HealthItem,
  MappingRecord,
  MonitorMatch,
} from "../types";


const RETAINED_HEALTH_COMPONENTS = new Set([
  "postmatch_worker",
  "raybet_full_odds_worker",
  "raybet_priority_odds_worker",
  "raybet_worker",
  "strict_ingest_worker",
  "vision_worker",
]);


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
  const retainedHealth = health.filter((item) => (
    RETAINED_HEALTH_COMPONENTS.has(item.component)
  ));
  const manualSource = activeMappings.find((mapping) => (
    mapping.acceptance_mode === "manual_exact" && mapping.evidence_approval_id
  ));
  const nextMap = manualSource && match?.best_of
    ? Array.from({ length: match.best_of }, (_, index) => index + 1)
      .find((map) => !activeMappings.some((mapping) => mapping.map_number === map))
    : undefined;

  return (
    <main className="operations-page">
      <section className="workspace-section">
        <div className="section-heading compact">
          <div><h2>运行控制</h2><p>仅保留 RayBet 采集和实时产品所需进程。</p></div>
        </div>
        <div className="worker-list">
          {components.map((component) => (
            <div className="worker-row managed-worker" key={component.component}>
              <div><strong>{component.label}</strong><span>{component.pid ? `PID ${component.pid}` : component.detail || "未运行"}</span></div>
              <div className="worker-actions">
                <Button
                  disabled={!controlsEnabled || !component.control_allowed || component.status === "running" || busyKey !== null}
                  icon={<Play size={15} />}
                  onClick={() => onControl(component.component, "start")}
                >启动</Button>
                <Button
                  disabled={!controlsEnabled || !component.control_allowed || component.status !== "running" || busyKey !== null}
                  icon={<Pause size={15} />}
                  onClick={() => onControl(component.component, "stop")}
                >停止</Button>
              </div>
            </div>
          ))}
          {!components.length && <div className="subtle-empty compact">控制会话未建立</div>}
        </div>
        {controlMessage && <p className="live-form-message" role="status">{controlMessage}</p>}
      </section>

      <section className="workspace-section">
        <div className="section-heading compact">
          <div><h2>Strict mapping</h2><p>{match ? `RayBet #${match.raybet_match_id}` : "请先选择赛事"}</p></div>
        </div>
        {activeMappings.map((mapping) => (
          <div className="mapping-row" key={mapping.mapping_id}>
            <div>
              <strong>Map {mapping.map_number}</strong>
              <span>{mapping.canonical_teams.map((team) => team.name).join(" vs ")}</span>
              <small>{mapping.acceptance_mode}</small>
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
        {!activeMappings.length && <div className="subtle-empty compact">尚无已确认 mapping</div>}
      </section>

      <section className="workspace-section">
        <div className="section-heading compact"><div><h2>运行健康</h2><p>直播、采集与 Vision 故障保持显式。</p></div></div>
        <div className="worker-list">
          {retainedHealth.map((item) => (
            <div className="worker-row" key={item.component}>
              {item.status === "healthy" ? <CheckCircle size={17} /> : <WarningCircle size={17} />}
              <strong>{item.component}</strong><span>{item.last_error || item.status}</span>
            </div>
          ))}
        </div>
      </section>

      <section className="workspace-section">
        <div className="section-heading compact"><div><h2>Operational alerts</h2><p>只显示当前运行链故障。</p></div></div>
        {alerts.filter((alert) => alert.category === "operational").map((alert) => (
          <div className="alert-row" key={alert.incident_id}>
            <div><strong>{alert.title}</strong><span>{alert.body}</span></div>
            {!alert.acknowledged_at && <Button onClick={() => onAcknowledge(alert.incident_id)}>确认</Button>}
          </div>
        ))}
      </section>
    </main>
  );
}
