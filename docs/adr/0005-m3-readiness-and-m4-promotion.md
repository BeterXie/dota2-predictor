# ADR-0005：M3 Readiness 与 M4 Promotion 分离

| 项目 | 内容 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-07-24 |
| 适用范围 | Comeback v4 forward sample readiness、统计 promotion 和策略版本变更 |
| 关联计划 | `docs/betting-decision-production-readiness-development-plan-2026-07-24.md` |

## 背景

计划原先把“样本达到 500/跨 event”与“通过 bootstrap、calibration、market baseline、return/slippage/drawdown gate”放在同一个 M3 叙述中。这会让团队无法判断 M3 到底表示“有资格评审”还是“策略已经通过”。

当前 report 的行为实际支持分离：样本和 event 门槛达到后只进入 `stability_review_required`，promotion gate 仍因 calibration、market baseline 和 economic gate 未记录/未批准而保持 not passed。样本数量本身不是有效性或盈利证据。

## 决策

### M3 只表示评审就绪

M3-C/M3-E 继续使用 ADR-0004 的独立 cohort。相关 track 满足以下条件时可以标记 ready：

- 达到 ADR-0008 定义的该 track 最低 samples、series、event support 和 concentration 门槛；
- cohort identity、data cutoff、适用于该 track 的 ADR-0008 maturity/coverage/missingness 和 invalid-evidence isolation 完整；
- 所需 bootstrap/event-sensitivity 计算具备输入条件；
- report 可重放，并显示 `review_required` 而不是 passed。

M3 ready 只解锁正式统计评审。它不表示模型校准良好、优于市场、ROI 为正、stake 可增加或策略可部署。

### M4 才表示统计 promotion 决策

M4 分为两个独立 track：

- `M4-C`：以 M3-C ready 为前置，评审 probability calibration、market baseline、entry threshold 和 Rosh direction；
- `M4-E`：以 M3-E ready 为前置，评审 slippage、ROI/PnL、drawdown、event sensitivity 和 stake sizing。

涉及两个 estimand 的变更必须 M4-C 与 M4-E 都 passed。一个 track 的 passed 不得外推到另一个 track。

每个 M4 track 的状态只能是：

| 状态 | 含义 |
|---|---|
| `not_ready` | 对应 M3 track 尚未 ready |
| `review_required` | M3 ready，但缺正式 promotion decision |
| `failed` | 已按冻结 spec 评审，至少一项必需 gate 失败 |
| `passed` | 已按冻结 spec 评审，全部必需 gate 通过且正式 approval record 完整 |

`failed` 是合法、不可改写的评审结果，不撤销 M1/M2/M3。缺 spec、缺 metric、缺 interval、缺 approval 或 hash 不一致不能解释为 failed/passed，只能保持 `review_required` 或 fail closed。

## 预注册与决策记录

M4 promotion specification 必须在查看目标 cohort cutoff 的 gate 结果前冻结并 content-addressed，至少包含：

- 目标 change 和所需 M4 track；
- metrics、方向、threshold 和多 gate 组合逻辑；
- cluster/bootstrap unit、置信水平和 interval 计算；
- outcome coverage/missingness gate；
- market baseline、event sensitivity 和 concentration gate；
- M4-E 的 slippage、ROI、drawdown 与 stake 约束；
- cohort identity、版本兼容和 exclusion policy；
- failed、unavailable 和 manual-review 的处理规则。

Immutable promotion decision record 必须绑定：

- promotion spec hash；
- report/artifact hash；
- 完整 cohort identity 和 data cutoff；
- event/series membership；
- 每项 measured value、interval、threshold 和结果；
- M4-C/M4-E 总体状态；
- approver、approved/rejected at 和 decision reason。

## 版本与发布约束

- M4 passed 只允许创建 proposed 新策略版本、独立 backtest 和新的 forward plan；不自动部署。
- 任何 threshold、entry policy、Rosh direction、evidence contract、execution policy 或 stake sizing 变化都必须按 ADR-0010 使用新 strategy version 和 policy hash。
- 不得覆盖旧 M4 failed record，也不得在同一 cohort 上事后调整 metric、cutoff、event membership 或 exclusion 后沿用原 spec identity。
- 新 spec 可以启动新的评审，但必须有新 hash，并明确多重尝试与先前 failed 结果。

## 后果

- 达到 500 条样本但尚未满足 ADR-0008 其他 readiness 条件时仍是 not ready；全部 readiness 条件满足后也只显示 M3 ready/M4 review required，而不会自动显示 passed。
- 策略可能长期停在 M3 ready，或以 M4 failed 结束；这属于正常、诚实的结果。
- 需要 report/Web 支持 M3/M4 独立状态和 machine-readable promotion record。
- M3 coverage/diversity readiness 和 M4 event sensitivity 由 ADR-0008 定义；M4-C core scoring/ECE 由 ADR-0006/0007 定义，M4-E hard gate 由 ADR-0008/0009 定义。Coverage/diversity shortfall 使 M3 not_ready，不是 M4 failed；在全部 performance gates 和治理记录完成前 M4 保持 `review_required`。

## 验证要求

- M3 threshold 达到但无 promotion record：M3 ready，M4 review_required。
- 冻结 spec 某 gate 失败：M4 failed，不能被人工 approval 覆盖为 passed。
- 全部 gate 通过但缺 approver/hash：仍非 passed。
- M4-C passed、M4-E not ready：只允许校准类提案，不允许 stake/economic 声明。
- Incompatible cohort、cutoff 漂移或 spec/report hash mismatch：fail closed，不计算 passed。
- UI/report 不得把 `stability_review_required`、M3 ready 或 M4 failed 渲染为策略已通过。
