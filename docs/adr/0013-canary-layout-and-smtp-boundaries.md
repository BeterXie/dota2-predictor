# ADR-0013：Canary Layout 与 SMTP 边界

| 项目 | 内容 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-07-24 |
| 适用范围 | P3/P4 canary candidate、Vision layout 投资和 notification delivery |
| 关联计划 | `docs/betting-decision-production-readiness-development-plan-2026-07-24.md` |

## 背景

过早扩展多个 overlay 会扩大 replay/OCR 攻击面并分散验收；等待单一 layout 无期限也会让 M1/M2 没有自然候选。SMTP 则是出站投递能力，不应与订单/结算事务中的正式 outbox evidence 混成同一里程碑 gate。

## 决策

### Layout expansion policy

Canary 始终优先使用现有 `STANDARD_DOTA_HUD`。只有满足以下任一客观触发条件，才允许批准一个新 overlay：

- 因 overlay 不受支持而错过两次已登记、原本满足 Tier-1 event/mapping 条件的 scheduled live window；或
- 从候选监测开始连续 14 个日历日没有可用的 `STANDARD_DOTA_HUD` candidate。

候选监测的不可回填 UTC 起点，是 P2 已退出、mapping/draft/Rosh/model/provider 上游依赖 ready 且 `STANDARD_DOTA_HUD` capability healthy 的首次记录时间。`Layout-only missed window` 必须预先登记，且 Tier-1/event/mapping 等非 layout 条件均满足，unsupported overlay 是唯一 blocker。

Missed window、唯一 blocker 和日期必须进入候选登记表。Overlay frequency 按 distinct registered live windows/maps 计数，不按帧数计。触发后只选择登记窗口中出现频率最高、且已有真实帧证据的一个 overlay；同一时间只能扩展一个。在该 overlay 完成正向/负向 evidence、两帧确认、reset 和 unknown fail-closed 验收前，不得启动第二个 overlay 扩展。

### SMTP boundary

实际 SMTP email 是否成功投递不阻塞 M1 或 M2。里程碑仍必须验证：

- filled 与 settled 两类正式 outbox payload 各自完整；
- outbox 与对应 fill/settlement 的 transaction boundary 正确；
- decision/order/evidence/settlement lineage 可重放；
- rejected order 不生成 filled 或 settled outbox。

SMTP 连接、认证、provider 或投递失败只影响 `delivery_health`、delivery attempt、retry/dead-letter 和运维告警；它不能删除 outbox、回滚已经成立的 fill/settlement，或把 M1/M2 改写为失败。Outbox domain health、持久化、事务或 lineage 失败仍是里程碑 blocker。

## 后果

- Layout 扩展有明确等待上限，同时保持一次一个的证据驱动范围。
- M1/M2 可在没有 SMTP credential 的环境完成，但不能跳过 notification contract。
- Monitoring/UI 必须把 domain/outbox health 与 delivery health 分开显示。

## 验证要求

- 未达到两次 missed window/14 天条件时，新 overlay approval 被拒绝。
- 达到条件后仍只能存在一个 active layout expansion。
- SMTP forced failure 不改变 fill/settlement/M1/M2，且 delivery health 明确 degraded。
- Outbox transaction 或 lineage failure 阻塞相应里程碑。
