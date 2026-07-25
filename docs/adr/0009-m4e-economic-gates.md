# ADR-0009：M4-E Economic Gates

| 项目 | 内容 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-07-24 |
| 适用范围 | M3-E filled-settled cohort 的 M4-E promotion review |
| 关联计划 | `docs/betting-decision-production-readiness-development-plan-2026-07-24.md` |

## 背景

M3-E ready 只表示经济样本具备评审条件。没有明确的 ROI uncertainty、slippage 和 drawdown 阈值，报告即使计算出不利结果也无法形成可证伪的 M4-E passed/failed 决策。

## 决策

同一冻结 M3-E cohort 必须同时通过以下 hard gates：

| Gate key | Pass 条件 |
|---|---|
| `roi_point` | Stake-weighted ROI point estimate `>0` |
| `roi_lower` | Series-cluster bootstrap 90% CI lower bound `>0` |
| `mean_adverse_slippage` | `<=1%` |
| `p95_adverse_slippage` | `<=2%` |
| `maximum_adverse_slippage` | `<=3%` |
| `maximum_drawdown_units` | Peak-to-trough drawdown `<=20` stake units |

ROI 定义为 `(sum(stake * return_units) - sum(stake)) / sum(stake)`。Bootstrap 与 ADR-0006 使用同一冻结方法：以 `raybet_match_id` 为 cluster，保留 series 内全部 orders，deterministic 1,000 iterations，以第 5/95 percentile 形成 90% interval。

每条 filled order 使用 decimal odds；raw signed slippage 为 `(signal_price - fill_price) / signal_price`，其中 signal price 来自 signal direct transport，fill price 来自不可跳过的 first processed direct successor。Adverse slippage 为 `max(0, raw_slippage)`，因此 favorable fill 以零进入全部 M3-E orders 的 mean/P95/maximum；三项按 order 等权，不按 stake 加权。P95 使用按 `(adverse_slippage, order_key)` 排序后的 nearest-rank `ceil(0.95 * n)`，避免实现间插值漂移。1%/2%/3% 是比率，不是 implied-probability 百分点。

Drawdown 使用按 `(settled_at, order_key)` 排序的已结算 PnL 序列，从零开始累积 `stake * (return_units - 1)`，取历史峰值到后续谷值的最大下降。阈值等于 1%、2%、3% 或 20 units 时通过；ROI 等于零不通过。

任一已计算 hard gate 不满足即 M4-E `failed`。Metric、interval、排序 authority、cohort identity 或 artifact/hash 不完整时保持 `review_required`。M4-E 以前置的 active ADR-0008 M3-E maturity/diversity readiness 为必要条件；readiness shortfall 返回 M3 `not_ready`/M4 `review_required`，不算 M4-E failed。M4-E 还必须通过 ADR-0008 leave-one-event-out gate 和 ADR-0005 的 approval contract。

因为 slippage rejection 已在 M3-E 入选前发生，这些 slippage gates 只描述 filled cohort；selection audit 必须另报全部 eligible orders 的 slippage rejection rate，不能把截断后的 M3-E slippage 称为全部执行质量。

任何 stake increase proposal 必须对同一 strategy proposal 同时取得 active、non-revoked 的 M4-C `passed` 和 M4-E `passed`；单独的 M4-E passed、M3 ready 或有利 ROI point estimate 都不授权增加 stake。

## 后果

- 经济 promotion 同时要求正收益的不确定性证据和受控执行风险。
- 当前 report 只输出 mean raw slippage、ROI 和 drawdown，仍需实现 adverse P95/maximum、ROI hard gate 和明确状态组合。
- 3% maximum 与当前 order fill slippage ceiling 对齐，但 promotion report 仍必须独立验证，不能假定执行 gate 永远正确。

## 验证要求

- ROI point positive 但 CI lower `<=0`：failed。
- Favorable slippage 以零而非负数抵消 adverse slippage；nearest-rank P95 边界可重放。
- Drawdown 序列对相同 settlement time 使用 `order_key` 稳定排序。
- 任一数值 gate 等于允许上界时通过；不可用 metric 保持 review_required。
- Stake increase 在任一 M4 track 未 passed 时 fail closed。
