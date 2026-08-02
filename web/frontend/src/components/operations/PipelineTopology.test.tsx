import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { MonitorMatch } from "../../types";
import { PipelineTopology } from "./PipelineTopology";

describe("PipelineTopology", () => {
  it("shows the core readiness states in operator-facing Chinese", () => {
    const match: MonitorMatch = {
      raybet_match_id: "match-1",
      tournament: "Test event",
      team_one: "Radiant",
      team_two: "Dire",
      scheduled_at: null,
      best_of: 3,
      provider_status: "live",
      updated_at: "2026-07-28T00:00:00Z",
      lifecycle: "degraded",
      history_eligible: false,
      winner: null,
      latest_vision: null,
      latest_decision: null,
      readiness: {
        odds: { status: "stale" },
        mapping: { status: "missing" },
        vision: { status: "unconfirmed" },
        model: { status: "ready" },
        strategy: { status: "stopped" },
      },
    };

    render(<PipelineTopology match={match} />);

    expect(screen.getByLabelText("赔率采集：已过期")).toBeInTheDocument();
    expect(screen.getByLabelText("赛事映射：无数据")).toBeInTheDocument();
    expect(screen.queryByText("视觉观测")).not.toBeInTheDocument();
    expect(screen.queryByText("模型判断")).not.toBeInTheDocument();
    expect(screen.queryByText("纸面策略")).not.toBeInTheDocument();
    expect(screen.queryByText("stale")).not.toBeInTheDocument();
    expect(screen.queryByText("missing")).not.toBeInTheDocument();
  });
});
