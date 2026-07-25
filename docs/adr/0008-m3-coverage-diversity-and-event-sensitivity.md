# ADR-0008：M3 Outcome Coverage、Diversity 与 Event Sensitivity

| 项目 | 内容 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-07-24 |
| 适用范围 | M3-C/M3-E readiness 与 M4-C/M4-E event-sensitivity review |
| 关联计划 | `docs/betting-decision-production-readiness-development-plan-2026-07-24.md` |

## 背景

仅要求 500 条样本和两个 event，可能让单一赛事、少量 series 或选择性缺失主导结论。Outcome provider 也需要合理时间完成赛后映射；把刚结束地图的暂时缺失直接算成永久 missing，会把 provider latency 与模型质量混在一起。

## 决策

### Mature outcome 与 coverage

Analysis data cutoff 为 UTC `T` 时，只有独立持久化的权威 map completion time `<= T - 72 hours` 的 eligible decision 才是 **outcome-mature eligible decision**；端点包含。Completion time 必须独立于 label/settlement 到达时间，不能从 numerator 反推。完成时间缺失、冲突或无法绑定 strict identity 时标记 `maturity_unknown`，不得静默排除或把 coverage readiness 标记为通过。`maturity_status` 与 `label_status` 正交；72 小时是成熟窗口，不是 provider SLA。

对同一冻结 cohort identity 和 cutoff，M3-C 必须同时满足：

| Gate key | Pass 条件 |
|---|---|
| `outcome_coverage_overall` | confirmed dual-source outcome labels / mature eligible decisions `>=95%` |
| `outcome_coverage_by_event` | 每个纳入 event 的 confirmed coverage `>=90%` |
| `outcome_coverage_resolution_gap` | filled 与 non-filled mature eligible decisions 的 confirmed coverage 绝对差 `<=5` 个百分点 |

Coverage denominator 是 cutoff 时全部有效、outcome-mature eligible decisions；per-event 集合也从该 denominator 取得，不能从 confirmed subset 取得。`non-filled` 固定为 `rejected|pending|no_order`。Overall denominator、filled denominator 或 non-filled denominator 为零，或存在 `maturity_unknown` 时，gate unavailable 且 M3-C `not_ready`，不能按 0%/100% 或自动零 gap 处理。Coverage 必须同时报告 numerator、denominator、pending/missing/manual-review/invalid/maturity-unknown 原因和两个 resolution 分组；不得插补 outcome、删除不利 event 或用 formal settlements 代替 eligible-decision denominator。

Coverage shortfall 是 M3 data-readiness failure：相关 track 保持 `not_ready`，不是 M4 `failed`。P6 只验证冻结的 active M3 readiness evidence/hash，不重新把 coverage 解释为 performance gate。

### M3 diversity readiness

M3-C 和 M3-E 分别、独立满足以下全部条件后才可标记 ready：

- 至少 500 条来自 outcome-mature maps 的该 track 有效 forward samples；M3-C 按 confirmed calibration decisions 计数，M3-E 按 filled-settled orders 计数；
- 至少 100 个不同的 `raybet_match_id` series；
- 至少 3 个 strict event；
- 每个纳入 event 至少 50 条有效 samples；
- 任一 event 的 samples 不超过该 track 总数的 50%；全部计数按样本条数，不按 stake 加权。

两个 track 的样本、series 或 event 计数不得相加。Event identity 使用 decision cutoff 时 strict mapping 绑定的 `event_id`，series identity 使用 `raybet_match_id`。不得为了满足 concentration gate 事后删除完整有效样本；cutoff、event membership 和 exclusion policy 必须在 readiness evidence 与 promotion specification 中冻结。

### Leave-one-event-out hard gate

M3 ready 前必须证明每个 leave-one-event-out slice 可计算；slice support 或 identity 不可验证时 M3 保持 `not_ready`。M4 必须在冻结 membership 上对每个 event 逐一删除其全部样本，不重新筛选 cutoff/event/exclusion，再重算相关 core point metric。每个 slice 都不得反转有利方向：

- M4-C：`market_brier - model_brier >0` 且 `market_log_loss - model_log_loss >0`；
- M4-E：stake-weighted ROI `>0`。

任一可计算 slice 等于零或反向时，对应 M4 track 为 `failed`。M4 执行时若 readiness hash、event identity、slice 或 metric 不再可靠，返回 M3 `not_ready`/M4 `review_required`；不得把不可用解释为通过。

## 后果

- 500 条仍是最低行政门槛，但不再允许 490/10 的双 event 集中样本进入正式 promotion review。
- 72 小时只定义 outcome maturity，不是 provider SLA，也不能允许已经成熟的 missing outcome 被忽略。
- M3 readiness 变得更慢但仍与 M4 pass/fail 分离；达到 diversity/coverage 只说明具备正式评审条件。

## 验证要求

- Cutoff 边界固定验证 `T-72h` 含端点，以及 `T-72h` 之后地图不进入 mature denominator。
- Overall、per-event 和 filled/non-filled gap 分别有边界值测试；95%、90% 和 5 个百分点等于边界均通过。
- 两个 track 分别验证 500 samples、100 series、3 events、每 event 50 和最大 50% concentration。
- Leave-one-event-out 的正向、等于零、反向和 unavailable fixtures 产生确定状态。
