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
          components={[{
            component: "raybet_collector",
            label: "RayBet collector",
            status: "stopped",
            pid: null,
            started_at: null,
            detail: null,
            control_allowed: true,
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

    expect(screen.getByText("RayBet collector")).toBeInTheDocument();
    expect(screen.getByText("RayBet stale")).toBeInTheDocument();
    expect(screen.queryByText("Mail worker")).not.toBeInTheDocument();
  });
});
