# ADR-0010：Versioned Executable Strategy Contract

| 项目 | 内容 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-07-24 |
| 适用范围 | Comeback v4 live evaluation、canary verification、report replay 和策略版本变更 |
| 关联计划 | `docs/betting-decision-production-readiness-development-plan-2026-07-24.md` |

## 背景

人工维护的 gate 列表容易与实际 predicate 漂移。当前实现还接受 `maximum_net_worth_deficit=10000`，但 Vision economy 使用整千区间 bucket；`10k` 实际表示 `10000..10999`，其上界超出策略最大值，不能作为合法 canonical evidence。

## 决策

版本化 canonical evaluator 与其 content-addressed policy artifact 是策略资格的唯一规范合同。每条 decision、order、canary evidence、report 和 promotion record 必须绑定：

- `strategy_version`；
- canonical evaluator version/content hash；
- canonical serialization version、完整 policy artifact 与 `policy_hash`；
- evaluator 输入 evidence/authority identity。

一个 `strategy_version` 只能映射到一个 `(evaluator_hash, policy_hash, serialization_version)` 组合；同名 version 出现多个组合即 contract drift，全部相关 evidence fail closed。

主计划、运维手册和 UI 中的自然语言 gate 列表只作为人类可读摘要；列表与 evaluator 不一致时，必须 fail closed 并修正文档或创建新策略版本，不能用摘要覆盖 evaluator。

Canary verifier、M1 rejection verifier 和 report 必须从持久化输入重放同一 evaluator 与完全相同的 policy hash。找不到 artifact、hash 不一致、运行不同 predicate 或不能得到相同 decision 时，证据无效。

任何会改变 eligible/rejection/fill 资格集合的 predicate 变化，包括阈值、边界包含性、freshness、稳定性、source/authority、evidence、排序、side direction 或 Rosh/data-quality 规则，都必须创建新的 `strategy_version` 和 policy hash；不得原地改写旧版本语义。

当前策略合法的 underdog economy buckets 只有 `1k..9k`。Canonical bucket `10k` 表示完整区间 `10000..10999`，超过 maximum net-worth deficit 10,000，因此整体 fail closed；不能因区间下端恰好为 10,000 而接受。

## 后果

- M1/M2 canary 不再靠人工逐项勾选近似 gate，而是验证同一可执行 contract 的重放结果。
- 计划中的数值列表仍可用于排障，但不能成为第二套策略定义。
- 需要新增稳定 canonical policy serialization、hash 持久化和 drift tests。

## 验证要求

- 同一 inputs/evaluator/policy hash 重放得到相同 decision 和 reason。
- 修改任一 predicate 或 policy 字段会改变 hash，并要求新 strategy version。
- Artifact 缺失、hash mismatch 或人工摘要与 evaluator 不一致时 fail closed。
- Economy buckets 0k、10k、11k 均 invalid；1k 和 9k 边界有效。
