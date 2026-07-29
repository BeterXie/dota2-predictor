import type { IntelligenceMatchRating } from "../../types";
import { ComparisonBar } from "../common/ComparisonBar";

function decimal(value: number | null | undefined, precision = 2): string {
  if (value == null) return "-";
  return value.toFixed(precision);
}

function percent(value: number | null | undefined): string {
  if (value == null) return "-";
  return `${(value * 100).toFixed(1)}%`;
}

function formatCutoff(iso: string | null | undefined): string {
  if (!iso) return "未指定截止";
  return iso.slice(0, 10);
}

export function MatchRatingPanel({
  rating,
  radiant,
  dire,
}: {
  rating: IntelligenceMatchRating | null;
  radiant: string;
  dire: string;
}) {
  if (!rating) {
    return (
      <section className="intel-match-rating unavailable" aria-label="比赛综合评分">
        <strong>比赛综合评分</strong>
        <span>当前评分证据不满足 10 人同版本、同基准截止条件</span>
      </section>
    );
  }

  const radiantScore = rating.radiant.execution_score ?? 50;
  const direScore = rating.dire.execution_score ?? 50;

  return (
    <section className="intel-match-rating" aria-label="比赛综合评分">
      <header>
        <div>
          <h3>比赛综合评分</h3>
          <p>{rating.player_count} 名当前版本选手评分的算术平均</p>
        </div>
        <code>{rating.rating_version}</code>
      </header>

      <div style={{ padding: "0 14px 10px" }}>
        <ComparisonBar
          leftLabel={radiant}
          leftValue={radiantScore}
          rightLabel={dire}
          rightValue={direScore}
          unit="分"
          precision={2}
          leftColor="var(--accent, #61cec1)"
          rightColor="var(--team-two, #ef8b79)"
        />
      </div>

      <div className="intel-match-rating-groups">
        <MatchRatingGroup label="全场" values={rating.overall} />
        <MatchRatingGroup label={radiant} values={rating.radiant} />
        <MatchRatingGroup label={dire} values={rating.dire} />
      </div>
      <footer>
        <span>来源版本 <code>{rating.source_score_version}</code></span>
        <span>基准截止 <code>{formatCutoff(rating.benchmark_cutoff)}</code></span>
      </footer>
    </section>
  );
}

function MatchRatingGroup({
  label,
  values,
}: {
  label: string;
  values: IntelligenceMatchRating["overall"];
}) {
  return (
    <div className="intel-match-rating-group">
      <strong>{label}</strong>
      <dl>
        <div><dt>执行分</dt><dd>{decimal(values.execution_score, 2)}</dd></div>
        <div><dt>赛果修正</dt><dd>{decimal(values.result_adjusted_score, 2)}</dd></div>
        <div><dt>覆盖率</dt><dd>{percent(values.coverage)}</dd></div>
      </dl>
    </div>
  );
}
