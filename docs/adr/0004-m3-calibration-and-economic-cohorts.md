# ADR-0004：M3 Calibration 与 Economic Cohort 分轨

| 项目 | 内容 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-07-24 |
| 适用范围 | Comeback v4 forward evaluation、calibration、经济表现与 stake sizing |
| 关联计划 | `docs/betting-decision-production-readiness-development-plan-2026-07-24.md` |

## 背景

当前 report 只有在 order 为 `filled`、存在非 review settlement 且 reconciliation confirmed 时才生成 binary outcome；Brier、log loss、calibration 和 ROI 随后在同一 filled-settled 子集上计算。

Fill/reject 取决于 successor price、slippage、market state、outcome identity 和 transport availability。这些执行条件可能与赛果或市场变化相关。只用 filled orders 评估全部 eligible signals 的概率质量，会把执行选择机制混入 calibration estimand，产生选择偏差。反过来，用未成交 decisions 计算 ROI 或 stake performance 也没有经济意义。

## 决策

M3 使用两个互不混算的 forward cohort 和 readiness track。

### M3-C：Eligible-decision calibration cohort

样本单位是一条 prospectively recorded、自然产生、authority 完整的 `eligible=true` strategy decision。它是否 filled、rejected 或最终仍未成交，不影响入选资格；但必须在赛后取得 ADR-0003 同级别的 RayBet/OpenDota confirmed outcome label。

M3-C 用于：

- Brier score 与相对 market Brier improvement；
- log loss 与相对 market log-loss improvement；
- ECE、calibration bins/intercept/slope；
- probability、entry threshold 和 Rosh direction 分析；
- outcome-label coverage 与 missingness sensitivity。

Eligible decision 的 outcome label 是独立赛后事实，不是 settlement。Rejected、unfilled 或 pending order 可以被 label，但不得因此创建 settlement、return units、PnL 或 ROI。

### M3-E：Filled-settled economic/execution cohort

样本单位是一条真实 `filled` order，且必须有 ADR-0003 定义的 confirmed dual-source reconciliation、非 review formal settlement 和完整 notification/report lineage。

M3-E 用于：

- slippage 和 signal-to-fill latency；
- return units、PnL、ROI 和 drawdown；
- stake sizing 与执行策略分析；
- event/series concentration 和经济稳定性。

M2-F 在 outcome label 与 formal settlement 尚未完成时不进入任何 scored cohort。取得 confirmed outcome label 后可进入 M3-C；只有正式 settlement 后才进入 M3-E。

## 共同不变量

1. 两个 cohort 必须使用 prospectively recorded decision/order；不得使用 reconstructed decision 冒充 forward sample。
2. Outcome label 缺失、pending 或 manual review 时不评分；达到 ADR-0008 的 72 小时 maturity 后，必须进入 coverage/missingness denominator 和原因分桶。
3. Invalid entry evidence、authority conflict、未来数据、browser market lineage 或不完整 identity 不进入任一 scored cohort。
4. 完整 cohort identity 至少包含 strategy、model、feature、calibration、draft deployment 和 global gate hash；identity 不兼容的样本不得 pooling。
5. M3-C 与 M3-E 必须有独立 denominator、sample tier、headline、bootstrap/event sensitivity 和 readiness 状态；两个样本数不得相加。
6. 同一 filled-settled order 可以分别代表其 eligible decision 进入 M3-C、代表其成交 order 进入 M3-E，但两条记录具有不同 estimand，不能在同一指标中重复或混算。
7. 100 条仍是每个 track 的 provisional 分界；正式 M3 readiness 必须由每 track 至少 500 mature samples、100 series、3 events、event support/concentration 和 LOEO 可计算性共同决定，M3-C 另须通过 ADR-0008 outcome coverage。这些不是统计通过标准；M3/M4 边界由 ADR-0005 定义。

## Outcome-label projection

需要建立不写 settlement 的 decision outcome-label projection，最少绑定：

- `decision_key`、strategy/cohort identity；
- strict mapping、`raybet_match_id`、`map_number`、`dota_match_id`；
- RayBet/OpenDota evidence IDs、refs、content hashes 和 first-usable times；
- confirmed winner side 和 decision underdog binary outcome；
- label status/reason：confirmed、pending、missing、manual_review、invalid；
- order resolution：none、pending、filled、rejected，仅用于 missingness/selection audit。

该 projection 可以复用 immutable postmatch authority，但不得通过伪造 `shadow_orders.status` 或 `settlements` row 获得 label。

## 报表合同

Report 必须分别输出：

- `eligible_decision_calibration`：eligible total、confirmed labels、coverage、missingness、M3-C metrics；
- `filled_settled_economics`：orders、filled、settled、M3-E execution/economic metrics；
- `selection_audit`：按 filled/rejected/pending、event、minute、odds 和 reason 比较 outcome coverage；
- `cohort_identity`：每个完整 identity 独立结果，以及 `incompatible_cohorts_not_pooled`。

不得再用一个 `settled_orders` denominator 同时代表概率 calibration 的目标总体和经济表现总体。

Fill/reject rate 使用全部 eligible orders 作为执行诊断 denominator，属于 `selection_audit`；它不以 M3-E 的 filled-settled 样本为分母。

## 后果

- M3-C 样本通常会比 M3-E 更快积累，但仍受双源 outcome coverage 约束。
- Threshold/calibration 建议与 stake sizing 建议可以在不同时间达到 review readiness，必须分别标记。
- 需要扩展 postmatch/report，使未成交 eligible decisions 获得权威 outcome label，而不创建 settlement。
- 现有 filled-only Brier/calibration headline 只能解释为 filled-order 子集描述，不能继续作为全部 eligible decisions 的校准结论。
- 本 ADR 不重复统计 promotion gate 的具体阈值；M4-C 由 ADR-0006/0007/0008，M4-E 由 ADR-0008/0009 定义。M3 ready 仍不等于 M4 passed。

## 验证要求

- Eligible + rejected order + confirmed outcome：进入 M3-C，不进入 M3-E，无 settlement/ROI。
- Eligible + filled + confirmed settlement：decision 进入 M3-C，order 进入 M3-E，各计一次且指标不混算。
- Eligible + missing/manual-review outcome：进入 coverage denominator，不进入 scored metrics。
- Non-eligible、reconstructed、invalid authority 或 browser-lineage decision：不进入任一 scored cohort。
- Incompatible cohort identities：分别报告，headline 为 not pooled。
- Golden report fixture 明确断言两个 denominator、sample tiers 和 readiness 状态不同。
