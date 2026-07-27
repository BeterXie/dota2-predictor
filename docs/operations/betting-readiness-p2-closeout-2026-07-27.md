# P2 综合结案记录 — 2026-07-27

## 决策

**P2 技术验证：PASS**
**P2 退出模式：M1-only**
**M1 path readiness：READY**
**M2 readiness：BLOCKED**
**P3 preparation：GO**
**P3 exit：NOT ACHIEVED**
**P4 live canary：NO-GO**
**Production deployment：NO-GO**

本记录关闭 P2 的技术验证工作，但不把 P2.5 的 non-confirmed witness 描述成 M2 ready。阻塞原因保持为：

`no_supported_unique_confirmed_dual_source_candidate`

## 冻结代码身份

- Current ref：`release/p1-betting-readiness-final-20260726`
- Current commit：`2237d5f120ded13eb4e393a0c6a4251b096085df`
- Current tree：`84ecdfa62c5e465860495c231623d7ccef939619`
- Recovery commit：`a5673f0a00a86702fc25f9139686b3c42139a99e`

所有 P2 result 均记录了相同的 Current commit/tree。

## 分项结果

| Gate | 结果 | 关键证据 | Production DB attempt |
|---|---|---|---:|
| Deterministic | PASS | 570 passed | 0 |
| P2.1 HLS marker | PASS | open/read failure；marker matches=0；signed URL 未记录 | 0 |
| P2.2 Browser isolation | PASS | 13 passed；4 类隔离场景 | 0 |
| P2.3 TTL/backoff/latency | PASS | 11 passed；TTL、retry、probe、continuity、metadata | 0 |
| P2.4 DB/raw pair | PASS | 11 passed；database/raw/component identity 稳定 | 0 |
| P2.5 Dual-source liveness v2 | PASS for M1 path | 7 passed；4 个 completed rows 均因非 approved formal event 被 fail-closed | 0 |

测试节点在不同 gate 中可能重叠，本记录不把上述数量相加后宣称为唯一测试覆盖数。

## P2.5 语义

RayBet completed feed 中观察到 4 条 `EPL大师赛` 记录。严格赛事注册表对这些记录返回 approved formal event count=0，因此没有形成可唯一确认的 RayBet/OpenDota witness。系统没有放宽赛事、队伍、series、map 或 winner 身份要求，也没有创建 order、settlement、notification 或 research sample。

因此：

- M1 路径可以进入 P3 准备；
- M2 仍需等待一场受支持正式比赛的 confirmed dual-source reconciliation；
- P2.5 可机会性重跑，但不得自动授权 P4。

## 治理与授权边界

P1 的单人治理例外继续被明确披露；本次没有声称出现了独立人工复核。用户/项目 authority 在当前会话中授权执行下一步，因此本记录授权 P2 closeout 和 P3 preparation。它不授权：

- P4 live canary；
- 启动 production writer；
- 连接或修改 production database；
- 真实下注；
- browser companion 进入 production projection；
- 降低 strict identity、source、Vision、authority 或 evaluator/policy gate。

## 下一 Gate

创建 P3 候选登记，优先 `STANDARD_DOTA_HUD`。只有候选同时具备 exact mapping、frozen draft deployment、Rosh/team/player/model refs、canonical evaluator/policy identity、exact HLS refresh、layout evidence 和运维批准后，才能单独评审 P3 exit。即使 P3 exit，P4 仍需独立明确 GO。
