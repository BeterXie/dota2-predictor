# ADR-0006：M4-C Core Scoring Gates

| 项目 | 内容 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-07-24 |
| 适用范围 | M3-C eligible-decision calibration cohort 的 M4-C promotion review |
| 关联计划 | `docs/betting-decision-production-readiness-development-plan-2026-07-24.md` |

## 背景

M3-C ready 只表示样本可正式评审。现有 report 能计算 Brier、log loss、market baseline 和 series-cluster bootstrap，但旧 promotion logic 只检查“是否计算”，没有判断数值好坏，因此不能形成可证伪的 M4-C passed/failed 决策。

仓库已有模型校准设计使用 Brier `<0.25` 与 log loss `<ln(2)` 作为绝对质量下限。对于投注决策，仅达到绝对下限仍不足以证明模型比 decision-time market baseline 提供增量信息；必须同时通过成对的相对 market gate。

## 决策

M4-C core scoring 必须在同一冻结 M3-C cohort 上同时通过四项 hard gate：

| Gate key | Metric | Pass 条件 |
|---|---|---|
| `absolute_brier` | Model Brier score | `<0.25` |
| `absolute_log_loss` | Model log loss | `<ln(2)` |
| `brier_vs_market` | `market_brier_score - model_brier_score` | Paired series-cluster bootstrap 90% CI lower bound `>0` |
| `log_loss_vs_market` | `market_log_loss - model_log_loss` | Paired series-cluster bootstrap 90% CI lower bound `>0` |

四项必须全部通过；等于边界不通过。任一已成功计算的 gate 未达到阈值，M4-C 总体为 `failed`，不能降为 provisional passed。

## Cohort 与 baseline

- Model 与 market 必须使用完全相同的 M3-C decision keys 和 confirmed outcomes；不得因某个模型/market metric 不利而使用不同样本。
- Market baseline 使用每条 decision 在 decision event time 持久化的 de-vigged `market_probability`；不得使用赛后 closing price、browser odds 或重新抓取的市场值。
- Cohort identity、data cutoff、event/series membership 和 exclusion 必须在 promotion spec 中冻结。
- Invalid/reconstructed/missing outcome 样本按 ADR-0004 处理，不能进入 scored cohort；missingness 仍进入 coverage gate。

## Bootstrap contract

- 比较为 paired：每个 bootstrap replicate 对 model 和 market 使用同一批重采样 decisions/outcomes。
- Cluster unit 是 `raybet_match_id` series；重采样 series 时保留该 series 内全部 M3-C decisions。
- 当前冻结方法为 deterministic 1,000-iteration percentile bootstrap。
- 90% interval 使用 replicate distribution 的 5th/95th percentiles。
- Improvement 定义为 `market metric - model metric`，因此正值代表 model 更好；pass 要求 lower bound 严格 `>0`。
- Seed、iterations、method 和 cohort identity 必须进入 report/spec hash；任何变更需要新的 promotion spec identity。

## 状态处理

| 情况 | M4-C core scoring 状态 |
|---|---|
| 四项均成功计算且通过 | `core_scoring_passed`，继续检查其他 M4-C gates |
| 任一成功计算的 gate 未通过 | `failed` |
| Metric/CI 未计算、series 不足、identity 不完整或 artifact/hash 缺失 | `review_required`/blocked，不得标记 failed 或 passed |

Core scoring passed 不是 M4-C passed。Active outcome coverage/diversity readiness 是 ADR-0008 的 M3 前置；ECE 由 ADR-0007 定义，event sensitivity 由 ADR-0008 定义；approval record 和 ADR-0005/0014 的其他完整性要求仍必须全部通过。Readiness shortfall 返回 not_ready/review_required，不作为本 gate 的 failed。

## 后果

- 一个绝对校准尚可但不能可靠优于 market 的模型会 M4-C failed。
- 两个相对 market gate 都使用 uncertainty-aware lower bound，避免只凭有利点估计 promotion。
- 该 gate 可能使 promotion 较难，但不会阻止 M1/M2 paper 链或 M3 readiness。
- M4-C failed 只能通过新的策略版本和新的 forward cohort 重新挑战，不能在原 cohort 上事后改 gate。

## 验证要求

- 四项明显通过：core status passed，但总体 M4-C 在其他 gate 未完成时仍 review_required。
- Brier/log loss 等于边界：failed。
- Improvement point positive 但 CI lower `<=0`：failed。
- Model/market decision keys 不一致：review_required/authority failure，不计算 promotion。
- Cluster seed/iterations/spec hash 改变：旧 decision record 不可复用。
- Golden report fixture 固定 improvement 方向，防止把负值误解释为 model 更好。
