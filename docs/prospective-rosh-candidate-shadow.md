# Prospective R.O.S.H. Candidate Shadow

## 结论

本分支冻结了一个只有一个新增特征的 prospective research candidate：

```text
M0 = P_team

logit(M1)
= logit(P_team)
  - beta_rosh * standardized_pure_rosh_score
```

候选状态固定为：

```text
retrospective_initialized
prospective_unvalidated
shadow_only
not_deployment_eligible
```

截至冻结时，prospective paired support 为 **0**。本分支没有把 retrospective
结果当作 prospective 证据，没有写入正式概率，没有下单，也没有修改 Team
Rating、Draft、Cluster、Player-Hero、Calibration 或 Deployment。

## 冻结候选

候选由既有的 513-map paired retrospective cohort 一次性生成。Team Rating
概率作为固定 logit offset，只拟合一个 `beta_rosh`；没有做额外参数搜索，也没有
使用 event、patch、month 或结果切片选择权重。

| 字段 | 冻结值 |
|---|---:|
| paired training support | 513 |
| `beta_rosh` | -0.6692263354789106 |
| score mean | 0.5471734892787526 |
| score population scale | 12.485361284192061 |
| full-sample fit log loss | 0.6182679503676233 |
| training cutoff | 2026-08-04T12:29:35Z |
| frozen at | 2026-08-06T14:15:00Z |
| prospective start | 2026-08-06T14:30:00Z |

五折中，在同一个减号参数化下得到：

```text
-0.5857440718760298
-0.6698525121972112
-0.6589956902179078
-0.7327795497885198
-0.7039008112726385
```

因此五折范围为：

```text
[-0.7327795497885198, -0.5857440718760298]
```

这里的负号是有意的。retrospective 实现原来采用
`logit(P1)=logit(P0)+positive_beta*z`；本候选遵守需求中的减号公式，所以冻结的
`beta_rosh` 必须为负数。不能把正 beta 放入该减号公式，否则会反转信号方向。

冻结 artifact：

```text
artifact hash:
e34c8dcce4e26a0fff3d9e34967233e215377ba8aaae250cb1a5f149d6428f6a

training cohort hash:
11e344fe7ace38befa701e9a554e9c4e82736f26ade370a8f00fa2be788be1c2

source OOF manifest hash:
428883895cefe6c73ac219119dbe928762497d9b9c8944d6531df127654b9896
```

提交的小型 immutable artifact 位于
`event_intelligence/resources/prospective_rosh_candidate_v1.json`。生成脚本只接受
上述冻结的 513-row OOF manifest；若 support、公式身份、只读研究标记或 OOF hash
变化，脚本会拒绝生成候选。已有输出内容不同时也拒绝覆盖。

## R.O.S.H. scorer 身份

513-map cohort 使用的 pure scorer 是：

```text
formula:
dematus-rosh-0e1e6651dd932055dee69c4fb44435774f619793

prospective profile:
legacy-dematus-pure-rosh-prospective-v1

profile hash:
e2613889f4d4aa683949e8d8be6b081d1f55ea547a9de9d77834e9670cdb4682

scorer source hash:
60206b1319d6e8a7f20c7c56ddb3c0e8396acc061ecb1a115b6bd5736785d691
```

该 profile 只使用十英雄和 position 1–5 的固定顺序。player identity 不参与 pure
lineup scorer。Draft、Cluster、Player-Hero、odds 和 player-adjusted score 均不进入
M1。

当前 active official-v2 是另一套 scorer/profile：

```text
stratz-official-rosh/2026-07-28-v2
stratz-rosh-web-2026-07-28-v2
```

没有证据证明 official-v2 的 `relative_advantage` 与 legacy pure score 具有相同公式和
尺度。因此冻结 profile 明确记录 `official_v2_compatible=false`。official-v2 输出不能
直接送入这个候选；profile、formula 或 scorer hash 不一致时必须保存 P0-only，并记录
`rosh_evidence_invalid` 或更具体的 missing reason。

## Exact artifacts 与离线重放

每次合法 R.O.S.H. evidence 必须归档三个 request 和三个 response 的原始字节：

```text
heroes_meta_positions
hero_stats_by_time_bracket
synergy
```

每个 artifact 保存 operation、content SHA-256、gzip SHA-256、相对路径和字节数。
request replay 会验证：

1. GraphQL query hash 与冻结 profile 一致；
2. `heroIds` 恰好是该地图的十个英雄；
3. request week 不晚于 `statistics_cutoff`；
4. 十英雄无重复，position 由两边各五个有序槽位固定为 1–5；
5. response 原始字节、压缩字节和 manifest hash 全部一致；
6. response 经 legacy normalizer 和 frozen pure scorer 可离线重算相同 score。

时间语义分别保存为：

```text
statistics_cutoff <= available_at <= prediction_cutoff
```

不存在 player IDs 也可以合法 exact replay。任何 artifact、lineup、query、profile、
scorer、content hash 或时间边界不合法时，不产生 P1。

## Append-only shadow ledger

Migration `20260806_0031` 新增四张独立 research ledger 表：

| 表 | 用途 |
|---|---|
| `prospective_rosh_candidates` | immutable candidate artifact 与冻结参数 |
| `prospective_rosh_shadow_predictions` | cutoff 前写入的 P0/P1 或 P0-only 记录 |
| `prospective_rosh_shadow_settlements` | 独立追加的正式比赛结果 |
| `prospective_rosh_shadow_evaluations` | 20/100/200 的冻结窗口和报告 |

prediction 保存：

```text
match_id / series_id / prediction_cutoff
P0 / P1
pure_rosh_score / standardized_rosh_score
beta / mean / scale / logit contribution
Team Rating prediction/run/artifact/input/training identities
R.O.S.H. hero lineups/profile/formula/scorer/evidence identities
request/response artifact manifests
statistics_cutoff / available_at
content hash / created_at
```

若 cutoff 前没有合法 R.O.S.H.，只保存 P0，所有 R.O.S.H. 输出和 lineage 字段保持
NULL，并记录 `missing_reason`。

数据库 insert guard 重新核对：

- candidate 已在 cutoff 前冻结，且参数逐字段相同；
- Team Rating parent run 是 `prospective`、`trained`，prediction 是未结算的
  `predicted`，P0 和全部 artifact/input identity 一致；
- formal map 的 series、start time 与 prediction cutoff 一致；
- 写 prediction 时目标比赛还没有结果；
- R.O.S.H. evidence 在 cutoff 前 available；
- profile/formula/scorer 等于冻结候选；
- standardized score、logit contribution 和 P1 可在数据库内重算。

四张表均拒绝 UPDATE 和 DELETE。settlement 不更新 prediction，而是追加独立结果行；
它必须与 authoritative match result、result artifact 和首次可用时间一致。完全相同的
重试返回 unchanged，不同内容使用同一 identity 会报 immutable conflict。事务中任一
后续写入失败时，之前的 shadow 写入一起回滚。

这些表不被正式 prematch probability、Calibration、Deployment 或 order 路径读取。

## 预注册阶段

### 20 paired maps

只验收：

- collection；
- Team/R.O.S.H. linkage；
- offline exact replay；
- settlement；
- idempotency；
- append-only。

不允许作有效性结论。

### 100 paired maps

在第 100 个 paired map 的固定边界内报告：

- paired coverage；
- P0-only missing reasons；
- pure score distribution；
- frozen beta contribution distribution；
- profile/formula/scorer drift。

仍不允许作有效性结论，也不允许调整 candidate。

### 200 paired maps

只使用按 `prediction_cutoff, match_id` 排序的前 200 个 paired maps，冻结窗口 manifest
和 hash，进行第一次预注册比较：

```text
Brier
log loss
AUC
accuracy
ECE (10 bins)
M1 - M0 delta
series-clustered bootstrap 95% CI (2,000 samples)
event / patch / month slices
```

只有同时满足以下条件，报告才可以把
`eligible_to_propose_followup_pr` 标为 true：

1. Brier 和 log loss 的 delta 95% CI 上界均小于 0；
2. ECE 增量不超过 0.01；
3. support 至少 20 的主要切片中，Brier 恶化不超过 0.01，log loss 恶化不超过
   0.02。

即使满足，该 candidate 本身仍是 `deployment_eligible=false`；结果只允许触发新的
训练/校准/Deployment 提案。看到 200-map 结果后不能修改候选并沿用同一窗口。任何
新参数都必须成为新 candidate hash，并重新开始 prospective 收集。

## 当前 operational blocker

数据库 schema 已支持 `availability_mode=prospective` 的 Team Rating authority，但当前
producer/storage orchestration 仍只实际产出 reconstructed walk-forward Team Rating。
因此真实采集开始前必须先提供一个完整、cutoff 合法、artifact identity 可验证的
prospective Team Rating producer。shadow API 会接受并由数据库复核该 authority，
但不会用 retrospective Team Rating 冒充 prospective P0。

在这个前置条件完成、并且未来 formal maps 自然积累前，正式状态保持：

```text
prospective paired support = 0
candidate = prospective_unvalidated
deployment = unchanged
calibration = unchanged / fail-closed
orders = none
```

## 验证

聚焦验证覆盖：

- frozen candidate hash、真实参数和负 beta 符号；
- scorer/profile drift 拒绝；
- 无 player identity 的十英雄+十位置 exact replay；
- request/response artifact tamper 拒绝；
- cutoff 后 evidence 只落 P0；
- prediction/settlement hash 与幂等；
- 20/100/200 固定窗口；
- PostgreSQL P1 数学篡改拒绝；
- append-only 与事务回滚。

执行命令：

```powershell
python -m ruff check .
python -m pytest -q -m "not postgres"
$env:DATABASE_URL = 'postgresql+psycopg://dota2:dota2_local@localhost:5432/dota2_predictor'
python -m pytest tests\integration\postgres\test_prospective_rosh_shadow.py -q
```
