import {
  decisionReasonRequiresReview,
  orderDecisionsChronologically,
  selectLatestDecision,
} from "../../decisionSemantics";
import type { StrategyDecision } from "../../types";

export type DecisionVerdict = "eligible" | "blocked";
export type RoshDirection = "supports" | "opposes";

export interface DecisionDelta {
  previous: StrategyDecision;
  current: StrategyDecision;
  previousVerdict: DecisionVerdict;
  currentVerdict: DecisionVerdict;
  verdictChanged: boolean;
  directionChanged: boolean;
  marketProbabilityDelta: number | null;
  modelProbabilityDelta: number | null;
  edgeDelta: number | null;
  dataQualityDelta: number | null;
  reasonChanged: boolean;
  versionChanged: boolean;
  previousRoshDirection: RoshDirection | null;
  currentRoshDirection: RoshDirection | null;
  roshDirectionChanged: boolean;
  summary: string;
}

export function latestDecisionDelta(
  decisions: StrategyDecision[],
): DecisionDelta | null {
  const current = selectLatestDecision(decisions);
  if (!current || !comparableDecision(current)) return null;
  const previous = selectPreviousComparableDecision(decisions, current);
  return previous ? compareDecisions(previous, current) : null;
}

export function selectPreviousComparableDecision(
  decisions: StrategyDecision[],
  current: StrategyDecision,
): StrategyDecision | null {
  if (!comparableDecision(current)) return null;
  const ordered = orderDecisionsChronologically(
    decisions.filter((decision) => (
      decision.map_number === current.map_number && comparableDecision(decision)
    )),
  );
  const unique = new Map<string, StrategyDecision>();
  ordered.forEach((decision, index) => {
    unique.set(decision.decision_key || `legacy-${index}`, decision);
  });
  const deduplicated = orderDecisionsChronologically([...unique.values()]);
  const currentIndex = deduplicated.findIndex((decision) => (
    decision === current
    || Boolean(current.decision_key) && decision.decision_key === current.decision_key
  ));
  return currentIndex > 0 ? deduplicated[currentIndex - 1] : null;
}

export function compareDecisions(
  previous: StrategyDecision,
  current: StrategyDecision,
): DecisionDelta | null {
  if (
    !comparableDecision(previous)
    || !comparableDecision(current)
    || previous.map_number !== current.map_number
    || Boolean(previous.decision_key)
      && previous.decision_key === current.decision_key
  ) {
    return null;
  }
  const previousVerdict = verdict(previous);
  const currentVerdict = verdict(current);
  const directionChanged = previous.underdog_side !== current.underdog_side;
  const previousRoshDirection = roshDirection(previous);
  const currentRoshDirection = roshDirection(current);
  const draft = {
    previous,
    current,
    previousVerdict,
    currentVerdict,
    verdictChanged: previousVerdict !== currentVerdict,
    directionChanged,
    marketProbabilityDelta: directionChanged
      ? null
      : current.market_probability - previous.market_probability,
    modelProbabilityDelta: directionChanged
      ? null
      : current.model_probability - previous.model_probability,
    edgeDelta: directionChanged ? null : current.edge - previous.edge,
    dataQualityDelta: finite(previous.data_quality) && finite(current.data_quality)
      ? Number(current.data_quality) - Number(previous.data_quality)
      : null,
    reasonChanged: previous.reason !== current.reason,
    versionChanged: previous.strategy_version !== current.strategy_version,
    previousRoshDirection,
    currentRoshDirection,
    roshDirectionChanged: previousRoshDirection !== null
      && currentRoshDirection !== null
      && previousRoshDirection !== currentRoshDirection,
  };
  return { ...draft, summary: deltaSummary(draft) };
}

export function verdictLabel(verdict: DecisionVerdict): string {
  return verdict === "eligible" ? "策略合格" : "策略拒绝";
}

export function roshDirectionLabel(direction: RoshDirection | null): string {
  if (direction === "supports") return "支持弱势方";
  if (direction === "opposes") return "不支持弱势方";
  return "方向不可判";
}

export function decisionReasonLabel(reason: string): string {
  return {
    edge_below_threshold: "Edge 未达到最终阈值",
    rosh_direction_opposes_underdog: "Rosh 方向不支持弱势方",
    rosh_direction_unavailable: "Rosh 方向不可判",
    insufficient_data_quality: "数据质量未达到门槛",
    conservative_probability_not_above_market: "保守概率未高于市场",
    no_independent_positive_contribution: "缺少独立正向贡献",
    eligible: "全部策略门槛已通过",
  }[reason] || "其他策略条件";
}

function comparableDecision(decision: StrategyDecision): boolean {
  return !decisionReasonRequiresReview(decision.reason)
    && Number.isInteger(decision.map_number)
    && decision.map_number > 0
    && (decision.underdog_side === "team_one" || decision.underdog_side === "team_two")
    && finiteUnit(decision.market_probability)
    && finiteUnit(decision.model_probability)
    && finite(decision.edge)
    && (decision.eligible === 0 || decision.eligible === 1)
    && Number.isFinite(Date.parse(decision.decided_at));
}

function verdict(decision: StrategyDecision): DecisionVerdict {
  return decision.eligible === 1 ? "eligible" : "blocked";
}

function roshDirection(decision: StrategyDecision): RoshDirection | null {
  const entry = decision.inputs?.comeback_entry;
  if (!entry || typeof entry !== "object" || Array.isArray(entry)) return null;
  const probability = (entry as Record<string, unknown>).rosh_underdog_probability;
  return finiteUnit(probability) ? probability > 0.5 ? "supports" : "opposes" : null;
}

function deltaSummary(delta: Omit<DecisionDelta, "summary">): string {
  const changes: string[] = [];
  if (delta.versionChanged) {
    changes.push(
      `策略版本由 ${delta.previous.strategy_version} 切换至 ${delta.current.strategy_version}，数值不可完全直接对比`,
    );
  }
  if (delta.directionChanged) {
    changes.push("策略关注方向发生变化");
  } else {
    const reasonChange = reasonDrivenChange(delta);
    if (reasonChange) changes.push(reasonChange);
    for (const change of secondaryChanges(delta)) {
      if (changes.length >= 2) break;
      if (change !== reasonChange) changes.push(change);
    }
  }
  if (!changes.length && delta.reasonChanged) changes.push("判断原因发生变化");
  if (!changes.length) changes.push("关键指标变化有限");
  const outcome = delta.verdictChanged
    ? `最终策略由${verdictLabel(delta.previousVerdict).replace("策略", "")}变为${verdictLabel(delta.currentVerdict).replace("策略", "")}`
    : `策略结论保持${verdictLabel(delta.currentVerdict).replace("策略", "")}`;
  const explanation = changes.length > 1
    ? `${changes[0]}；同时${/^[A-Za-z]/.test(changes[1]) ? " " : ""}${changes[1]}`
    : changes[0];
  return `${explanation}，因此${outcome}。`;
}

function reasonDrivenChange(delta: Omit<DecisionDelta, "summary">): string | null {
  if (delta.current.reason === "edge_below_threshold") {
    return thresholdChange(
      "模型 Edge",
      delta.previous.edge,
      delta.current.edge,
      true,
      "已低于最终阈值",
    );
  }
  if (delta.current.reason === "insufficient_data_quality") {
    if (!finite(delta.previous.data_quality) || !finite(delta.current.data_quality)) return null;
    return thresholdChange(
      "数据质量",
      delta.previous.data_quality,
      delta.current.data_quality,
      false,
      "未达到策略门槛",
    );
  }
  if (delta.current.reason === "rosh_direction_opposes_underdog") {
    return delta.currentRoshDirection === null
      ? "Rosh 方向不可判"
      : `Rosh 方向转为${roshDirectionLabel(delta.currentRoshDirection)}`;
  }
  if (delta.current.reason === "rosh_direction_unavailable") {
    return "Rosh 方向变为不可判";
  }
  if (delta.current.reason === "conservative_probability_not_above_market") {
    const probability = conservativeProbability(delta.current);
    return probability === null
      ? "保守概率未高于市场概率"
      : `保守概率 ${formatPercentValue(probability)} 未高于市场概率 ${formatPercentValue(delta.current.market_probability)}`;
  }
  if (delta.current.reason === "no_independent_positive_contribution") {
    return "独立正向贡献未达到策略要求";
  }
  return null;
}

function secondaryChanges(delta: Omit<DecisionDelta, "summary">): string[] {
  const rosh = delta.roshDirectionChanged
    ? `Rosh 方向转为${roshDirectionLabel(delta.currentRoshDirection)}`
    : null;
  const edge = meaningful(delta.edgeDelta)
    ? changeText("模型 Edge", Number(delta.edgeDelta))
    : null;
  const market = meaningful(delta.marketProbabilityDelta)
    ? changeText("市场概率", Number(delta.marketProbabilityDelta))
    : null;
  const dataQuality = meaningful(delta.dataQualityDelta)
    ? changeText("数据质量", Number(delta.dataQualityDelta))
    : null;
  const ordered = delta.current.reason === "edge_below_threshold"
    ? [rosh, market, dataQuality]
    : delta.current.reason === "insufficient_data_quality"
      ? [rosh, edge, market]
      : delta.current.reason.startsWith("rosh_direction_")
        ? [edge, market, dataQuality]
        : delta.current.reason === "conservative_probability_not_above_market"
          ? [rosh, edge, dataQuality]
          : [market, rosh, edge, dataQuality];
  return ordered.filter((change): change is string => Boolean(change));
}

function thresholdChange(
  label: string,
  previous: number,
  current: number,
  signed: boolean,
  outcome: string,
): string {
  const previousText = signed ? formatSignedPercent(previous) : formatPercentValue(previous);
  const currentText = signed ? formatSignedPercent(current) : formatPercentValue(current);
  const separator = /[A-Za-z]$/.test(label) ? " " : "";
  if (approximatelyEqual(previous, current)) return `${label}${separator}为 ${currentText}，${outcome}`;
  const direction = current > previous ? "升至" : "降至";
  return `${label}${separator}从 ${previousText} ${direction} ${currentText}，${outcome}`;
}

function changeText(label: string, value: number): string {
  const direction = value > 0 ? "上升" : "下降";
  const separator = /[A-Za-z]$/.test(label) ? " " : "";
  return `${label}${separator}${direction} ${Math.abs(value * 100).toFixed(1)} 个百分点`;
}

function conservativeProbability(decision: StrategyDecision): number | null {
  const value = decision.inputs?.conservative_probability;
  return finiteUnit(value) ? value : null;
}

function formatSignedPercent(value: number): string {
  return `${value > 0 ? "+" : ""}${formatPercentValue(value)}`;
}

function formatPercentValue(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

function approximatelyEqual(left: number, right: number): boolean {
  return Math.abs(left - right) < 1e-9;
}

function meaningful(value: number | null): boolean {
  return value !== null && Math.abs(value) >= 0.0005;
}

function finite(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function finiteUnit(value: unknown): value is number {
  return finite(value) && value >= 0 && value <= 1;
}
