import { LineChart, ScatterChart } from "echarts/charts";
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from "echarts/components";
import * as echarts from "echarts/core";
import type { EChartsCoreOption } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import ReactEChartsCore from "echarts-for-react/lib/core";
import { useMemo } from "react";

import { formatClock, formatDateTime, formatPercent, parseTimestamp } from "../format";
import {
  comparePeriods,
  mapNumberForPeriod,
  periodLabel,
  resolvePeriod,
} from "../probability-period";
import type { StrategyDecision, WinnerTimelinePoint } from "../types";

interface ProbabilityChartProps {
  timeline: WinnerTimelinePoint[];
  decisions: StrategyDecision[];
  teamOne: string;
  teamTwo: string;
  preferredPeriod?: string | null;
  /** Historical replay should open on the most recently observed map. */
  preferLatestPeriod?: boolean;
  selectedPeriod: string | null;
  onPeriodChange: (period: string) => void;
}

type SeriesPoint = [number, number | null];

echarts.use([
  LineChart,
  ScatterChart,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  CanvasRenderer,
]);

export function ProbabilityChart({
  timeline,
  decisions,
  teamOne,
  teamTwo,
  preferredPeriod,
  preferLatestPeriod = false,
  selectedPeriod,
  onPeriodChange,
}: ProbabilityChartProps) {
  const periods = useMemo(
    () => Array.from(new Set(timeline.map((point) => point.period))).sort(comparePeriods),
    [timeline],
  );
  const period = resolvePeriod(
    periods,
    selectedPeriod,
    preferredPeriod,
    preferLatestPeriod,
  );
  const points = useMemo(
    () => timeline.filter((point) => !period || point.period === period),
    [period, timeline],
  );
  const mapNumber = mapNumberForPeriod(period) || 0;
  const selectedDecisions = decisions.filter(
    (decision) => !mapNumber || decision.map_number === mapNumber,
  );

  const option = useMemo<EChartsCoreOption>(() => {
    const byTime = new Map(
      points.map((point) => [parseTimestamp(point.observed_at)?.getTime(), point]),
    );
    const teamOneData = withGaps(points, "team_one");
    const teamTwoData = withGaps(points, "team_two");
    const modelData: Array<[number, number]> = selectedDecisions.flatMap((decision) => {
      const time = parseTimestamp(decision.decided_at)?.getTime();
      if (time == null) return [];
      const probability = decision.underdog_side === "team_two"
        ? 1 - decision.model_probability
        : decision.model_probability;
      return [[time, probability]];
    });

    return {
      animation: false,
      backgroundColor: "transparent",
      grid: { left: 54, right: 22, top: 32, bottom: 54 },
      legend: {
        top: 0,
        left: 0,
        itemHeight: 3,
        itemWidth: 18,
        textStyle: { color: "#b8c2c9", fontFamily: "Segoe UI Variable", fontSize: 12 },
      },
      tooltip: {
        trigger: "axis",
        backgroundColor: "#11171c",
        borderColor: "#465560",
        textStyle: { color: "#f1f5f7", fontFamily: "Segoe UI Variable", fontSize: 12 },
        formatter: (rawParams: unknown) => {
          const params = Array.isArray(rawParams) ? rawParams : [rawParams];
          const first = params[0] as { value?: [number, number] } | undefined;
          const timestamp = first?.value?.[0];
          if (typeof timestamp !== "number") return "";
          const observed = byTime.get(timestamp);
          const lines = [`<strong>${formatDateTime(new Date(timestamp).toISOString())}</strong>`];
          if (observed?.game_clock_seconds != null) {
            lines.push(`比赛时钟 ${formatClock(observed.game_clock_seconds)}`);
          }
          for (const entry of params as Array<{ marker?: string; seriesName?: string; value?: [number, number] }>) {
            if (entry.value?.[1] == null) continue;
            lines.push(`${entry.marker || ""}${entry.seriesName || ""} ${formatPercent(entry.value[1])}`);
          }
          return lines.join("<br />");
        },
      },
      xAxis: {
        type: "time",
        axisLine: { lineStyle: { color: "#465560" } },
        axisTick: { show: false },
        axisLabel: { color: "#9ba8b1", fontSize: 11, hideOverlap: true },
        splitLine: { show: false },
      },
      yAxis: {
        type: "value",
        min: 0,
        max: 1,
        interval: 0.25,
        axisLabel: {
          color: "#9ba8b1",
          fontSize: 11,
          formatter: (value: number) => `${Math.round(value * 100)}%`,
        },
        splitLine: { lineStyle: { color: "#2b363e", type: "dashed" } },
      },
      dataZoom: [
        { type: "inside", filterMode: "none" },
        {
          type: "slider",
          height: 16,
          bottom: 10,
          borderColor: "transparent",
          backgroundColor: "#172027",
          fillerColor: "rgba(85, 199, 187, 0.2)",
          handleStyle: { color: "#55c7bb", borderColor: "#55c7bb" },
          textStyle: { color: "#87959f" },
        },
      ],
      series: [
        {
          name: teamOne || "队伍一",
          type: "line",
          step: "end",
          showSymbol: false,
          connectNulls: false,
          data: teamOneData,
          lineStyle: { width: 2, color: "#55c7bb" },
          itemStyle: { color: "#55c7bb" },
        },
        {
          name: teamTwo || "队伍二",
          type: "line",
          step: "end",
          showSymbol: false,
          connectNulls: false,
          data: teamTwoData,
          lineStyle: { width: 2, color: "#ef8b79" },
          itemStyle: { color: "#ef8b79" },
        },
        {
          name: "模型概率",
          type: "scatter",
          symbol: "diamond",
          symbolSize: 9,
          data: modelData,
          itemStyle: { color: "#e1b95b" },
        },
      ],
    };
  }, [points, selectedDecisions, teamOne, teamTwo]);

  if (!points.length) {
    return (
      <div className="chart-empty">
        <span>暂无完整胜负盘快照</span>
        <small>曲线只在同一采集时刻同时收到双方报价时生成</small>
      </div>
    );
  }

  return (
    <div className="probability-chart">
      {periods.length > 1 && (
        <div className="chart-controls">
          <label className="period-select">
            <span>局数</span>
            <select value={period || ""} onChange={(event) => onPeriodChange(event.target.value)}>
              {periods.map((item) => (
                <option key={item} value={item}>{periodLabel(item)}</option>
              ))}
            </select>
          </label>
        </div>
      )}
      <div className="probability-chart-canvas">
        <ReactEChartsCore
          echarts={echarts}
          option={option}
          notMerge
          lazyUpdate
          style={{ height: 330 }}
        />
      </div>
    </div>
  );
}

function withGaps(
  points: WinnerTimelinePoint[],
  side: "team_one" | "team_two",
): SeriesPoint[] {
  const output: SeriesPoint[] = [];
  let previousTime: number | null = null;
  for (const point of points) {
    const time = parseTimestamp(point.observed_at)?.getTime();
    if (time == null) continue;
    if (previousTime != null && time - previousTime > 60_000) {
      output.push([previousTime + 1, null], [time - 1, null]);
    }
    output.push([time, point.probabilities[side]]);
    previousTime = time;
  }
  return output;
}

export { resolvePeriod } from "../probability-period";
