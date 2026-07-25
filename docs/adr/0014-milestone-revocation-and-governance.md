# ADR-0014：Milestone Revocation 与 Governance

| 项目 | 内容 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-07-24 |
| 适用范围 | M1/M2 acceptance、M3 readiness、M4 promotion 和生产数据库操作 |
| 关联计划 | `docs/betting-decision-production-readiness-development-plan-2026-07-24.md` |

## 背景

权威证据可能在里程碑记录后才暴露 mapping、Vision、draft、source 或 settlement 冲突。若只保留最初的 passed 结论，历史报告会继续使用已失效样本；若直接修改或删除旧记录，又会破坏审计链。正式验收还需要明确谁执行、谁独立核验、谁有权操作生产库和谁批准 promotion。

## 决策

### Append-only revocation

验收前发现的 strict mapping、Vision、draft、source 或 settlement conflict 继续使用 manual-review/block 语义；只有在里程碑记录后确认的同类 conflict 才创建 append-only `milestone_revocation_record`，而不是更新、覆盖或删除原 acceptance/readiness/promotion record。Revocation 至少绑定：

- 原 record、workspace/evidence manifest、cohort/report/spec hashes；
- conflict type、权威 evidence refs、发现时间和 effective time；
- 受影响的 decision/order/settlement/sample keys；
- 被撤销的 M1、M2、M3-C/M3-E、M4-C/M4-E 结论；
- 发起人、独立 verifier、处置状态和时间。

受影响样本立即进入 isolated/revoked projection，不再进入当前 scored cohort。任何依赖这些样本或证据的 M1/M2/M3/M4 结论都视为 revoked；即使移除样本后数值仍通过，也必须用新 cutoff、manifest/report hash 和新正式记录重新验收，不能恢复或改写旧 passed record。

Evaluation result 与 governance status 正交：原 `passed|failed` 结果保持不变，另将 `governance_status=active|revoked`。Revocation 沿 evidence→decision/order/settlement→milestone/cohort→promotion 的依赖闭包传播：M1 revoked 时停止依赖该 chain 的新 paper decisions 直至重新验收；M2 revoked 时撤销该 order 的 M2 证明并隔离依赖样本；M3/M4 revoked 时阻止其继续授权 review、proposal 或 stake change。

旧 evidence、report、decision 和 revocation 永不删除或覆盖。Conflict 不得通过手工 winner/mapping 修补后沿用旧 identity。当前无-migration 边界下，record 写入与 database/raw pair 配套、content-addressed 的 append-only audit ledger，并由 report 投影；本 ADR 不暗中授权新增 SQLite migration。

### Named governance roles

执行开始前必须在 P0 manifest 中实名绑定四个角色及其账号/职责生效时间：

| 角色 | 最低职责 |
|---|---|
| Execution owner | 组织 P0-P6、控制变更范围并提交 acceptance package |
| Independent verifier | 不以执行者自证替代独立核验，验证 evidence、hash、gate 和 revocation |
| Production DB operator | 独占管理 production writer、database/raw identity、停写与回滚边界 |
| M4 decision owner | 对 promotion specification 与最终 passed/failed record 负责 |

Independent verifier 不得与被核验范围的 execution owner/production DB operator 使用同一人员或账号。M4 analysis author 不得成为唯一 approver；至少还需具名 M4 decision owner 或 independent verifier 独立签署，且 owner 不能把 deterministic failed 覆盖成 passed。每份 M1/M2 acceptance、M3 readiness、M4 promotion 和 revocation record 都必须绑定相关具名人员、角色、决定和 UTC 时间；缺角色、签署或时间时状态保持 `review_required`/未完成。

## 后果

- 历史结论保留但可以被明确撤销，不会继续污染当前 projection。
- 一次 revocation 可能级联撤销后续 M3/M4 结论；这是 authority 修正的必要代价。
- 计划可以在未提前写死具体姓名的情况下获批，但 P0 未完成角色实名绑定前不得执行生产 canary 或签署里程碑。

## 验证要求

- 对已 accepted M2 注入后发 settlement conflict：原记录保留、新 revocation 追加、样本隔离、依赖 M3/M4 失效。
- 删除/更新旧 evidence 或 acceptance record 被 append-only ledger/verifier 拒绝。
- M4 analysis author 是唯一 approver、角色未实名或 timestamp 缺失时不得 passed。
