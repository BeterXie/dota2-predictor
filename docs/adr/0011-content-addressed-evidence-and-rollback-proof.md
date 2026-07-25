# ADR-0011：Content-addressed Evidence Manifest 与 Rollback Proof

| 项目 | 内容 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-07-24 |
| 适用范围 | P0 baseline、M1/M2 canary evidence、发布和回滚 |
| 关联计划 | `docs/betting-decision-production-readiness-development-plan-2026-07-24.md` |

## 背景

当前核心实现和测试包含未跟踪文件，普通 `git diff` 不覆盖它们；仅记录 diff hash 无法重建真实工作区。另一方面，“没有 migration”只说明 schema 没有显式迁移，并不证明新代码写出的 row、JSON 或 outbox shape 能被旧代码安全读取和继续写入。

## 决策

### Workspace/evidence manifest

P0 必须生成 canonical、content-addressed manifest，并记录 manifest 自身 hash。它至少覆盖：

- `HEAD`、完整 status，以及 staged/unstaged tracked diff 的内容 hash；
- 所有 untracked source、test 和正式 docs 文件的相对路径、大小与 SHA-256；
- Python、pytest、OpenCV、FFmpeg 和全部运行/测试关键依赖版本；
- 执行过的精确命令、非秘密环境合同、开始/结束时间和退出状态；
- production database 的绝对路径、稳定 file identity，以及各 authority/table 的 row/time/key cutoff；file identity 只证明同一文件，frozen cutoffs 才定义本次 evidence slice；
- 配对 raw archive root 和纳入证据的 raw object hashes；
- Vision JSONL、frame/crop evidence 的路径、cutoff 与 content hashes；
- frozen draft deployment key/artifact hash；
- RayBet、OpenDota、HLS/FFmpeg、STRATZ 等 provider evidence refs、response/audit hashes 与 first-usable times。

Manifest 使用版本化 canonical serialization，并只保存 secret-safe refs/hashes，不复制 token、signed URL 或敏感原始内容。普通 `git diff` hash、提交 SHA 或数据库路径单独存在时都不构成可复现 workspace identity。动态数据库在 cutoff 后继续写入不会改变已冻结 evidence 的含义；重放必须限定原 keys/cutoffs，不能查询“当前全部 rows”。

### Rollback proof

发布前必须在隔离、schema-compatible fixture 上演练基线 `8f6d4cd`。Fixture 应先由新代码写入本计划会产生的代表性 decision/order/attempt/outbox/settlement/report shapes，再由旧代码执行其实际 rollback read/write 路径，并验证：

- 旧代码不会误读新 authority 或把 invalid row 当 production；
- 旧 writer 不会覆盖、降级、重复或写坏新 data shapes；
- restart、pending order 和 outbox 路径保持可预测；
- 失败时 rollback 改为停止 writer/只读保全，而不是冒险启动旧 writer。

回滚演练结果固定为：`write_compatible`（旧 writer 可按演练路径安全接管）、`read_only_only`（只能停写后只读）、`failed` 或 `unverified`。只有 `write_compatible` 才允许旧 writer 在实际生产回滚中恢复写入；其余状态只能停写/只读或 forward fix。

回滚演练绝不得连接、复制回写或测试写入 production database。无 migration 不能作为跳过该演练的理由。

## 后果

- P0 比单一 patch hash 更重，但能覆盖当前未跟踪的 acceptance tests 和实现。
- 回滚是否可写由演练结果决定；不能默认 `git checkout 8f6d4cd` 就安全。
- Evidence manifest 成为后续 acceptance、revocation 和审计记录引用的根 identity。

## 验证要求

- 修改任一 tracked/untracked source、test、doc 或 evidence artifact 会改变 manifest hash。
- Manifest 验证器能发现缺文件、hash mismatch、database cutoff 漂移和 provider evidence 缺失。
- `8f6d4cd` 在隔离 fixture 上完成预定读写或明确证明只能只读回滚。
- 测试连接 production path 时立即失败。
