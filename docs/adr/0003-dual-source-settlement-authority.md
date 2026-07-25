# ADR-0003：Dual-source Settlement Authority 与 M2-F

| 项目 | 内容 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-07-24 |
| 适用范围 | Filled shadow/paper order 的 postmatch reconciliation、正式结算和 M2 验收 |
| 关联计划 | `docs/betting-decision-production-readiness-development-plan-2026-07-24.md` |

## 背景

计划使用 `direct-only` 描述生产链，容易被误读为正式 settlement 也只依赖 RayBet。当前实现并非如此：它要求 RayBet final evidence 与 OpenDota evidence 同时存在，并对同一 strict mapping、`dota_match_id` 和 winner 完成 confirmed reconciliation。任何单源缺失、身份不一致或赛果冲突都不能产生正式、可评分的 settlement。

双源要求会增加 M2 的可用性依赖和等待时间，但省略该依赖只会让计划与实现不一致，并不会使当前代码更容易完成 M2。

## 决策

### Direct-only 的边界

`direct-only` 严格限定以下生产路径必须只使用 RayBet `source='direct'` transport：

- market current/previous 和 strategy decision；
- processed watermark；
- first successor 和 fill；
- order 的 market lineage；
- market projection、freshness 和 browser isolation。

OpenDota 不是市场源，不参与 entry、定价、策略概率、watermark、first successor 或 fill，也不能成为 RayBet/browser fallback。Browser 只允许进入 compare/audit projection，永不参与正式 settlement。

### 正式 settlement authority

Filled order 只有同时满足以下条件才可生成正式 settlement：

1. order 与 shadow map attempt 均为 `filled`，strict mapping、direct signal/fill lineage 和 bound Vision authority 可验证；
2. RayBet direct completed/final evidence 为 `confirmed`，并绑定正式 audit、transport、response state/artifact authority；
3. OpenDota evidence 为 `confirmed`，并绑定 immutable artifact/observation/content hash；
4. 两源映射到同一 strict mapping、`raybet_match_id`、`map_number` 和正整数 `dota_match_id`；
5. 两源 winner side 完全一致；
6. 两源 first-usable time 均晚于 order fill，settlement time 等于 reconciliation 首次完整可用时间；
7. reconciliation、map result、settlement authority snapshot、settlement ledger 和 `settled` notification outbox 可相互重放验证；实际 SMTP 投递状态不属于 settlement authority；
8. settlement 不处于 `review_required`，且没有后续 authority invalidation/conflict。

RayBet 或 OpenDota evidence 暂时缺失/延迟时，reconciliation 保持 `pending` 并重试；它不完成 M2。身份、winner、source authority 或 immutable lineage 冲突进入 sticky `manual_review`；该 order 不生成正式 settled sample，也不能通过人工覆盖计作 M2。

### M2-F 运行检查点

`M2-F` 只表示同一自然产生的 eligible order 已完成：

- `pending -> filled`；
- verified filled outbox payload/transaction/formal lineage；
- immutable decision/order/market/Vision/draft lineage；
- production report 中的 filled projection。

M2-F 是非正式运行检查点，不是发布里程碑，不等于 M2，不代表 outcome 已确认，也不得进入 settled count、ROI、Brier、calibration 或稳定性样本。同一 M2-F order 在 dual-source reconciliation 和正式 settlement 完成后可升级为 M2 evidence。

## 运行行为

| 状态 | 运行结果 | M2 |
|---|---|---|
| 两源 confirmed 且身份/winner 一致 | 写正式 settlement 与 settled outbox | 可完成 |
| 任一来源延迟/缺失 | reconciliation pending、继续重试并暴露 source age | 未完成 |
| 身份、winner 或 authority 冲突 | sticky manual review、禁止正式 settled sample | 未完成 |
| 只有 RayBet final | 不降级为 RayBet-only settlement | 未完成 |
| 只有 OpenDota | 不替代 RayBet direct final | 未完成 |

OpenDota 暂时不可用不改变市场/决策/fill 的 direct-only 行为，也不自动使 M1 失败；它必须将 M2 标记为 blocked/deferred。运维不得通过切换 browser、手写赛果或人工改 reconciliation 状态完成 M2。

## Canary 前置演练

在首个 live M2 canary 前，必须用一场近期已完成、无 paper order 的受支持正式地图演练：

- exact RayBet/OpenDota identity mapping；
- 两源 evidence authority 和 first-usable latency；
- confirmed、pending、winner conflict 和 identity conflict 路径；
- health/Web/report 对 source age、retry 和 blocker 的一致投影。

演练不创建 order、settlement、notification 或表现样本，也不能替代真实 M2。

## 后果

- M2 的日历时间同时受自然 entry/fill 频率和 postmatch 双源可用性影响，不能承诺固定完成日。
- OpenDota 成为正式外部依赖，必须进入 health、运行手册、风险和证据包。
- M2-F 提供“成交链已工作”的进展可见性，但不能弱化正式 M2。
- 当前 dual-source 实现无需因本 ADR 放宽；文档、tests 和 report 必须反映其真实行为。
- M3 的 calibration/performance cohort 由 ADR-0004 定义。
- Notification outbox 与 SMTP delivery health 的边界由 ADR-0013 定义。

## 验证要求

- `tests/test_settlement_authority.py`：filled-only、source evidence、authority snapshot 和重放验证；
- `tests/test_postmatch_settlement.py`：RayBet/OpenDota exact reconciliation、pending 和 sticky manual review；
- `tests/test_notification_outbox.py`：filled/settled outbox 与 settlement 的事务边界；
- `tests/test_live_report.py`：M2-F 不进入 settled metrics，invalid/manual-review settlement 被隔离；
- live evidence：同一 order 的 M2-F、两源 evidence、confirmed reconciliation、settlement、settled outbox payload/transaction/formal lineage 和 report lineage。
