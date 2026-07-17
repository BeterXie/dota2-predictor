import { FluentProvider, webDarkTheme } from "@fluentui/react-components";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { OperationsPanel } from "./OperationsPanel";


describe("OperationsPanel", () => {
  it("renders allowlisted controls, persisted alerts, and exact evidence", () => {
    render(
      <FluentProvider theme={webDarkTheme} applyStylesToPortals={false}>
        <OperationsPanel
          alerts={[{
            incident_id: 7,
            dedupe_key: "operational:raybet_worker",
            episode: 1,
            category: "operational",
            severity: "critical",
            title: "赔率采集状态异常",
            body: "timeout",
            first_detected_at: "2026-07-15T01:00:00+00:00",
            opened_at: "2026-07-15T01:00:30+00:00",
            last_detected_at: "2026-07-15T01:00:30+00:00",
            acknowledged_at: null,
            acknowledged_by: null,
            source: {},
            occurrence_count: 1,
          }]}
          busyKey={null}
          components={[{
            component: "raybet_collector",
            label: "RayBet collector",
            status: "stopped",
            pid: null,
            started_at: null,
            detail: null,
            control_allowed: true,
          }, {
            component: "draft_publisher",
            label: "Draft publisher",
            status: "stopped",
            pid: null,
            started_at: null,
            detail: null,
            control_allowed: true,
          }]}
          controlMessage={null}
          controlsEnabled
          health={[]}
          mappings={[{
            mapping_id: 3,
            map_number: 1,
            event_id: "event-1",
            raybet_team_ids: [11, 22],
            canonical_teams: [{ id: 101, name: "Alpha" }, { id: 202, name: "Beta" }],
            acceptance_mode: "manual_exact",
            automatic_approval_id: null,
            accepted_by: "operator",
            accepted_at: "2026-07-15T01:00:00+00:00",
            recorded_at: "2026-07-15T01:00:00+00:00",
            evidence: {},
            evidence_hash: "a".repeat(64),
            invalidation: null,
            evidence_approval_id: null,
          }]}
          match={null}
          onAcknowledge={vi.fn()}
          onApproveMapping={vi.fn()}
          onControl={vi.fn()}
          onCreateAutomaticMap={vi.fn()}
          onInvalidateMapping={vi.fn()}
        />
      </FluentProvider>,
    );

    expect(screen.getByRole("button", { name: "启动赔率采集" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "停止赔率采集" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "启动阵容预测发布器" })).toBeEnabled();
    expect(screen.getByText("赔率采集状态异常")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /确认告警/ })).toBeEnabled();
    expect(screen.getByText("manual_exact")).toBeInTheDocument();
  });

  it("disables per-component actions for supervisor-managed workers", () => {
    const { container } = render(
      <FluentProvider theme={webDarkTheme} applyStylesToPortals={false}>
        <OperationsPanel
          alerts={[]}
          busyKey={null}
          components={[{
            component: "raybet_collector",
            label: "RayBet collector",
            status: "running",
            pid: null,
            started_at: "2026-07-16T00:00:00+00:00",
            detail: "managed by unified supervisor",
            control_allowed: false,
          }]}
          controlMessage={null}
          controlsEnabled
          health={[]}
          mappings={[]}
          match={null}
          onAcknowledge={vi.fn()}
          onApproveMapping={vi.fn()}
          onControl={vi.fn()}
          onCreateAutomaticMap={vi.fn()}
          onInvalidateMapping={vi.fn()}
        />
      </FluentProvider>,
    );

    const buttons = container.querySelectorAll(".managed-worker button");
    expect(buttons).toHaveLength(3);
    buttons.forEach((button) => expect(button).toBeDisabled());
  });

  it("shows optional SMTP as unconfigured despite a stale worker heartbeat", () => {
    render(
      <FluentProvider theme={webDarkTheme} applyStylesToPortals={false}>
        <OperationsPanel
          alerts={[]}
          busyKey={null}
          components={[]}
          controlMessage={null}
          controlsEnabled
          health={[
            {
              component: "mail_worker",
              status: "unhealthy",
              reported_status: "degraded",
              freshness: "stale",
              age_seconds: 3600,
              last_heartbeat_at: "2026-07-15T00:00:00+00:00",
              last_success_at: null,
              last_error_at: "2026-07-15T00:00:00+00:00",
              last_error: "heartbeat_expired",
              details: {},
            },
            {
              component: "mail",
              status: "degraded",
              reported_status: "degraded",
              freshness: "fresh",
              age_seconds: 1,
              last_heartbeat_at: "2026-07-16T00:00:00+00:00",
              last_success_at: null,
              last_error_at: "2026-07-16T00:00:00+00:00",
              last_error: "configuration_missing",
              details: { smtp_configured: false },
            },
          ]}
          mappings={[]}
          match={null}
          onAcknowledge={vi.fn()}
          onApproveMapping={vi.fn()}
          onControl={vi.fn()}
          onCreateAutomaticMap={vi.fn()}
          onInvalidateMapping={vi.fn()}
        />
      </FluentProvider>,
    );

    expect(screen.getByText("未配置或未启动")).toBeInTheDocument();
    expect(screen.queryByText(/heartbeat_expired/)).not.toBeInTheDocument();
  });

  it("keeps unacknowledged alerts visible when the panel is capped", () => {
    const acknowledged = Array.from({ length: 9 }, (_, index) => ({
      incident_id: index + 1,
      dedupe_key: `acknowledged:${index}`,
      episode: 1,
      category: "operational" as const,
      severity: "critical" as const,
      title: `acknowledged-${index}`,
      body: "handled",
      first_detected_at: "2026-07-15T01:00:00+00:00",
      opened_at: "2026-07-15T01:00:30+00:00",
      last_detected_at: "2026-07-15T01:00:30+00:00",
      acknowledged_at: "2026-07-15T01:01:00+00:00",
      acknowledged_by: "operator",
      source: {},
      occurrence_count: 1,
    }));
    render(
      <FluentProvider theme={webDarkTheme} applyStylesToPortals={false}>
        <OperationsPanel
          alerts={[...acknowledged, {
            ...acknowledged[0],
            incident_id: 100,
            dedupe_key: "unacknowledged",
            title: "must-remain-visible",
            acknowledged_at: null,
            acknowledged_by: null,
          }]}
          busyKey={null}
          components={[]}
          controlMessage={null}
          controlsEnabled
          health={[]}
          mappings={[]}
          match={null}
          onAcknowledge={vi.fn()}
          onApproveMapping={vi.fn()}
          onControl={vi.fn()}
          onCreateAutomaticMap={vi.fn()}
          onInvalidateMapping={vi.fn()}
        />
      </FluentProvider>,
    );

    expect(screen.getByText("must-remain-visible")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认告警：must-remain-visible" })).toBeEnabled();
  });
});
