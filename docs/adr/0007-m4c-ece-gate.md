# ADR-0007：M4-C ECE Gate

| 项目 | 内容 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-07-24 |
| 适用范围 | M3-C eligible-decision calibration cohort 的 calibration-shape promotion gate |
| 关联计划 | `docs/betting-decision-production-readiness-development-plan-2026-07-24.md` |

## 背景

ADR-0006 约束整体 proper-scoring-rule 表现，但 Brier/log loss 即使通过，也可能掩盖局部概率区间的系统性偏高或偏低。当前 report 已输出五个 equal-count bins 的 ECE 点估计，但没有最低 bin support、cluster-bootstrap uncertainty 或 promotion threshold；当前实现还用 outcome 作为相同 probability 的排序 tie-break，不适合作为冻结的 promotion contract。

## 决策

M4-C 必须同时通过以下 ECE hard gates：

| Gate key | Pass 条件 |
|---|---|
| `ece_bin_support` | 正好五个 equal-count bins，且每箱至少 50 条 confirmed M3-C samples |
| `ece_point` | Expected calibration error `<=0.05` |
| `ece_upper` | Series-cluster bootstrap 90% CI upper bound `<=0.08` |

等于边界通过；超过任一数值边界即 M4-C `failed`。Bin 数量/support 或 CI 无法满足时保持 `review_required`，不能标记 passed 或 failed。

## 分箱合同

- 使用完整、冻结的 M3-C scored cohort；每条 decision 只出现一次。
- 按 `(model_probability, decision_key)` 升序确定性排序，划分五个尽量等大的 equal-count bins。
- 禁止使用 outcome 作为 tie-break，避免在相同 probability 下让分箱依赖赛果。
- 每箱输出 count、mean probability、observed rate 和 absolute calibration gap。
- ECE 为各箱 absolute gap 按 count 加权的平均值。
- Point estimate、bin membership 和 report hash 必须可重放。

## Bootstrap contract

- 与 ADR-0006 一致，以 `raybet_match_id` series 为 cluster，deterministic 1,000 iterations，使用 percentile 90% interval。
- 每个 replicate 对抽中的 series 保留全部 decisions，并按本 ADR 的排序/分箱规则重新构造五箱。
- Bootstrap seed、iterations、method、cohort identity 和 binning version 进入 promotion spec/report hash。
- Replicate 无法形成五个满足 support 的 bins 时，该 replicate 不可静默删除；总体 gate 标记 unavailable，并保持 `review_required`。

## 状态组合

ECE 三项全部通过只表示 `ece_gate_passed`。总体 M4-C 仍需 active ADR-0008 M3 readiness、ADR-0006 core scoring、ADR-0008 event sensitivity 和完整 approval record 全部通过；coverage/diversity shortfall 返回 not_ready/review_required，不是 ECE failed。

若 ECE point 或 upper bound 超过阈值，即使 Brier/log loss 与 market-improvement gate 通过，M4-C 仍为 `failed`。

## 后果

- 平均校准误差目标不超过 5 个百分点；考虑 series-level 不确定性后的 90% 上界不超过 8 个百分点。
- M3-C 最低 500 样本通常可提供约 100 条/箱，但实际 promotion 仍显式验证每箱 support。
- Report 需要新增 ECE cluster-bootstrap interval，并修正相同 probability 的 outcome-dependent tie-break。
- Outcome coverage、diversity 和 event sensitivity threshold 由 ADR-0008 定义。

## 验证要求

- ECE `0.05`、upper `0.08` 且 support 满足：通过 ECE gate。
- 任一数值高于边界：M4-C failed。
- 只有四个有效 bins、任一 bin `<50` 或 bootstrap unavailable：review_required。
- 相同 probabilities、不同 outcomes：改变输入行顺序不改变 bin membership/result。
- Golden fixture 固定 binning version、seed、iterations、point/interval 和 report hash。
