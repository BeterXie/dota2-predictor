import { FluentProvider, webDarkTheme } from "@fluentui/react-components";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { OperationsPanel } from "./OperationsPanel";

describe("OperationsPanel", () => {
  it("shows only retained operational controls and alerts", () => {
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
          components={[
            {
              component: "raybet_collector",
              label: "RayBet collector",
              status: "stopped",
              pid: null,
              started_at: null,
              detail: null,
              control_allowed: true,
            },
            {
              component: "vision_supervisor",
              label: "Stable Vision",
              status: "running",
              pid: 321,
              started_at: "2026-08-07T10:01:00+00:00",
              detail: "started",
              control_allowed: true,
            },
          ]}
          controlMessage={null}
          controlsEnabled
          health={[
            {
              component: "raybet_worker",
              status: "healthy",
              reported_status: "healthy",
              freshness: "fresh",
              age_seconds: 1,
              last_heartbeat_at: "2026-08-07T10:01:00+00:00",
              last_success_at: "2026-08-07T10:01:00+00:00",
              last_error_at: null,
              last_error: null,
              details: {},
            },
            {
              component: "draft_publisher",
              status: "unhealthy",
              reported_status: "healthy",
              freshness: "stale",
              age_seconds: 3600,
              last_heartbeat_at: "2026-08-07T09:01:00+00:00",
              last_success_at: "2026-08-07T09:01:00+00:00",
              last_error_at: null,
              last_error: null,
              details: {},
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

    expect(screen.getByText("RayBet collector")).toBeInTheDocument();
    expect(screen.getByText("Stable Vision")).toBeInTheDocument();
    expect(screen.getByText("raybet_worker")).toBeInTheDocument();
    expect(screen.getByText("RayBet stale")).toBeInTheDocument();
    expect(screen.queryByText("draft_publisher")).not.toBeInTheDocument();
    expect(screen.queryByText("Mail worker")).not.toBeInTheDocument();
  });
});
