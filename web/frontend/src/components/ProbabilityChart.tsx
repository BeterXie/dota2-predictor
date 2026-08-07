import { LineChart } from "echarts/charts";
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
import { useEffect, useMemo, useRef, useState } from "react";

import { formatClock, formatDateTime, formatPercent } from "../format";
import type { WinnerTimelinePoint } from "../types";


interface ProbabilityChartProps {
  timeline: WinnerTimelinePoint[];
  teamOne: string;
  teamTwo: string;
  preferredPeriod?: string | null;
}


type SeriesPoint = [number, number | null];

const GAP_BREAK_MS = 150_000;
const PROBABILITY_SUM_TOLERANCE = 1e-6;


echarts.use([
  LineChart,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  CanvasRenderer,
]);


export function ProbabilityChart({
  timeline,
  teamOne,
  teamTwo,
  preferredPeriod = null,
}: ProbabilityChartProps) {
  const [selectedPeriod, setSelectedPeriod] = useState<string | null>(null);
  const chartContainerRef = useRef<HTMLDivElement | null>(null);
  const resizeChartRef = useRef<(() => void) | null>(null);
  const periods = useMemo(
    () => Array.from(new Set(timeline.map((point) => point.period))),
    [timeline],
  );
  const period = resolveProbabilityPeriod(periods, selectedPeriod, preferredPeriod);
  const points = useMemo(
    () => timeline
      .filter((point) => !period || point.period === period)
      .filter(isCompleteDeVigPoint)
      .sort((left, right) => Date.parse(left.observed_at) - Date.parse(right.observed_at)),
    [period, timeline],
  );
  const latest = points.at(-1) || null;

  useEffect(() => {
    const container = chartContainerRef.current;
    if (!container || typeof ResizeObserver === "undefined") return undefined;
    let animationFrame: number | null = null;
    const resize = () => {
      if (animationFrame != null) window.cancelAnimationFrame(animationFrame);
      animationFrame = window.requestAnimationFrame(() => {
        resizeChartRef.current?.();
      });
    };
    const observer = new ResizeObserver(resize);
    observer.observe(container);
    window.addEventListener("resize", resize);
    resize();
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", resize);
      if (animationFrame != null) window.cancelAnimationFrame(animationFrame);
    };
  }, []);

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
      itemHeight: 3,
      itemWidth: 18,
      textStyle: {
        color: "#b8c2c9",
        fontFamily: "Segoe UI Variable",
        fontSize: 11,
      },
    },
    tooltip: {
      trigger: "axis",
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
      type: "time",
      axisLine: { lineStyle: { color: "#465560" } },
      axisTick: { show: false },
      axisLabel: { color: "#9ba8b1", fontSize: 10, hideOverlap: true },
      splitLine: { show: false },
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
        type: "line",
        step: "end",
        showSymbol: true,
        symbolSize: 5,
        connectNulls: false,
        data: withGaps(points, "team_one"),
        lineStyle: { width: 2, color: "#55c7bb" },
        itemStyle: { color: "#55c7bb" },
      },
      {
        name: teamTwo || "队伍二",
        type: "line",
        step: "end",
        showSymbol: true,
        symbolSize: 5,
        connectNulls: false,
        data: withGaps(points, "team_two"),
        lineStyle: { width: 2, color: "#ef8b79" },
        itemStyle: { color: "#ef8b79" },
      },
    ],
  }), [points, teamOne, teamTwo]);

  if (!points.length) {
    return (
      <div className="chart-empty" role="status">
        <span>暂无完整胜负盘快照</span>
        <small>只有同一采集时刻同时存在双方有效赔率时才计算并展示去水概率</small>
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
              aria-label="市场概率走势局数"
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
      <dl className="probability-chart-summary" aria-label="市场概率走势文字摘要">
        <div>
          <dt>完整胜负盘</dt>
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
      <div
        aria-hidden="true"
        className="probability-chart-canvas"
        ref={chartContainerRef}
      >
        <ReactEChartsCore
          echarts={echarts}
          lazyUpdate
          notMerge
          onChartReady={(instance) => {
            resizeChartRef.current = () => {
              const width = chartContainerRef.current?.clientWidth || 0;
              if (width > 0) instance.resize({ width });
            };
            resizeChartRef.current();
          }}
          option={option}
          style={{ height: 260 }}
        />
      </div>
    </div>
  );
}


export function resolveProbabilityPeriod(
  periods: string[],
  selectedPeriod: string | null,
  preferredPeriod: string | null,
): string | null {
  if (selectedPeriod && periods.includes(selectedPeriod)) return selectedPeriod;
  if (preferredPeriod && periods.includes(preferredPeriod)) return preferredPeriod;
  return periods.at(-1) || null;
}


export function isCompleteDeVigPoint(point: WinnerTimelinePoint): boolean {
  const prices = [point.prices.team_one, point.prices.team_two];
  const probabilities = [
    point.probabilities.team_one,
    point.probabilities.team_two,
  ];
  return Number.isFinite(Date.parse(point.observed_at))
    && prices.every((price) => Number.isFinite(price) && price > 1)
    && probabilities.every((probability) => (
      Number.isFinite(probability) && probability >= 0 && probability <= 1
    ))
    && Math.abs(probabilities[0] + probabilities[1] - 1)
      <= PROBABILITY_SUM_TOLERANCE;
}


export function withGaps(
  points: WinnerTimelinePoint[],
  side: "team_one" | "team_two",
): SeriesPoint[] {
  const output: SeriesPoint[] = [];
  let previousTime: number | null = null;
  for (const point of points) {
    const time = Date.parse(point.observed_at);
    if (!Number.isFinite(time)) continue;
    if (previousTime != null && time - previousTime > GAP_BREAK_MS) {
      output.push([previousTime + 1, null], [time - 1, null]);
    }
    output.push([time, point.probabilities[side]]);
    previousTime = time;
  }
  return output;
}


function probabilityTooltip(
  rawParams: unknown,
  points: WinnerTimelinePoint[],
): string {
  const params = Array.isArray(rawParams) ? rawParams : [rawParams];
  const first = params[0] as { value?: [number, number] } | undefined;
  const timestamp = first?.value?.[0];
  if (typeof timestamp !== "number") return "";
  const point = points.find((candidate) => Date.parse(candidate.observed_at) === timestamp);
  if (!point) return "";
  const lines = [`<strong>${formatDateTime(point.observed_at)}</strong>`];
  if (point.game_clock_seconds != null) {
    lines.push(`比赛时钟 ${formatClock(point.game_clock_seconds)}`);
  }
  lines.push(
    `${teamMarker(params, 0)}${String((params[0] as { seriesName?: string }).seriesName || "")} ${formatPercent(point.probabilities.team_one)}`,
    `${teamMarker(params, 1)}${String((params[1] as { seriesName?: string } | undefined)?.seriesName || "")} ${formatPercent(point.probabilities.team_two)}`,
  );
  return lines.join("<br />");
}


function teamMarker(params: unknown[], index: number): string {
  const entry = params[index] as { marker?: string } | undefined;
  return entry?.marker || "";
}


function periodLabel(period: string): string {
  const map = /^map_(\d+)$/.exec(period);
  return map ? `第 ${map[1]} 局` : period;
}
