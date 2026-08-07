import { BarChart } from "echarts/charts";
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
import { useMemo, useState } from "react";

import { formatClock, formatDateTime, formatPercent } from "../format";
import type { WinnerTimelinePoint } from "../types";


interface OddsProbabilityChartProps {
  timeline: WinnerTimelinePoint[];
  teamOne: string;
  teamTwo: string;
  preferredPeriod?: string | null;
}


echarts.use([
  BarChart,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  CanvasRenderer,
]);


export function OddsProbabilityChart({
  timeline,
  teamOne,
  teamTwo,
  preferredPeriod = null,
}: OddsProbabilityChartProps) {
  const [selectedPeriod, setSelectedPeriod] = useState<string | null>(null);
  const periods = useMemo(
    () => Array.from(new Set(timeline.map((point) => point.period))),
    [timeline],
  );
  const period = resolveOddsPeriod(periods, selectedPeriod, preferredPeriod);
  const points = useMemo(
    () => timeline
      .filter((point) => !period || point.period === period)
      .filter(validPoint)
      .sort((left, right) => Date.parse(left.observed_at) - Date.parse(right.observed_at)),
    [period, timeline],
  );
  const latest = points.at(-1) || null;

  const option = useMemo<EChartsCoreOption>(() => ({
    animation: false,
    backgroundColor: "transparent",
    grid: {
      left: 48,
      right: 18,
      top: 38,
      bottom: points.length > 12 ? 60 : 34,
    },
    legend: {
      top: 0,
      left: 0,
      itemHeight: 9,
      itemWidth: 9,
      textStyle: {
        color: "#b8c2c9",
        fontFamily: "Segoe UI Variable",
        fontSize: 11,
      },
    },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      backgroundColor: "#11171c",
      borderColor: "#465560",
      textStyle: {
        color: "#f1f5f7",
        fontFamily: "Segoe UI Variable",
        fontSize: 12,
      },
      formatter: (rawParams: unknown) => probabilityTooltip(rawParams, points),
    },
    xAxis: {
      type: "category",
      data: points.map((point) => point.observed_at),
      axisLine: { lineStyle: { color: "#465560" } },
      axisTick: { show: false },
      axisLabel: {
        color: "#9ba8b1",
        fontSize: 10,
        hideOverlap: true,
        formatter: (value: string) => compactTimestamp(value),
      },
    },
    yAxis: {
      type: "value",
      min: 0,
      max: 1,
      interval: 0.25,
      axisLabel: {
        color: "#9ba8b1",
        fontSize: 10,
        formatter: (value: number) => `${Math.round(value * 100)}%`,
      },
      splitLine: { lineStyle: { color: "#2b363e", type: "dashed" } },
    },
    dataZoom: points.length > 12 ? [
      { type: "inside", filterMode: "none" },
      {
        type: "slider",
        height: 14,
        bottom: 8,
        borderColor: "transparent",
        backgroundColor: "#172027",
        fillerColor: "rgba(85, 199, 187, 0.2)",
        handleStyle: { color: "#55c7bb", borderColor: "#55c7bb" },
        textStyle: { color: "#87959f" },
      },
    ] : [],
    series: [
      {
        name: teamOne || "队伍一",
        type: "bar",
        stack: "probability",
        barMaxWidth: 28,
        data: points.map((point) => point.probabilities.team_one),
        itemStyle: { color: "#55c7bb" },
      },
      {
        name: teamTwo || "队伍二",
        type: "bar",
        stack: "probability",
        barMaxWidth: 28,
        data: points.map((point) => point.probabilities.team_two),
        itemStyle: { color: "#ef8b79", borderRadius: [3, 3, 0, 0] },
      },
    ],
  }), [points, teamOne, teamTwo]);

  if (!points.length) {
    return (
      <div className="chart-empty" role="status">
        <span>暂无完整胜负盘快照</span>
        <small>只有同一采集时刻同时存在双方赔率时才生成柱状图</small>
      </div>
    );
  }

  return (
    <div className="probability-chart">
      {periods.length > 1 && (
        <div className="chart-controls">
          <label className="period-select">
            <span>局数</span>
            <select
              aria-label="赔率图局数"
              onChange={(event) => setSelectedPeriod(event.target.value)}
              value={period || ""}
            >
              {periods.map((item) => (
                <option key={item} value={item}>{periodLabel(item)}</option>
              ))}
            </select>
          </label>
        </div>
      )}
      <dl className="odds-chart-summary" aria-label="赔率柱状图文字摘要">
        <div>
          <dt>样本范围</dt>
          <dd>{points.length} 次 · {formatDateTime(points[0].observed_at)} 至 {formatDateTime(latest?.observed_at)}</dd>
        </div>
        <div>
          <dt>{teamOne || "队伍一"} 最新去水概率</dt>
          <dd>{formatPercent(latest?.probabilities.team_one)}</dd>
        </div>
        <div>
          <dt>{teamTwo || "队伍二"} 最新去水概率</dt>
          <dd>{formatPercent(latest?.probabilities.team_two)}</dd>
        </div>
      </dl>
      <div className="probability-chart-canvas" aria-hidden="true">
        <ReactEChartsCore
          echarts={echarts}
          lazyUpdate
          notMerge
          option={option}
          style={{ height: 260 }}
        />
      </div>
    </div>
  );
}


export function resolveOddsPeriod(
  periods: string[],
  selectedPeriod: string | null,
  preferredPeriod: string | null,
): string | null {
  if (selectedPeriod && periods.includes(selectedPeriod)) return selectedPeriod;
  if (preferredPeriod && periods.includes(preferredPeriod)) return preferredPeriod;
  return periods.at(-1) || null;
}


function validPoint(point: WinnerTimelinePoint): boolean {
  return Number.isFinite(Date.parse(point.observed_at))
    && Number.isFinite(point.probabilities.team_one)
    && Number.isFinite(point.probabilities.team_two)
    && point.probabilities.team_one >= 0
    && point.probabilities.team_one <= 1
    && point.probabilities.team_two >= 0
    && point.probabilities.team_two <= 1;
}


function probabilityTooltip(
  rawParams: unknown,
  points: WinnerTimelinePoint[],
): string {
  const params = Array.isArray(rawParams) ? rawParams : [rawParams];
  const first = params[0] as { dataIndex?: number } | undefined;
  const point = typeof first?.dataIndex === "number" ? points[first.dataIndex] : null;
  if (!point) return "";
  const lines = [`<strong>${formatDateTime(point.observed_at)}</strong>`];
  if (point.game_clock_seconds != null) {
    lines.push(`比赛时钟 ${formatClock(point.game_clock_seconds)}`);
  }
  for (const entry of params as Array<{
    marker?: string;
    seriesName?: string;
    value?: number;
  }>) {
    if (typeof entry.value !== "number") continue;
    lines.push(`${entry.marker || ""}${entry.seriesName || ""} ${formatPercent(entry.value)}`);
  }
  return lines.join("<br />");
}


function compactTimestamp(value: string): string {
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return "-";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(parsed);
}


function periodLabel(period: string): string {
  const map = /^map_(\d+)$/.exec(period);
  return map ? `第 ${map[1]} 局` : period;
}
