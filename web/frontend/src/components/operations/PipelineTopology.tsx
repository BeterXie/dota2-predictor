import {
  CloudArrowDown,
  GitMerge,
} from "@phosphor-icons/react";
import type { MonitorMatch, ReadinessStatus } from "../../types";

interface PipelineTopologyProps {
  match: MonitorMatch | null;
}

export function PipelineTopology({ match }: PipelineTopologyProps) {
  const readiness = match?.readiness;
  const nodes = [
    { key: "odds", label: "赔率采集", icon: CloudArrowDown, status: readiness?.odds.status || "missing" },
    { key: "mapping", label: "赛事映射", icon: GitMerge, status: readiness?.mapping.status || "missing" },
  ];

  return (
    <div
      className="pipeline-topology-container"
      style={{
        display: "grid",
        gridColumn: "1 / -1",
        gap: "12px",
        padding: "16px 18px",
        marginBottom: "0",
        background: "rgba(17, 24, 32, 0.8)",
        borderBottom: "1px solid var(--border-accent, #2a3746)",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h3 style={{ margin: 0, fontSize: "14px", fontWeight: 650, color: "var(--text)" }}>数据链路实时拓扑</h3>
        <span style={{ fontSize: "11px", color: "var(--text-dim)", fontFamily: "var(--mono)" }}>
          {match ? `RayBet #${match.raybet_match_id}` : "未选中比赛"}
        </span>
      </div>

      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", overflowX: "auto", padding: "10px 0" }}>
        {nodes.map((node, index) => {
          const isReady = node.status === "ready";
          const isWarning = ["delayed", "stale", "degraded", "unconfirmed"].includes(node.status);
          const isError = ["invalid", "unhealthy", "stopped"].includes(node.status);
          const nodeColor = isReady
            ? "var(--positive, #69c58b)"
            : isWarning
              ? "var(--warning, #d9ad55)"
              : isError
                ? "var(--critical, #e0767c)"
                : "var(--text-dim, #93a2ac)";

          const Icon = node.icon;

          return (
            <div key={node.key} style={{ display: "flex", alignItems: "center", flex: 1 }}>
              {/* Node Card */}
              <div
                aria-label={`${node.label}：${readinessStatusLabel(node.status)}`}
                style={{
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  gap: "6px",
                  padding: "10px 12px",
                  minWidth: "100px",
                  borderRadius: "6px",
                  background: isReady
                    ? "rgba(105, 197, 139, 0.08)"
                    : "rgba(255, 255, 255, 0.02)",
                  border: `1px solid ${nodeColor}55`,
                  textAlign: "center",
                  position: "relative",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: "5px", color: nodeColor }}>
                  <Icon size={18} />
                  <strong style={{ fontSize: "12px" }}>{node.label}</strong>
                </div>
                <span style={{ fontSize: "10px", color: "var(--text-dim)", textTransform: "capitalize" }}>
                  {readinessStatusLabel(node.status)}
                </span>
                {isReady && (
                  <span
                    style={{
                      position: "absolute",
                      top: "-4px",
                      right: "-4px",
                      width: "8px",
                      height: "8px",
                      borderRadius: "50%",
                      background: "var(--positive)",
                      boxShadow: "0 0 6px var(--positive)",
                    }}
                  />
                )}
              </div>

              {/* Connecting Line */}
              {index < nodes.length - 1 && (
                <div
                  style={{
                    flex: 1,
                    height: "2px",
                    background: isReady ? `linear-gradient(90deg, ${nodeColor}, var(--border-strong))` : "var(--border)",
                    margin: "0 6px",
                    position: "relative",
                  }}
                />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function readinessStatusLabel(status: ReadinessStatus): string {
  return {
    ready: "就绪",
    delayed: "延迟",
    stale: "已过期",
    missing: "无数据",
    invalid: "无效",
    unconfirmed: "未确认",
    degraded: "降级",
    unhealthy: "异常",
    stopped: "已停止",
  }[status];
}
