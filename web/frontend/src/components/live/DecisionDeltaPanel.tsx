import { ArrowRight } from "@phosphor-icons/react";
import { useMemo, type ReactNode } from "react";

import { formatDateTime, formatPercent } from "../../format";
import type { StrategyDecision } from "../../types";
import {
  decisionReasonLabel,
  latestDecisionDelta,
  roshDirectionLabel,
  verdictLabel,
} from "./decisionDelta";

interface DecisionDeltaPanelProps {
  decisions: StrategyDecision[];
  mapNumber?: number | null;
  teamOne: string;
  teamTwo: string;
}

export function DecisionDeltaPanel({
  decisions,
  mapNumber,
  teamOne,
  teamTwo,
}: DecisionDeltaPanelProps) {
  const delta = useMemo(
    () => latestDecisionDelta(decisions, mapNumber),
    [decisions, mapNumber],
  );
  if (!delta) return null;

  const previousSide = sideLabel(delta.previous.underdog_side, teamOne, teamTwo);
  const currentSide = sideLabel(delta.current.underdog_side, teamOne, teamTwo);
  const probabilityContext = delta.directionChanged;

  return (
    <section className="decision-delta" aria-label="与上次判断相比">
      <div className="decision-delta-heading">
        <div>
          <h3>与上次判断相比</h3>
          <p>{delta.summary}</p>
        </div>
        <span>第 {delta.current.map_number} 局 · {formatDateTime(delta.current.decided_at)}</span>
      </div>

      <dl className="decision-delta-grid">
        <DeltaRow
          label="结论"
          previous={verdictLabel(delta.previousVerdict)}
          current={verdictLabel(delta.currentVerdict)}
          change={delta.verdictChanged ? "已变化" : "未变化"}
        />
        {delta.directionChanged && (
          <DeltaRow
            label="策略方向"
            previous={previousSide}
            current={currentSide}
            change="方向变化"
          />
        )}
        <DeltaRow
          label="模型概率"
          previous={probabilityValue(
            delta.previous.model_probability,
            probabilityContext ? previousSide : null,
          )}
          current={probabilityValue(
            delta.current.model_probability,
            probabilityContext ? currentSide : null,
          )}
          change={formatPointDelta(delta.modelProbabilityDelta, probabilityContext)}
        />
        <DeltaRow
          label="市场概率"
          previous={probabilityValue(
            delta.previous.market_probability,
            probabilityContext ? previousSide : null,
          )}
          current={probabilityValue(
            delta.current.market_probability,
            probabilityContext ? currentSide : null,
          )}
          change={formatPointDelta(delta.marketProbabilityDelta, probabilityContext)}
        />
        <DeltaRow
          label="模型 Edge"
          previous={formatSignedPercent(delta.previous.edge)}
          current={formatSignedPercent(delta.current.edge)}
          change={formatPointDelta(delta.edgeDelta, probabilityContext)}
        />
        {delta.dataQualityDelta !== null && (
          <DeltaRow
            label="数据质量"
            previous={formatPercent(delta.previous.data_quality)}
            current={formatPercent(delta.current.data_quality)}
            change={formatPointDelta(delta.dataQualityDelta, false)}
          />
        )}
        {(delta.previousRoshDirection || delta.currentRoshDirection) && (
          <DeltaRow
            label="Rosh 方向"
            previous={roshDirectionLabel(delta.previousRoshDirection)}
            current={roshDirectionLabel(delta.currentRoshDirection)}
            change={delta.roshDirectionChanged ? "已变化" : "未变化"}
          />
        )}
        {delta.reasonChanged && (
          <DeltaRow
            label="判断原因"
            previous={decisionReasonLabel(delta.previous.reason)}
            current={decisionReasonLabel(delta.current.reason)}
            change="已变化"
          />
        )}
        {delta.versionChanged && (
          <DeltaRow
            label="策略版本"
            previous={delta.previous.strategy_version}
            current={delta.current.strategy_version}
            change="版本变化"
          />
        )}
      </dl>
    </section>
  );
}

function DeltaRow({
  change,
  current,
  label,
  previous,
}: {
  change: string;
  current: ReactNode;
  label: string;
  previous: ReactNode;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>
        <span>{previous}</span>
        <ArrowRight size={14} weight="bold" aria-hidden="true" />
        <strong>{current}</strong>
        <small>{change}</small>
      </dd>
    </div>
  );
}

function probabilityValue(value: number, side: string | null): string {
  return side ? `${formatPercent(value)} · ${side}` : formatPercent(value);
}

function formatSignedPercent(value: number): string {
  if (!Number.isFinite(value)) return "-";
  const sign = value > 0 ? "+" : "";
  return `${sign}${(value * 100).toFixed(1)}%`;
}

function formatPointDelta(value: number | null, directionChanged: boolean): string {
  if (directionChanged) return "方向变化，不直接比较";
  if (value === null || !Number.isFinite(value)) return "不可比较";
  const sign = value > 0 ? "+" : "";
  return `${sign}${(value * 100).toFixed(1)}pp`;
}

function sideLabel(side: string | undefined, teamOne: string, teamTwo: string): string {
  if (side === "team_one") return teamOne || "队伍一";
  if (side === "team_two") return teamTwo || "队伍二";
  return "方向不可判";
}
