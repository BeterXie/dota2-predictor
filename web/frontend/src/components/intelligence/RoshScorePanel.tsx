import { Button } from "@fluentui/react-components";
import { CaretDown, CaretUp, Sword } from "@phosphor-icons/react";
import { useState } from "react";
import type {
  IntelligenceRoshLineupScoreSection,
  IntelligenceRoshMinutePoint,
} from "../../types";
import { selectRoshMinutePoints } from "../../utils/intelligenceUtils";
import { AdvantageSparkline } from "../common/AdvantageSparkline";

function percent(value: number | null | undefined): string {
  if (value == null) return "-";
  return `${(value * 100).toFixed(1)}%`;
}

function signedDecimal(value: number | null | undefined): string {
  if (value == null) return "-";
  return `${value > 0 ? "+" : ""}${value.toFixed(2)}`;
}

function percentagePoints(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "-";
  return `${value.toFixed(1)}%`;
}

function roshAdvantageLabel(score: number | null, radiant: string, dire: string): string {
  if (score == null) return "评分不可用";
  if (Math.abs(score) < 0.05) return `均势 ${Math.abs(score).toFixed(2)}`;
  return `${score > 0 ? radiant : dire} 占优 ${Math.abs(score).toFixed(2)}`;
}

export function RoshScorePanel({
  dire,
  radiant,
  score,
}: {
  dire: string;
  radiant: string;
  score: IntelligenceRoshLineupScoreSection | null;
}) {
  const data = score?.status === "available" ? score.data : null;
  if (!data) {
    return (
      <section className="intel-rosh-score unavailable" aria-label="Rosh 阵容评分">
        <header>
          <div>
            <h3>Rosh 阵容评分</h3>
            <p>历史 OpenDota 比赛的 dematus 阵容评分</p>
          </div>
          <Sword size={19} aria-hidden="true" />
        </header>
        <div className="intel-rosh-empty">
          <strong>当前没有可展示的 Rosh 阵容评分</strong>
          <span>{score?.reason || "历史阵容评分尚未生成"}</span>
        </div>
      </section>
    );
  }

  const coverage = Math.max(0, Math.min(10, data.player_coverage_count)) / 10;
  const hasCurrentCorrection = data.current_player_adjusted_lineup_score != null;
  const displayStatus = score?.status === "available" ? "可展示" : score?.status || "未知";
  const points = data.pure_minute_table || [];

  return (
    <section className="intel-rosh-score" aria-label="Rosh 阵容评分">
      <header>
        <div>
          <h3>Rosh 阵容评分</h3>
          <p>纯阵容分与当前 STRATZ 选手修正分分开显示</p>
        </div>
        <Sword size={19} aria-hidden="true" />
      </header>
      <div className="intel-rosh-score-grid">
        <div>
          <dt>纯阵容分</dt>
          <dd>{roshAdvantageLabel(data.pure_lineup_score, radiant, dire)}</dd>
        </div>
        <div>
          <dt>当前选手修正分</dt>
          <dd>
            {hasCurrentCorrection
              ? roshAdvantageLabel(data.current_player_adjusted_lineup_score, radiant, dire)
              : "不可用"}
          </dd>
        </div>
        <div>
          <dt>最终展示分</dt>
          <dd className="score-primary">
            {roshAdvantageLabel(data.effective_lineup_score, radiant, dire)}
          </dd>
        </div>
        <div>
          <dt>选手覆盖</dt>
          <dd>{percent(coverage)} ({data.player_coverage_count}/10)</dd>
        </div>
        <div>
          <dt>评分模式</dt>
          <dd>{data.scoring_mode === "current_player_adjusted" ? "当前选手修正" : "纯阵容"}</dd>
        </div>
        <div>
          <dt>状态</dt>
          <dd>{displayStatus}</dd>
        </div>
      </div>

      <dl className="intel-rosh-score-meta">
        <div><dt>公式版本</dt><dd><code>{data.formula_version}</code></dd></div>
        <div><dt>数据源</dt><dd><code>{data.source_name}</code></dd></div>
        <div><dt>阵容数据时间</dt><dd><code>{data.source_as_of}</code></dd></div>
        <div><dt>选手数据时间</dt><dd><code>{data.player_stats_as_of || "未提供"}</code></dd></div>
      </dl>

      {points.length > 0 && (
        <div style={{ padding: "0 12px" }}>
          <AdvantageSparkline points={points} radiantName={radiant} direName={dire} />
        </div>
      )}

      <p className="intel-rosh-score-warning">
        当前选手修正来自当前 STRATZ 数据，不是比赛当时快照；该修正不可用于历史回测或下注证据。
      </p>

      <RoshMinuteTable
        adjusted={data.current_player_adjusted_minute_table}
        dire={dire}
        pure={data.pure_minute_table}
        radiant={radiant}
      />
    </section>
  );
}

function RoshMinuteTable({
  adjusted,
  dire,
  pure,
  radiant,
}: {
  adjusted: IntelligenceRoshMinutePoint[] | null;
  dire: string;
  pure: IntelligenceRoshMinutePoint[];
  radiant: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const adjustedByMinute = new Map((adjusted || []).map((point) => [point.minute, point]));
  const keyPoints = selectRoshMinutePoints(pure);
  const visiblePoints = expanded ? pure : keyPoints;
  const canExpand = keyPoints.length < pure.length;

  return (
    <div className="intel-rosh-curve">
      <div className="intel-rosh-curve-heading">
        <div>
          <strong>20-60 分钟优势曲线</strong>
          <span>默认显示每 5 分钟与优势反转点</span>
        </div>
        {canExpand && (
          <Button
            appearance="subtle"
            aria-expanded={expanded}
            icon={expanded ? <CaretUp size={14} /> : <CaretDown size={14} />}
            onClick={() => setExpanded((value) => !value)}
            size="small"
          >
            {expanded ? "收起完整记录" : `展开全部 ${pure.length} 个时间点`}
          </Button>
        )}
      </div>
      {!pure.length ? (
        <div className="intel-rosh-curve-empty">暂无可展示的 20-60 分钟纯阵容数据</div>
      ) : (
        <div className={`intel-table-scroll intel-rosh-table-scroll ${expanded ? "expanded" : ""}`}>
          <table className="intel-table intel-rosh-table">
            <thead>
              <tr>
                <th>时间</th>
                <th>样本时间窗</th>
                <th>纯阵容优势</th>
                <th>当前选手修正优势</th>
                <th>样本覆盖</th>
                <th>纯调整</th>
                <th>选手调整</th>
              </tr>
            </thead>
            <tbody>
              {visiblePoints.map((point) => {
                const current = adjustedByMinute.get(point.minute);
                return (
                  <tr key={point.minute}>
                    <td className="intel-number">{point.minute} 分钟</td>
                    <td className="intel-number">{point.time_start}-{point.time_end} 分钟</td>
                    <td>{roshAdvantageLabel(point.win_rate_graph, radiant, dire)}</td>
                    <td>
                      {current
                        ? roshAdvantageLabel(current.win_rate_graph, radiant, dire)
                        : "不可用"}
                    </td>
                    <td className="intel-number">{percentagePoints(point.match_percentage)}</td>
                    <td className="intel-number">{signedDecimal(point.hero_adjustment + point.synergy_adjustment)}</td>
                    <td className="intel-number">{current ? signedDecimal(current.player_adjustment) : "-"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
