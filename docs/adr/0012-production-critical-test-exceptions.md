# ADR-0012：Production-critical Test 与例外政策

| 项目 | 内容 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-07-24 |
| 适用范围 | P1、M1、M2 和发布验收 |
| 关联计划 | `docs/betting-decision-production-readiness-development-plan-2026-07-24.md` |

## 背景

“全量测试无本轮新增回归”不足以保护 production chain：一个 settlement、outbox 或 report 的失败即使也出现在旧基线，也不能被当作可接受的既有噪声。与此同时，与本链路完全无关的旧失败不应无限期阻塞文档范围内的工作，但例外必须可审计和有期限。

## 决策

以下 12 个 production-critical 文件必须零失败，不接受 baseline、owner 或 ticket 豁免：

1. `tests/test_raybet_direct_response_audit.py`
2. `tests/test_raybet_collector_resilience.py`
3. `tests/test_direct_source_isolation.py`
4. `tests/test_raybet_stream_scripts.py`
5. `tests/test_service_health.py`
6. `tests/test_monitoring_dashboard.py`
7. `tests/test_successor_fill.py`
8. `tests/test_shadow_monitor_safety.py`
9. `tests/test_settlement_authority.py`
10. `tests/test_postmatch_settlement.py`
11. `tests/test_notification_outbox.py`
12. `tests/test_live_report.py`

全量 suite 的其他单项失败只有在以下条件全部满足时才可形成有期限例外：

- 同一 test node 在干净、独立的 `8f6d4cd` checkout 和相同受控依赖下独立复现；
- 有证据证明与 direct collection、strict mapping、Vision、canonical evaluator、order/fill、settlement、notification、production report、database/raw identity、安全或里程碑治理链无关；
- exception record 绑定 exact test node、两边输出/evidence manifest、root-cause 判断、具名 owner、ticket 和截止日期；
- 截止日期未过，且 M1/M2 acceptance record 明确列出该例外。

不在 `8f6d4cd` 中的新增/untracked acceptance test 不能用“基线不存在”获得例外；它若属于 production chain 就必须通过。口头豁免、仅记录失败数量、无法独立复现或已过期例外均无效。

## 后果

- M1 也必须等四个 settlement/outbox/report critical 文件归零，避免先接受一条无法可信收尾的生产链。
- 非关键旧失败可以透明、限时地管理，但不能掩盖本计划链路的真实缺陷。
- P0 manifest 必须区分共同 baseline tests 和仅新工作区存在的 acceptance tests。

## 验证要求

- 12 文件任一 failure 时 M1/M2/发布均 fail closed。
- 例外 schema 缺 owner、ticket、deadline、clean-baseline evidence 或 scope proof 时拒绝。
- Deadline 到期后自动从 excepted 变为 blocking。

