# ADR-0002：M1 Qualifying Strategy Rejection

| 项目 | 内容 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-07-24 |
| 适用范围 | Dota 2 direct-only live shadow/paper 决策链的 M1 验收 |
| 关联计划 | `docs/betting-decision-production-readiness-development-plan-2026-07-24.md` |

## 背景

系统会在多个层级 fail closed。`strict_live_ineligible`、`draft_landmark_unavailable`、`draft_authority_unavailable` 等 pre-strategy hard gate 可以在 canonical strategy evaluator 运行前持久化 `eligible=false`、空 contributions 的 `no_signal` decision。这类事实证明对应 gate 没有放行不完整输入，但不能证明完整的 direct market、Vision、mapping、draft、Rosh、profile/model 和策略求值链已经工作。

若任何持久化 rejection 都能完成 M1，一个始终缺 draft、Rosh 或 model 输入的系统也可能被错误标记为 direct-only accepted。

## 决策

M1 可以由 eligible decision，或满足本 ADR 全部条件的 qualifying strategy rejection 完成。Qualifying strategy rejection 必须：

1. 是 canonical evaluator 实际运行后产生的 scored strategy decision，而不是 response rejection、pre-strategy `no_signal`、`waiting_for_*` 或 order rejection；
2. `eligible=false`，且公开 contributions、conservative contributions、quality、market、Vision、entry 和 strategy-version inputs 均可重算；
3. exact strict event、match/team/map mapping 已批准且在 decision cutoff 有效；
4. current/previous market transport 均为 on-time、processed、`source='direct'`，身份完整、互不复用、event time 有序，winner market 完整；若 rejection 原因为价格波动超过 stability tolerance，两条完整 transport 仍必须存在；
5. current/previous Vision v4 均来自受支持 live layout，confirmed、fresh、未暂停，side、clock、kills 和 canonical net-worth evidence 完整；正式 Vision decision authority 可验证；
6. frozen draft deployment、active draft landmark authority、Rosh lineup score/selected minute 和 draft identity 均存在且一致；
7. team/player profile 与 model refs 存在且不是 missing/unavailable placeholder；
8. 没有 source、identity、authority、schema、freshness、invalidation 或 lineage conflict；
9. 由单一正式 verifier 从持久化事实重算为 qualifying，未知 reason 默认 non-qualifying。

在上述完整性条件同时成立时，下列当前策略 reason 可作为 policy rejection allowlist：

- `odds_outside_range`；
- `market_not_stable_two_snapshots`，但只允许“两条完整快照的去水概率移动超过 tolerance”，不允许缺 previous context；
- `vision_situation_collapsed`；
- `underdog_deficit_not_material`；
- `comeback_entry_outside_time_window`；
- `rosh_direction_opposes_underdog`；
- `draft_landmark_support_or_calibration_failed`；
- `insufficient_data_quality`，但所有 profile/model inputs 必须存在；
- `no_independent_positive_contribution`；
- `edge_below_threshold`；
- `conservative_probability_not_above_market`。

任何新 reason 在更新策略版本、本文档、verifier 和测试前均 fail closed 为 non-qualifying。

## 明确不具备 M1 资格的结果

- `waiting_for_*` 或没有持久化 canonical evaluation；
- response/audit rejection；
- pre-strategy `no_signal`，包括 `strict_live_ineligible:*`、`draft_landmark_unavailable:*` 和 `draft_authority_unavailable`；
- missing、unavailable、stale、invalid、untrusted、paused、identity、authority、source、schema、conflict 或 unsupported-layout 类 reason；
- `transport_identity_missing_or_reused`、`map_already_attempted`；
- `rosh_lineup_score_unavailable`、`rosh_lineup_draft_mismatch`、`rosh_minute_score_unavailable`；
- order rejection。Order rejection 属于订单生命周期，不能替代策略链完整求值证据。

这些结果仍应进入诊断、告警和 fail-closed 审计，但 `m1_qualifying_rejection=false`。

## 后果

- M1 不能由“系统正确拒绝了缺失输入”单独完成；canary 必须真正到达完整策略求值路径。
- 需要实现单一、可重放的 M1 verifier，并在 service report/Web/证据包中输出 qualification 结果与原因。
- Verifier 必须按 ADR-0010 使用 persisted authority、strategy version、canonical evaluator artifact 和完全相同的 policy hash 重放，而不是信任运行时状态字符串或人工 gate 清单。
- 本决策可能增加 M1 日历时间，但防止把长期缺 model、Rosh 或 Vision 能力误报为生产就绪。
- 本 ADR 不改变 ADR-0003 的 settlement authority 组合或 M2 filled-only contract。

## 验证要求

- 正向 fixture：完整 inputs/authority、canonical evaluator policy rejection、verifier 返回 qualifying。
- 负向 fixture：每一类 pre-strategy/missing/identity/authority/source rejection 均返回 non-qualifying。
- Tamper fixture：任一持久化 lineage、reason 或 strategy-version identity 不一致时 fail closed。
- Projection fixture：non-qualifying rejection 可见于诊断，但不能推进 M1 状态。
