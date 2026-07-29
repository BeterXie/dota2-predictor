import type { CSSProperties } from "react";

interface ComparisonBarProps {
  leftLabel: string;
  leftValue: number;
  rightLabel: string;
  rightValue: number;
  unit?: string;
  precision?: number;
  leftColor?: string;
  rightColor?: string;
  showLabels?: boolean;
}

export function ComparisonBar({
  leftLabel,
  leftValue,
  rightLabel,
  rightValue,
  unit = "",
  precision = 1,
  leftColor = "var(--accent, #61cec1)",
  rightColor = "var(--team-two, #ef8b79)",
  showLabels = true,
}: ComparisonBarProps) {
  const total = leftValue + rightValue;
  const leftPercent = total > 0 ? (leftValue / total) * 100 : 50;
  const rightPercent = total > 0 ? 100 - leftPercent : 50;

  const barStyle: CSSProperties = {
    display: "flex",
    height: "8px",
    width: "100%",
    borderRadius: "4px",
    overflow: "hidden",
    background: "rgba(255, 255, 255, 0.05)",
    margin: "6px 0",
  };

  const leftSegmentStyle: CSSProperties = {
    width: `${leftPercent}%`,
    background: `linear-gradient(90deg, ${leftColor}99, ${leftColor})`,
    transition: "width 0.4s ease",
    boxShadow: `0 0 8px ${leftColor}44`,
  };

  const rightSegmentStyle: CSSProperties = {
    width: `${rightPercent}%`,
    background: `linear-gradient(90deg, ${rightColor}, ${rightColor}99)`,
    transition: "width 0.4s ease",
    boxShadow: `0 0 8px ${rightColor}44`,
  };

  return (
    <div className="comparison-bar-container" style={{ width: "100%" }}>
      {showLabels && (
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: "12px", marginBottom: "3px" }}>
          <span style={{ color: leftColor, fontWeight: 600 }}>
            {leftLabel} {leftValue.toFixed(precision)}{unit}
          </span>
          <span style={{ color: rightColor, fontWeight: 600 }}>
            {rightValue.toFixed(precision)}{unit} {rightLabel}
          </span>
        </div>
      )}
      <div style={barStyle}>
        <div style={leftSegmentStyle} title={`${leftLabel}: ${leftValue.toFixed(precision)}${unit}`} />
        <div style={rightSegmentStyle} title={`${rightLabel}: ${rightValue.toFixed(precision)}${unit}`} />
      </div>
    </div>
  );
}
