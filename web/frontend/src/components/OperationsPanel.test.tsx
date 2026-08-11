import { FluentProvider, webDarkTheme } from "@fluentui/react-components";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ControlComponent, HealthItem } from "../types";
import { OperationsPanel } from "./OperationsPanel";


const visionComponent: ControlComponent = {
  component: "vision_supervisor",
  label: "Stable Vision",
  status: "running",
  pid: 321,
  started_at: "2026-08-07T10:01:00+00:00",
  detail: "started",
  control_allowed: true,
};


function healthItem(
  component: string,
  status: HealthItem["status"] = "healthy",
  details: Record<string, unknown> = {},
): HealthItem {
  return {
    component,
    status,
    reported_status: status,
    freshness: "fresh",
    age_seconds: 1,
    last_heartbeat_at: "2026-08-07T10:01:00+00:00",
    last_success_at: status === "healthy" ? "2026-08-07T10:01:00+00:00" : null,
    last_error_at: status === "healthy" ? null : "2026-08-07T10:01:00+00:00",
    last_error: status === "healthy" ? null : "upstream error",
    details,
  };
}


function renderPanel({
  components,
  health,
  onControl = vi.fn(),
}: {
  components: ControlComponent[];
  health: HealthItem[];
  onControl?: ReturnType<typeof vi.fn>;
}) {
  render(
    <FluentProvider theme={webDarkTheme}>
      <OperationsPanel
        alerts={[{
          incident_id: 1,
          dedupe_key: "raybet:stale",
          episode: 1,
          category: "operational",
          severity: "warning",
          title: "RayBet stale",
          body: "collector heartbeat is stale",
          first_detected_at: "2026-08-07T10:00:00+00:00",
          opened_at: "2026-08-07T10:00:00+00:00",
          last_detected_at: "2026-08-07T10:01:00+00:00",
          acknowledged_at: null,
          acknowledged_by: null,
          source: {},
          occurrence_count: 1,
        }]}
        busyKey={null}
        components={components}
        controlMessage={null}
        controlsEnabled
        health={health}
        mappings={[]}
        match={null}
        onAcknowledge={vi.fn()}
        onApproveMapping={vi.fn()}
        onControl={onControl}
        onCreateAutomaticMap={vi.fn()}
        onInvalidateMapping={vi.fn()}
      />
    </FluentProvider>,
  );
  return onControl;
}


describe("OperationsPanel", () => {
  it("separates controllable services from internal RayBet channels and shows the root cause once", () => {
    renderPanel({
      components: [{
        component: "raybet_collector",
        label: "RayBet collector",
        status: "identity_mismatch",
        pid: null,
        started_at: "2026-08-07T10:00:00+00:00",
        detail: null,
        control_allowed: false,
      }, visionComponent],
      health: [
        healthItem("raybet_worker", "degraded", {
          delegated: 2,
          live_list_cache: { state: "fresh" },
        }),
        healthItem("raybet_priority_odds_worker", "healthy", {
          errors: 0,
          interval_seconds: 8,
          listed: 0,
          matches: 0,
        }),
        healthItem("raybet_full_odds_worker", "degraded", {
          errors: 1,
          failed_match_ids: ["38423645"],
          interval_seconds: 120,
          listed: 3,
          matches: 0,
        }),
        healthItem("vision_worker"),
        healthItem("map_decision_worker"),
        healthItem("postmatch_worker", "unhealthy"),
        healthItem("strict_ingest_worker"),
        healthItem("draft_publisher", "unhealthy"),
      ],
    });

    expect(screen.getByRole("heading", { name: "系统控制" })).toBeInTheDocument();
    expect(screen.getByText("RayBet 采集服务")).toBeInTheDocument();
    expect(screen.getByText("Stable Vision")).toBeInTheDocument();
    expect(screen.getByText("进程身份不匹配")).toBeInTheDocument();
    expect(screen.getByText(/命令身份与当前 master 不一致/)).toBeInTheDocument();

    expect(screen.getByText("赛事发现与调度")).toBeInTheDocument();
    expect(screen.getByText("优先赔率通道")).toBeInTheDocument();
    expect(screen.getByText("常规赔率通道")).toBeInTheDocument();
    expect(screen.queryByText("全量赔率采集")).not.toBeInTheDocument();
    expect(screen.getByText("主循环心跳正常，1 条子通道异常已在下方定位。")).toBeInTheDocument();
    expect(screen.getAllByText("1 场比赛在本轮采集失败：38423645，下一轮将自动重试。")).toHaveLength(1);
    expect(screen.queryByText("1 full odds collection error(s)")).not.toBeInTheDocument();
    expect(screen.getByText(/正常空闲状态/)).toBeInTheDocument();

    expect(screen.getByText("赛后数据同步")).toBeInTheDocument();
    expect(screen.getByText("Map 决策检查点")).toBeInTheDocument();
    expect(screen.getByText(/每个五分钟节点生成可追溯的 shadow 决策/)).toBeInTheDocument();
    expect(screen.getByText("正式赛果入库")).toBeInTheDocument();
    expect(screen.queryByText("draft_publisher")).not.toBeInTheDocument();
    expect(screen.getByText("RayBet stale")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "启动 RayBet 采集服务" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "停止 RayBet 采集服务" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "停止 Stable Vision" })).toBeEnabled();
  });

  it("keeps supported start and stop actions directly usable", () => {
    const onControl = renderPanel({
      components: [{
        component: "raybet_collector",
        label: "RayBet collector",
        status: "stopped",
        pid: null,
        started_at: null,
        detail: null,
        control_allowed: true,
      }, visionComponent],
      health: [healthItem("raybet_worker"), healthItem("vision_worker")],
    });

    const start = screen.getByRole("button", { name: "启动 RayBet 采集服务" });
    expect(start).toBeEnabled();
    expect(screen.getByRole("button", { name: "停止 RayBet 采集服务" })).toBeDisabled();
    fireEvent.click(start);
    expect(onControl).toHaveBeenCalledWith("raybet_collector", "start");
  });
});
