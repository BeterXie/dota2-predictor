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
  mapNumber?: number | null,
): DecisionDelta | null {
  const unique = new Map<string, { decision: StrategyDecision; index: number }>();
  decisions.forEach((decision, index) => {
    if (!comparableDecision(decision)) return;
    unique.set(decision.decision_key || `legacy-${index}`, { decision, index });
  });
  const ordered = [...unique.values()].sort((left, right) => {
    const timeOrder = Date.parse(left.decision.decided_at) - Date.parse(right.decision.decided_at);
    if (timeOrder) return timeOrder;
    const keyOrder = compareText(
      left.decision.decision_key || "",
      right.decision.decision_key || "",
    );
    return keyOrder || left.index - right.index;
  }).map(({ decision }) => decision);
  const targetMap = mapNumber ?? ordered[ordered.length - 1]?.map_number;
  const currentMap = ordered.filter((decision) => decision.map_number === targetMap);
  if (currentMap.length < 2) return null;
  return compareDecisions(currentMap[currentMap.length - 2], currentMap[currentMap.length - 1]);
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
  }[reason] || reason;
}

function comparableDecision(decision: StrategyDecision): boolean {
  const reason = decision.reason.toLocaleLowerCase("en-US");
  return !reason.includes("invalid")
    && !reason.includes("mismatch")
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
  if (delta.versionChanged) changes.push("策略版本已切换");
  if (delta.directionChanged) {
    changes.push("策略关注方向发生变化");
  } else {
    if (meaningful(delta.marketProbabilityDelta)) {
      changes.push(changeText("市场概率", Number(delta.marketProbabilityDelta)));
    }
    if (delta.roshDirectionChanged) {
      changes.push(`Rosh 方向转为${roshDirectionLabel(delta.currentRoshDirection)}`);
    }
    if (meaningful(delta.edgeDelta)) {
      changes.push(changeText("模型 Edge", Number(delta.edgeDelta)));
    }
  }
  if (!changes.length && delta.reasonChanged) changes.push("判断原因发生变化");
  if (!changes.length) changes.push("关键指标变化有限");
  const outcome = delta.verdictChanged
    ? `最终策略由${verdictLabel(delta.previousVerdict).replace("策略", "")}变为${verdictLabel(delta.currentVerdict).replace("策略", "")}`
    : `策略结论保持${verdictLabel(delta.currentVerdict).replace("策略", "")}`;
  return `${changes.slice(0, 2).join("，")}，${outcome}。`;
}

function changeText(label: string, value: number): string {
  const direction = value > 0 ? "上升" : "下降";
  return `${label}${direction} ${Math.abs(value * 100).toFixed(1)} 个百分点`;
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

function compareText(left: string, right: string): number {
  if (left === right) return 0;
  return left < right ? -1 : 1;
}
