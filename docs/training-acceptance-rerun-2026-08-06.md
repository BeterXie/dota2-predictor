# Dota 2 训练验收复验（2026-08-06）

## 结论

当前默认模型应保持 **Team Rating-only**。3933 图 Team Rating 全量 dry-run
已完成；不得执行 3933 图正式 Prematch 训练或冻结新 Deployment。

- Team Rating 在 300/800/1500 三档均通过，1500 图相对固定 50% 有稳定增量。
- Team Rating 3933 图全量仍通过 Gate，并已输出 patch、赛事、月份、队伍经验、
  概率分布和校准分箱诊断。
- Draft 在 300 图 paired support 上显著劣于 Team-only，应停止进入默认模型。
- R.O.S.H. 与 Cluster 的历史可用 support 都是 0，不能进入下一阶段。
- 所有校准器均 fail-closed，没有发布 calibrated probability。
- Prematch 300 图耗时已触发计算停止规则；800/1500 不继续盲跑。

## 数据与角色就绪度

实际 PostgreSQL authority（schema `20260806_0029`）：

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

新 run 不再重复内嵌完整 training corpus。Migration `20260806_0028` 新增共享
corpus 表；`20260806_0029` 删除了与 Python 浮点文本不兼容的 PostgreSQL JSON
文本等价 CHECK，保留 JSON object 检查和应用层 hash/replay 校验。

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
实际全量运行结果见下方定向复验。

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

- 不运行 3933 图正式 Prematch 训练；Team Rating 全量验证不等同于 Prematch
  全量授权。
- 不冻结新 Deployment，不启动订单或实盘。
- 默认模型保持 Team Rating-only。
- D0-D8 消融没有找到稳定有价值的 Draft 特征组，因此本轮不优化 Prematch 重复
  拟合器；先修正特征语义和证据投影。
- Draft 需要新的游戏语义/特征方案和独立证据，不能仅靠增加样本重新启用。

## 技术验证

- Ruff：受影响文件 `All checks passed`。
- 模型、存储、Team Rating、CLI 单元回归：`194 passed in 125.00s`。
- CLI dry-run 边界补充回归：`8 passed in 2.14s`。
- PostgreSQL schema/runtime/migration 集成：`40 passed in 115.86s`。
- `git diff --check`：通过。
- 生产样数据库 revision：`20260806_0029 (head)`。
- 上述 300/800/1500 基准均为 dry-run；源库新 corpus 表、model run、prediction
  和 validation 行数均未因这些基准增加。
- 定向复验新增 D0-D8 消融、R.O.S.H. 漏斗和隔离 Artifact 实写验证；实写只发生
  在临时克隆库，验证后已删除。

## 定向复验结果（2026-08-06）

### Team Rating 3933 图全量

| 指标 | 结果 |
|---|---:|
| Formal maps / evaluated OOS | 3933 / 3931 |
| Brier / log loss / AUC | 0.231274 / 0.654378 / 0.654827 |
| Brier delta vs 50% | -0.018726 |
| Brier 90% CI | [-0.022570, -0.015144] |
| Log-loss delta vs 50% | -0.038769 |
| Log-loss 90% CI | [-0.047079, -0.031195] |
| Gate | passed |
| Wall time / peak RSS | 1898s / 2854 MiB |

所有 5 个 patch 的 Brier delta 均为负：patch 56/57/58/59/60 分别为
`-0.014816/-0.018294/-0.018805/-0.014812/-0.022554`。39 个赛事切片中
32 个相对 50% 更好、7 个更差；最差为 `blast-slam-iii-2025`（support 43，
Brier delta `+0.035431`），最好为 `ewc-dota2-2026`（support 157，delta
`-0.063293`）。20 个月份窗口中 17 个更好、3 个更差。新队伍参与（任一方
cutoff 前少于 5 张正式图）support 401、Brier `0.235274`；成熟队伍 support
3530、Brier `0.230819`。完整逐赛事、逐月份、概率分布和 5-bin 校准表由
`run_team_rating_backtest.py --json-output/--markdown-output` 生成。

### Draft D0-D8 消融（固定相同 300-map cohort）

| Variant | Features | Support | Brier | Log loss | AUC | Paired Brier delta vs D0 |
|---|---:|---:|---:|---:|---:|---:|
| D0 Team-only | 0 | 276 | 0.234433 | 0.662685 | 0.645014 | baseline |
| D1 semantic values | 10 | 276 | 0.261721 | 0.738400 | 0.593079 | +0.027288 |
| D2 hero + role | 2 | 276 | 0.237057 | 0.668533 | 0.635352 | +0.002624 |
| D3 synergy + counter | 2 | 276 | 0.235674 | 0.666213 | 0.648847 | +0.001241 |
| D4 scaling | 1 | 276 | 0.238957 | 0.673181 | 0.628157 | +0.004524 |
| D5 five proxy values | 5 | 276 | 0.250423 | 0.704385 | 0.607415 | +0.015990 |
| D6 values + missing | 20 | 276 | 0.257689 | 0.732592 | 0.607887 | +0.023256 |
| D7 values + support/coverage | 30 | 276 | 0.283427 | 0.825816 | 0.559051 | +0.048994 |
| D8 current full 40 columns | 40 | 276 | 0.281338 | 0.817131 | 0.565090 | +0.046905 |

8 个 paired comparison 全部 `rejected`；D1、D4-D8 的 90% CI 完整位于变差一侧，
D2/D3 的 CI 跨 0，没有稳定增量。决策为 `optimize_prematch=false`，不继续跑
Prematch 800/1500，也不做只为加速而加速的拟合器优化。

### R.O.S.H. Support Funnel

| Stage | Support |
|---|---:|
| historical rows | 694 |
| formal map linked | 694 |
| ten heroes complete | 694 |
| ten expected positions complete | 561 |
| player coverage complete (`player_coverage_count=10`) | 0 |
| legacy backtest eligible | 0 |
| official run authority linked | 0 |

当前正式 Draft role-ready targets 为 3910，精确 expected-position targets 为 2655；
对这 2655 个目标的 official replay 全部为 `run_unavailable`，最终 snapshot
available 为 0。当前正式 authority 有 20 个 runs、11 个 links，其中只有 3 个
不同的 formal-map link。数据库里的 694 条 legacy historical score 没有自动进入
`rosh_analysis_runs`/`rosh_run_match_links` 的可审计权威链，因此下一步应修复
权威桥接，而不是修改 R.O.S.H. 模型。

### Cluster 路线

已选择 **prospective shadow only**。7.41 静态资源不回灌 reconstructed 历史；
如果要获得历史 OOS support，必须另建逐 cutoff 的 walk-forward 资源。

### Artifact 真实写入（隔离数据库，100 maps）

| 项目 | 结果 |
|---|---:|
| 首次新增 corpus rows / prefixes | 392 / 392 |
| 首次新增 model runs / predictions / validations | 397 / 392 / 392 |
| 首次写入耗时 | 52.74s |
| 相同结果重复写入新增 | 0（全部 unchanged） |
| 最终 `team_plus_draft_rosh` Artifact reload | 0.266s |
| append-only DELETE | 被拒绝，Artifact 仍可加载 |
| 模拟中断事务回滚 | 通过 |
| 源库新增 corpus rows / prefixes | 0 / 0 |

隔离库验证完成后已删除。实写暴露的浮点 JSON CHECK 已由 migration
`20260806_0029` 修复，相关 PostgreSQL schema 回归为 `7 passed`。
