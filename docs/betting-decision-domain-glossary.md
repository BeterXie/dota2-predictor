# Dota 2 Paper 投注决策领域术语表

| 项目 | 内容 |
|---|---|
| 状态 | Living document |
| 起始日期 | 2026-07-24 |
| 适用范围 | direct-only live shadow/paper 决策、订单和结算链 |

本文只固定已经由实现或已接受 ADR 支持的语义。自然语言摘要与版本化可执行策略合同冲突时，以后者为准并 fail closed。

## 决策与订单

| 术语 | 定义 |
|---|---|
| Strategy decision | 一次策略求值产生的持久化决策事实；通过 `eligible` 和 `reason` 表达是否允许创建 paper order。它不等同于 order。 |
| Ineligible decision / strategy rejection | `eligible=false` 的策略决策；它可能是 pre-strategy `no_signal`，也可能是 canonical evaluator 的完整 scored rejection，二者不能混用。 |
| Qualifying strategy rejection | 满足 ADR-0002：全部必需输入/authority 完整、canonical evaluator 已运行、reason 属于 policy allowlist，且正式 verifier 从持久化 lineage 重算通过的 scored rejection。可以完成 M1。 |
| Non-qualifying rejection | 等待、pre-strategy `no_signal`，或 missing/stale/invalid/identity/authority/source 等失败。只能证明局部 fail closed，不能完成 M1。 |
| Paper order / shadow order | 同一个模拟订单领域对象，不会提交到真实投注 endpoint。 |
| Shadow map attempt | `(raybet_match_id, map_number)` 级的一次性订单尝试；每 map 最多一个，与 order 原子创建并同步进入相同终态。 |
| Pending order | 已由 eligible decision 创建、正在等待 first processed direct successor 解析的 paper order。 |
| Filled order | first processed direct successor 满足成交 contract 后得到的 order 终态；只有它具备正式结算资格。 |
| Rejected order / order rejection | first-successor、超时、市场或权威检查使 pending order 终止的结果；它是终态，不能 settlement，也不完成 M2。 |
| Order resolution | pending order 被互斥地解析为 `filled` 或 `rejected`。 |
| First processed direct successor | Signal event time 之后按 `(observed_at, observation_key)` 升序选出的第一条 on-time、processed、direct transport observation。不可跳过；expiry 含端点；没有 processed successor 时 order 保持 pending。 |
| Executable strategy contract | 由 `strategy_version`、canonical evaluator artifact/hash、canonical policy 与 `policy_hash` 共同标识的规范策略 predicate；人工 gate 清单只是摘要。 |
| Policy hash | 对 canonical serialized policy 的 content hash；predicate 变化必须产生新 hash 和新 strategy version。 |
| Strategy contract identity | `(strategy_version, evaluator_hash, policy_hash, serialization_version)`；同一 strategy version 对应多个组合即 contract drift。 |
| Legal economy bucket | 当前策略只接受 underdog deficit `1k..9k` canonical buckets；`10k` 表示 `10000..10999`，超过最大 10,000，整体 invalid。 |

## 结算与里程碑

| 术语 | 定义 |
|---|---|
| Settlement | 仅与 filled order 关联的独立权威结算事实；不是 rejected order 的后续状态。正式 authority 由 RayBet direct final 与 OpenDota exact reconciliation 共同构成。 |
| Settled order | 已有可验证 settlement 记录的 filled order；`settled` 是派生事实，不是 `shadow_orders.status` 值。 |
| M1 | Direct-only 决策链验收里程碑；必须由 eligible decision 或 ADR-0002 qualifying strategy rejection 支持。 |
| M2 | 首次合法 filled-and-settled paper order 里程碑；要求 order 执行 `eligible decision -> pending -> filled` 后关联 authoritative settlement record，并通过 filled/settled outbox payload/transaction/formal lineage、report 和全部 lineage 验证。 |
| M2-F | 非正式 filled checkpoint：自然 eligible order 已 filled，filled outbox payload/transaction/formal lineage 和 report projection 可验证，但尚未要求双源 outcome reconciliation。不是 M2，不进入 settled performance。 |
| M2 未完成 | 包括 order 被 rejected、filled 后 settlement 尚未完成、settlement authority 无效或 lineage/report 未通过。 |
| Dual-source reconciliation | RayBet direct final 与 OpenDota evidence 对同一 strict mapping、`dota_match_id` 和 winner 的赛后核对；confirmed 才能支持正式 settlement。 |
| Settlement pending | 至少一项双源 evidence 尚未可用；继续重试，不完成 M2。 |
| Settlement manual review | 身份、winner 或 authority 冲突形成的 sticky 状态；不得作为正式 settled sample 或 M2 evidence。 |

## M3 前向评估

| 术语 | 定义 |
|---|---|
| Outcome label | 与 settlement 分离的赛后权威赛果标签；使用 RayBet/OpenDota confirmed authority 绑定 eligible decision。可以标注未成交 decision，但不产生 return、PnL 或 ROI。 |
| M3-C | Eligible-decision calibration readiness track。包含取得 confirmed outcome label 的全部有效 eligible forward decisions，不以 fill 为条件；达到样本门槛只解锁 M4-C review。 |
| M3-E | Filled-settled economic/execution readiness track。只包含正式 filled 且 settled 的有效 forward orders；达到样本门槛只解锁 M4-E review。 |
| Outcome coverage | Eligible decisions 中 confirmed、pending、missing、manual-review outcome label 的数量和比例，并按 resolution/event/time 等维度审计 missingness。 |
| Outcome-mature eligible decision | 对 UTC cutoff `T`，其独立权威 `map_completion_at <= T-72h` 的有效 eligible decision；端点包含，maturity 不等于 outcome 已 confirmed。 |
| Outcome coverage gate | Outcome-mature eligible decisions 的 confirmed coverage overall `>=95%`、每 event `>=90%`，且 resolution coverage gap `<=5` 个百分点；maturity unknown 或必要 denominator 为零时 M3 not_ready。 |
| Resolution coverage gap | `abs(confirmed_filled/mature_filled - confirmed_nonfilled/mature_nonfilled)`；`non-filled=rejected\|pending\|no_order`，用于检测 missingness 是否与执行结果相关。 |
| Settled forward sample | M3-E 样本：prospectively recorded、filled、双源 reconciliation confirmed、正式 settlement 且 authority/identity 完整的 order。它不是 M3-C 的唯一样本。 |
| Calibration forward sample | M3-C 样本：prospectively recorded eligible decision + confirmed 双源 outcome label；order 可以 filled、rejected 或未成交。 |
| Cohort identity | Strategy、model、feature、calibration、draft deployment 和 global gate 等不可兼容维度的组合；不同 identity 默认不得 pooling。 |
| Selection audit | 比较 filled/rejected/pending 等执行结果下 outcome coverage 与特征分布，用来揭示 filled-only 选择偏差。 |
| Strict event identity | Decision cutoff 时 strict mapping 绑定的 `event_id`；不同于 `raybet_match_id` series identity。 |
| M3 diversity gate | 每个 M3 track 独立要求至少 500 个 mature valid samples、100 series、3 strict events、每 event 50 samples，且任一 event 占比不超过 50%。 |
| Event concentration | 某 strict event 的有效 sample count / 对应 track 总 sample count；按样本条数而非 stake 加权，最大允许值为 50%。 |
| Leave-one-event-out gate | 在冻结 membership 上逐个删除一个 event 的全部样本且不重新筛选，M4-C 两项 paired market improvement 或 M4-E ROI 都须保持严格正向。 |
| M3 ready | 样本、series、event support/concentration、coverage 和 cohort identity 已达到正式评审前置条件；不表示策略通过。 |
| M4-C | Calibration promotion decision。按预注册 gate 对 M3-C cohort 作出 failed/passed 决策。 |
| M4-E | Economic promotion decision。按预注册 gate 对 M3-E cohort 作出 failed/passed 决策。 |
| Promotion specification | 在查看目标 cutoff 结果前冻结、content-addressed 的 metrics、interval、coverage、baseline、sensitivity 和通过逻辑。 |
| Promotion decision record | 绑定 spec/report/cohort hashes、逐 gate 结果、总体 failed/passed、审批人和时间的不可变正式记录。 |
| Core scoring gates | ADR-0006 定义的 M4-C 四项 hard gate：绝对 Brier/log loss，以及相对 decision-time market 的两项 paired 90% bootstrap improvement。 |
| Paired market improvement | 在相同 decision/outcome samples 上计算 `market metric - model metric`；正值表示 model 更优。 |
| Series-cluster bootstrap | 以 `raybet_match_id` series 为 cluster 重采样，保留 series 内所有 decisions；model/market 在同一 replicate 中成对计算。 |
| ECE gate | ADR-0007 的 M4-C 校准形状 gate：五个 equal-count bins、每箱至少 50 条，ECE `<=0.05` 且 series-cluster 90% upper bound `<=0.08`。 |
| Equal-count calibration bins | 按 `(model_probability, decision_key)` 排序后划分的五个近似等样本箱；不得用 outcome 作为 tie-break。 |
| Stake-weighted ROI | M3-E 的 `(sum(stake * return_units) - sum(stake)) / sum(stake)`；point estimate 和 series-cluster bootstrap 90% lower bound 都必须严格大于零。 |
| Raw signed slippage | Decimal odds 上的 `(signal_price - fill_price) / signal_price`；正值不利、负值有利，价格分别来自 signal direct transport 和 first processed direct successor。 |
| Adverse slippage | `max(0, raw signed slippage)`；M3-E orders 等权计算，favorable fill 以零计，mean/P95/maximum 上限为 1%/2%/3%。 |
| Favorable price improvement | `max(0, -raw signed slippage)`；只用于诊断，不能用来抵消 adverse slippage。 |
| Slippage rejection rate | 全部 eligible orders 中因 slippage 被 rejected 的比例；属于 selection audit，不与 filled-only M3-E adverse-slippage gate 混用。 |
| Stake unit | Paper stake 的规范计量单位；drawdown 使用实际 stake 产生的累计 PnL units，不按订单数量计。 |
| Peak-to-trough drawdown | 按 `(settled_at, order_key)` 排序的累计 PnL 从历史峰值到后续谷值的最大下降；M4-E 上限为 20 stake units。 |
| M4-E economic gate | ADR-0009 的 ROI、adverse slippage 和 drawdown hard gates；stake increase 还要求 M4-C 与 M4-E 同时 passed。 |
| M4 failed | 已完成正式评审但至少一项 performance gate 失败；是合法 evaluation result，本身不撤销 M3 ready，也不授权调参或部署。 |
| M4 passed | 全部必需 gate 和 approval record 均通过；只有 governance status active/non-revoked 时才允许提出新策略版本及独立验证计划，不自动部署。 |

## 证据、撤销与治理

| 术语 | 定义 |
|---|---|
| Workspace/evidence manifest | 覆盖 tracked diff、untracked source/test/docs、依赖、命令、database identity/cutoff、raw/Vision/draft/provider evidence 的 content-addressed P0 根清单。普通 git diff hash 不等价。 |
| Database file identity | Database path 和稳定文件标识；只证明同一文件，不定义本次 evidence 内容。 |
| Evidence cutoff | 对 database authority relations 冻结的 row/time/key high-water marks；后续动态写入不改变原 manifest slice。 |
| Rollback proof | 在隔离、schema-compatible fixture 上把结果定为 `write_compatible\|read_only_only\|failed\|unverified`；只有第一种允许旧 writer 接管。 |
| Production-critical test | ADR-0012 列出的 12 个零失败测试文件；不接受 baseline exception。 |
| Test exception | 仅适用于其他全量测试失败的限时审计记录，必须在 clean `8f6d4cd` 独立复现、证明与生产链无关，并绑定 owner、ticket、deadline。 |
| Milestone revocation record | 后发 mapping/Vision/draft/source/settlement conflict 对既有 M1/M2/M3/M4 结论的 append-only 撤销事实；原 evidence/record 保留，受影响样本隔离。 |
| Governance status | 与原 evaluation result 正交的 `active\|revoked` 状态；revocation 不把旧 passed/failed 结果原地改写。 |
| Execution owner | 组织 P0-P6、控制范围并提交 acceptance package 的具名负责人。 |
| Independent verifier | 独立核验 evidence、hash、gate 与 revocation 的具名角色，不能由执行者自证替代。 |
| Production DB operator | 对 production writer 独占、database/raw identity、停写和回滚边界负责的具名操作员。 |
| M4 decision owner | 对 promotion specification 和最终 M4 decision record 负责的具名批准者；analysis author 不能是唯一 approver。 |
| Outbox domain health | Filled/settled outbox payload、持久化事务边界和 formal lineage 的领域状态；失败会阻塞对应 M1/M2。 |
| Delivery health | SMTP/provider 实际投递的运维状态；与 outbox domain transaction 分离，失败不撤销已验证的 M1/M2。 |

## 必须区分的“拒绝”

| 术语 | 所属层 | 是否产生 order | 是否可 settlement |
|---|---|---:|---:|
| Response rejection | provider response/audit 层 | 否 | 否 |
| Strategy rejection | 策略求值层 | 否 | 否 |
| Order rejection | paper order successor/执行模拟层 | 已产生，但终止为 rejected | 否 |

## 决策完整性

本文范围内原评审清单的领域定义均已接受。后续若改变 predicate、gate、authority 或治理边界，必须创建新 ADR/strategy version，而不是把旧术语静默改义。
