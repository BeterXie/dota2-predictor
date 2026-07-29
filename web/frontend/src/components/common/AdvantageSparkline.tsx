import { useEffect, useId, useRef, useState } from "react";
interface AdvantageSparklineProps {
  points: Array<{ minute: number; win_rate_graph: number }>;
  height?: number;
  radiantName?: string;
  direName?: string;
  metricLabel?: string;
  unit?: string;
}

export function AdvantageSparkline({
  points,
  height = 90,
  radiantName = "Radiant",
  direName = "Dire",
  metricLabel = "优势变动曲线",
  unit = "%",
}: AdvantageSparklineProps) {
  const wrapperRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(500);
  const id = useId().replace(/:/g, "");
  const radiantGradientId = `${id}-radiant-gradient`;
  const direGradientId = `${id}-dire-gradient`;
  const radiantClipId = `${id}-radiant-clip`;
  const direClipId = `${id}-dire-clip`;

  useEffect(() => {
    const wrapper = wrapperRef.current;
    if (!wrapper || typeof ResizeObserver === "undefined") return;

    const observer = new ResizeObserver(([entry]) => {
      if (entry.contentRect.width > 0) {
        setWidth(Math.max(240, Math.round(entry.contentRect.width)));
      }
    });
    observer.observe(wrapper);
    return () => observer.disconnect();
  }, []);

  if (!points || points.length === 0) return null;

  const padding = { top: 12, right: 16, bottom: 20, left: 24 };
  const graphHeight = height - padding.top - padding.bottom;
  const graphWidth = width - padding.left - padding.right;

  const minutes = points.map((p) => p.minute);
  const minMin = Math.min(...minutes);
  const maxMin = Math.max(...minutes);
  const minRange = maxMin - minMin || 1;

  const values = points.map((p) => p.win_rate_graph);
  const maxAbsValue = Math.max(0.1, ...values.map(Math.abs));
  const hasRadiantAdvantage = values.some((value) => value > 0);
  const hasDireAdvantage = values.some((value) => value < 0);

  const getX = (minute: number) =>
    padding.left + ((minute - minMin) / minRange) * graphWidth;
  const getY = (val: number) =>
    padding.top + (0.5 - val / (maxAbsValue * 2.2)) * graphHeight;

  const zeroY = getY(0);

  const pathD = points
    .map((p, i) => `${i === 0 ? "M" : "L"} ${getX(p.minute).toFixed(1)} ${getY(p.win_rate_graph).toFixed(1)}`)
    .join(" ");

  const areaD = `${pathD} L ${getX(points[points.length - 1].minute).toFixed(1)} ${zeroY.toFixed(1)} L ${getX(points[0].minute).toFixed(1)} ${zeroY.toFixed(1)} Z`;

  return (
    <div ref={wrapperRef} className="advantage-sparkline-wrapper" style={{ width: "100%", margin: "8px 0" }}>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: "11px", color: "var(--text-dim)", marginBottom: "4px" }}>
        <span style={{ color: "var(--accent, #61cec1)", fontWeight: 600 }}>▲ {radiantName} 占优</span>
        <span>{minMin}-{maxMin} 分钟{metricLabel}</span>
        <span style={{ color: "var(--team-two, #ef8b79)", fontWeight: 600 }}>▼ {direName} 占优</span>
      </div>
      <svg
        aria-label={`${minMin}-${maxMin} 分钟${metricLabel}`}
        height={height}
        role="img"
        viewBox={`0 0 ${width} ${height}`}
        width="100%"
        style={{ display: "block", background: "rgba(17, 24, 32, 0.6)", borderRadius: "6px", border: "1px solid var(--border-subtle)" }}
      >
        <defs>
          <linearGradient id={radiantGradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#61cec1" stopOpacity="0.35" />
            <stop offset="100%" stopColor="#61cec1" stopOpacity="0.0" />
          </linearGradient>
          <linearGradient id={direGradientId} x1="0" y1="1" x2="0" y2="0">
            <stop offset="0%" stopColor="#ef8b79" stopOpacity="0.35" />
            <stop offset="100%" stopColor="#ef8b79" stopOpacity="0.0" />
          </linearGradient>
          <clipPath id={radiantClipId}>
            <rect x="0" y="0" width={width} height={zeroY} />
          </clipPath>
          <clipPath id={direClipId}>
            <rect x="0" y={zeroY} width={width} height={Math.max(0, height - zeroY)} />
          </clipPath>
        </defs>

        {/* Zero baseline */}
        <line
          x1={padding.left}
          y1={zeroY}
          x2={width - padding.right}
          y2={zeroY}
          stroke="rgba(147, 166, 178, 0.3)"
          strokeDasharray="3 3"
          strokeWidth="1"
        />

        {/* Fill Area */}
        <path className="advantage-area" d={areaD} fill={`url(#${radiantGradientId})`} clipPath={`url(#${radiantClipId})`} />
        <path className="advantage-area" d={areaD} fill={`url(#${direGradientId})`} clipPath={`url(#${direClipId})`} />

        {/* Trend Line */}
        {hasRadiantAdvantage && (
          <path
            className="advantage-trend-line"
            data-side="radiant"
            d={pathD}
            fill="none"
            stroke="var(--accent, #61cec1)"
            strokeWidth="2.5"
            strokeLinecap="round"
            strokeLinejoin="round"
            clipPath={`url(#${radiantClipId})`}
          />
        )}
        {hasDireAdvantage && (
          <path
            className="advantage-trend-line"
            data-side="dire"
            d={pathD}
            fill="none"
            stroke="var(--team-two, #ef8b79)"
            strokeWidth="2.5"
            strokeLinecap="round"
            strokeLinejoin="round"
            clipPath={`url(#${direClipId})`}
          />
        )}
        {!hasRadiantAdvantage && !hasDireAdvantage && (
          <path
            className="advantage-trend-line"
            data-side="even"
            d={pathD}
            fill="none"
            stroke="var(--text-dim)"
            strokeWidth="2.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        )}

        {/* Point Markers */}
        {points.map((p) => {
          const cx = getX(p.minute);
          const cy = getY(p.win_rate_graph);
          const pointColor = p.win_rate_graph > 0
            ? "var(--accent, #61cec1)"
            : p.win_rate_graph < 0
              ? "var(--team-two, #ef8b79)"
              : "var(--text-dim)";
          const pointLabel = p.win_rate_graph > 0
            ? `${radiantName} +${p.win_rate_graph.toFixed(1)}${unit}`
            : p.win_rate_graph < 0
              ? `${direName} +${Math.abs(p.win_rate_graph).toFixed(1)}${unit}`
              : `均势 0.0${unit}`;
          return (
            <circle
              key={p.minute}
              cx={cx}
              cy={cy}
              r="3"
              fill={pointColor}
              stroke="#0b0e13"
              strokeWidth="1.5"
            >
              <title>{`${p.minute}分钟: ${pointLabel}`}</title>
            </circle>
          );
        })}

        {/* X Axis Time Labels */}
        {points.filter((_, idx) => idx % Math.ceil(points.length / 5) === 0 || idx === points.length - 1).map((p) => (
          <text
            key={p.minute}
            x={getX(p.minute)}
            y={height - 4}
            fill="var(--text-dim)"
            fontSize="9"
            textAnchor="middle"
          >
            {p.minute}m
          </text>
        ))}
      </svg>
    </div>
  );
}
