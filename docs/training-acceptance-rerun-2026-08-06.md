# Dota 2 训练验收复验（2026-08-06）

## 结论

当前默认模型应保持 **Team Rating-only**。不得执行 3933 图正式 Prematch
训练或冻结新 Deployment。

- Team Rating 在 300/800/1500 三档均通过，1500 图相对固定 50% 有稳定增量。
- Draft 在 300 图 paired support 上显著劣于 Team-only，应停止进入默认模型。
- R.O.S.H. 与 Cluster 的历史可用 support 都是 0，不能进入下一阶段。
- 所有校准器均 fail-closed，没有发布 calibrated probability。
- Prematch 300 图耗时已触发计算停止规则；800/1500 不继续盲跑。

## 数据与角色就绪度

实际 PostgreSQL authority（schema `20260806_0028`）：

| 指标 | 地图数 |
|---|---:|
| 正式地图 | 3933 |
| 当前版本 expected-role 十槽位就绪 | 3932 |
| 当前版本 observed-role 十槽位就绪 | 3932 |
| expected + observed 具体位置完整 | 3810 |
| Draft raw ready | 3910 |
| Draft role-ready / 可加载目标 | 3910 |

Coverage 报告现在同时输出总体、逐赛事角色就绪计数，并把缺失槽位和缺失具体
位置写入 `issues`。Draft loader 只加载当前 assignment version 的十个 expected-role
槽位完整地图，但保留 raw Draft ready 总数。

## 已修复问题

### 稀疏特征与校准

- 连续特征少于 20 个非缺失观测时不学习该数值列。
- scale 只由真实观测计算，下限为 `1e-6`。
- 训练与预测的标准化值限制为 `[-8, 8]`。
- `8781808385` 对应的 2/81 support 回归案例不再产生 `108.9 sigma` 和
  `+47.66` log-odds contribution。
- Platt causal fit 最低 support 从 20 提高到 100；非正 slope 直接失败且不应用。
- 模型版本升级为 `prematch-offset-logistic-l2-v2`，校准版本升级为
  `prematch-platt-v2`。

### Team Rating 计算

144 组候选参数仍保留每个 cutoff 自己的 Radiant prior 和完整前缀重放语义，
但候选状态改为分批向量化，累计损失按结果可用时间增量维护。最终选中参数仍走
原有 PR-1 Artifact 重放。逐 cutoff 对照测试的概率误差不超过 `1e-12`，选中参数
一致。

### Prematch Artifact 存储

新 run 不再重复内嵌完整 training corpus。Migration `20260806_0028` 新增：

- `prematch_training_corpus_rows`：内容寻址、去重的训练行；
- `prematch_training_corpus_prefixes`：parent + row 的不可变前缀链；
- model run 只保存紧凑 manifest 和 prefix head，加载时递归还原并执行完整 hash/replay。

现有 2500 个旧 model run（`319,660,032` bytes）未改写。所有验收均为 dry-run，
两张新表复验后仍为 0 行。单元测试中，同一 24 行 corpus 的两个不同模型只产生
24 个 row 和 24 个 prefix 节点。

## 计算基准

口径：同一机器、默认 144 参数网格、1000 次 series-cluster bootstrap、
PostgreSQL 只读加载、dry-run 持久化检查。

| 地图数 | Team Rating 新耗时 | 旧耗时 | 新峰值 RSS | Gate |
|---:|---:|---:|---:|---|
| 300 | 26.03s | 52.20s | 201.4 MiB | passed |
| 800 | 95.07s | 327.08s | 302.2 MiB | passed |
| 1500 | 265.59s | 1145.35s | 600.3 MiB | passed |

新总耗时增长指数：300→800 为 `1.321`，800→1500 为 `1.634`，总体为
`1.443`；原实现为 `2.033`。按总体指数外推 3933 图约 `1068s`（17.8 分钟），
但这只是外推，不构成全量运行授权。

Prematch 300 图完整 dry-run 成功，耗时 `599.3s`。成功进程的峰值 RSS 未被启动
器可靠保留，因此不填伪精确值。按 300 图的逐 cutoff 拟合成本二次外推，800 与
1500 约需 71 分钟和 4.2 小时，已触发计算停止规则，未继续运行。

## 分阶段结果

### Team Rating 1500 图

| 模型 | Support | Brier | Log loss | AUC |
|---|---:|---:|---:|---:|
| 固定 50% | 1500 | .25000 | .69315 | .50000 |
| Team Rating | 1498 | **.23189** | **.65537** | **.65023** |

- Team Rating - 50% Brier delta：`-0.01811`，90% CI
  `[-0.02468, -0.01165]`。
- Team Rating - 50% log-loss delta：`-0.03778`，90% CI
  `[-0.05209, -0.02309]`。

### Prematch 300 图

| 阶段 | Support | Coverage | Brier | Log loss | AUC | 结论 |
|---|---:|---:|---:|---:|---:|---|
| B0 固定 50% | 300 | 1.000 | .25000 | .69315 | .50000 | baseline |
| B1 Radiant prior | 300 | 1.000 | .25301 | .69954 | .47835 | 更差 |
| B2 Team-only | 276 | 1.000 | **.23443** | **.66268** | **.64501** | 保留 |
| B2 + Draft | 276 | .705 | .28134 | .81713 | .56509 | 拒绝 |
| B2 + R.O.S.H. | 276 | 0 available | .23443 | .66268 | .64501 | unsupported |
| B2 + Draft + R.O.S.H. | 276 | 0 R.O.S.H. | .28134 | .81713 | .56509 | 拒绝 |
| + Cluster | 0 | 0 | n/a | n/a | n/a | unsupported |

Draft - Team-only paired Brier delta 为 `+0.04691`，90% CI
`[+0.02807,+0.06754]`；log-loss delta 为 `+0.15445`，90% CI
`[+0.08389,+0.23571]`。Draft 在同一 support 上显著变差。

校准 causal fit 为 102、evaluation 为 174；四个有预测的模型均因
`reverse_monotonic_calibration` 失败，Cluster 为 `no_legal_series_split`。
所有 calibrated probability 保持 null。

## 停止与后续

- 不运行 3933 图正式 Prematch 训练。
- 不冻结新 Deployment，不启动订单或实盘。
- 默认模型保持 Team Rating-only。
- 下一项工程工作应是减少 Prematch 每个 cutoff 的重复优化器拟合；在该问题解决前，
  800/1500 Prematch 长跑没有验收价值。
- Draft 需要新的游戏语义/特征方案和独立证据，不能仅靠增加样本重新启用。

## 技术验证

- Ruff：受影响文件 `All checks passed`。
- 模型、存储、Team Rating、CLI 单元回归：`194 passed in 125.00s`。
- CLI dry-run 边界补充回归：`8 passed in 2.14s`。
- PostgreSQL schema/runtime/migration 集成：`40 passed in 115.86s`。
- `git diff --check`：通过。
- 生产样数据库 revision：`20260806_0028 (head)`。
- 所有基准均为 dry-run；新 corpus 表、model run、prediction 和 validation
  行数均未因本轮基准增加。
