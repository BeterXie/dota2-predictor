# 历史比赛智能交付设计

## 文档状态

本文件是截至 2026-07-15 的历史比赛智能交付基线，覆盖选手评分、球队局势与风格画像、阵容概率模型，以及它们从 SQLite 到 API 和前端的交付边界。

它不替代直播采集、影像识别和 shadow betting 的专项设计，也不授权真实下注。仓库根目录中过时的 `DESIGN.md` 不再作为本功能的当前交付依据。

## 交付目标

用户应当能够在网页中看到并区分：

1. 历史比赛的原始赛果、击杀和双方局势标签；
2. 当前算法版本下、满足排名条件的选手逐局评分聚合；
3. 每支球队当前版本、最新 cutoff 的机会条件化风格画像；
4. 按模型类型、时间点、数据可用模式和版本完全分开的阵容模型指标；
5. 数据缺失、版本不匹配、重建数据和校准失败等限制。

“数据库中已有派生行”和“用户已能在网页看到”是两个不同的完成条件。后端表、报告、API 和 UI 必须依次交付，任一层缺失都不能宣称产品功能完成。

## 后端权威产物

| 语义 | 权威表 | 当前版本过滤 | 说明 |
|---|---|---|---|
| 正式比赛范围 | `formal_map_eligibility` | 视图内置严格赛事政策 | 只包含人工批准的 Tier 1 正赛有效地图 |
| 选手逐局评分 | `player_map_scores` | 精确匹配当前 `score_version` | 一名选手一局一行，不是比赛综合分 |
| 双方局势标签 | `team_map_states` | `label_version = team-state-v1` | 一局通常有 radiant、dire 两行 |
| 球队风格画像 | `team_style_profiles` | `profile_version = team-style-v2` 后每队取最新 cutoff | 概率后验和机会次数，不是单一战力分 |
| 阵容模型运行 | `draft_model_runs` | 配置中的 `score_version`、`model_version` | 训练运行元数据和模型状态 |
| 阵容预测 | `draft_predictions` | 与运行表联结后过滤 | 一次 OOS 目标预测一行 |
| 派生血缘 | `strict_derived_status` | 原始源、算法和画像上下文全部匹配 | 决定地图是否需要重新派生 |

`event_intelligence.report.build_intelligence_report` 是机器可读的汇总报告。它保留已有计数字段，并新增：

- `versions`：当前交付版本；
- `player_rankings`：当前精确评分版本的排名聚合；
- `team_style_profiles`：当前画像版本下每队最新 payload；
- `team_state_distribution`：当前标签版本的双方行分布；
- `draft_metrics`：按评分版本、模型版本、模型类型、时间点和 availability mode 独立分组的指标与状态。

报告不是前端数据库访问层。网页仍应通过只读 API 获取数据。
为避免周期报告膨胀，球队画像会保留机会、后验和分位数等可展示字段，但将逐图 weighting/evidence 压缩为地图数和总权重；完整逐图血缘仍以数据库原行为准。

## API 与 UI 边界

只读 API 的交付面为：

- `GET /api/intelligence/overview`：版本、覆盖率、局势分布和阵容质量切片；
- `GET /api/intelligence/matches`：可搜索、筛选、分页的历史比赛；
- `GET /api/intelligence/matches/{match_id}`：比赛、双方标签、十名选手当前评分，以及 OpenDota 原始赛后表现；
- `GET /api/intelligence/players`：当前评分版本的选手排名；
- `GET /api/intelligence/teams`：当前画像版本的每队最新画像。

API 层负责：

- 精确版本过滤；
- JSON 字段解析和稳定的空值语义；
- 分页、搜索和标签筛选；
- 将 reconstructed 与 prospective 数据分开；
- 返回明确的 calibration status 和失败原因。

前端层负责：

- 将历史比赛、选手排名、球队画像和阵容验证显示为不同视图；
- 在比赛详情中紧凑显示 K/D/A、GPM/XPM、补刀/反补、净值和英雄/建筑伤害，明确与算法评分区分；
- 显示当前版本、样本量、coverage、role confidence 和 cutoff；
- 对 `missing`、`unsupported`、`failed`、`provisional` 使用不同状态文案；
- 在无数据和 API 错误时显示真实空态，而不是补造默认分数；
- 将 reconstructed 数据明确标成“历史重建/回放验证”，不能标成“实时验证”。

前端不能直接读取 SQLite、重新实现评分公式，或把多个版本的行合并后再排名。

## 规范语义

### 比赛比分

`matches.radiant_score` 和 `matches.dire_score` 是来源记录的击杀数。前端应标注为“击杀”或“击杀比分”。

当前系统没有批准的“历史比赛综合评分”或 `match_score`。不得把击杀、选手均分、球队画像后验、阵容概率或胜负结果拼成一个看似权威的比赛分数。如果以后要增加比赛综合分，必须另行定义公式、版本、校准和解释字段。

### 选手评分

- `execution_score`：0–100 的个人执行分，不直接奖励胜负；
- `result_adjusted_score`：在执行分基础上加入受限的赛果/转化修正；
- 50 是中性点，不代表 50% 胜率；
- 排名只使用 `explanation_json.ranking_eligible = 1` 的当前版本行；
- coverage 或角色置信度不足的行可以展示，但不能偷偷进入位置排名。
- `performance` 中的 K/D/A、GPM/XPM、补刀、净值和伤害是 OpenDota 赛后原始事实；它们与 0–100 的算法评分并列展示，但不能互相替代。

### 优势、劣势、碾压和翻盘

标签以球队为单位，因此一局有一对语义：

| 胜方/一方 | 对手行 | 中文展示 |
|---|---|---|
| `comeback` | `throw` | 翻盘 / 被翻盘 |
| `stomp` | `stomp_loss` | 碾压 / 被碾压 |
| `advantage` | `disadvantage` | 优势局 / 劣势局 |
| `even` | `even` | 均势局 |
| `state_unscorable` | 视数据而定 | 局势数据不足 |

`team_state_distribution` 统计的是球队行，不是比赛数；例如一场均势局通常贡献两条 `even`。比赛数量必须按 `match_id` 去重或按配对规则计算。

局势标签来自金钱曲线、持续时间和阈值规则。击杀比分不能替代缺失的金钱曲线。

### 球队画像

球队画像包含机会次数、Beta-Binomial 后验、持续时间分位数、有效样本量和逐图权重。它回答“在出现某类机会时，这支队伍过去如何转化”，不等于全局战力评分，也不应缩成没有解释的单一 0–100 数字。

页面应同时显示 profile cutoff、effective sample size 和主要 posterior rate；样本量很低时应标注不稳定。

### 阵容概率

阵容模型输出 Radiant 获胜概率。10/20/30/40/50 分钟切片是按达到相应 landmark 的训练/评估人群形成的模型切片，不是从该分钟实时金钱差计算出的现场胜率，也不是比赛评分。

`pure_draft` 与 `context_adjusted` 必须分开；不同 horizon 的概率也不能直接当成同一模型的连续实时曲线插值。

## 当前版本策略

当前批准版本为：

- observed role：`role-assignment-v1-reconstructed-walk-forward`；
- player score：`player-score-v3+observed-role=role-assignment-v1-reconstructed-walk-forward`；
- team state：`team-state-v1`；
- team profile：`team-style-v2`；
- draft model：`draft-logistic-l2-v1`；
- draft backtest：`strict-draft-walk-forward-v1`。

查询规则：

1. 详情和排名精确匹配当前完整 `score_version`，不能只匹配 `player-score-v3` 前缀；
2. 兼容性总计可以保留按 v3 family 统计，但必须同时返回 `player_scores_by_version`；
3. 球队画像先过滤 `profile_version`，再按 `profile_cutoff` 和稳定主键选每队最新行；
4. 局势分布精确过滤 `label_version`；
5. 阵容指标至少按 `score_version + model_version + model_kind + horizon_minutes + availability_mode` 分组；
6. `reconstructed_walk_forward` 与 `prospective` 永不合并；
7. 不使用“最后创建的一行”代替版本过滤。

## 赛事元数据与派生血缘

球队画像会读取赛事等级、奖池和正式范围政策。仅记录比赛原始 hash 无法发现 EWC 奖池等注册表元数据修正，因此 `strict_derived_status` 额外保存每个赛事独立的 `profile_context_hash`。

哈希输入是规范化后的赛事画像/范围上下文，包括赛事 ID、tier、奖池、scope policy、批准/证据状态、included/excluded stages 和 LCQ/资格赛等排除开关。JSON 字段先解析再以排序键的紧凑 JSON 编码，最终使用 SHA-256。

行为约束：

- 新派生行写入当时的 `profile_context_hash`；
- pending 检测将保存值与该地图所属赛事的当前上下文比较；
- 旧数据库加法迁移后该字段保持 `NULL`，因为系统无法证明旧画像使用了哪个元数据快照；这些行必须重新派生，不能在迁移时被静默“认证”；
- 单个赛事元数据变化首先只标记该赛事地图，不使用全局 registry hash；
- 若变化会影响后续 earlier-only benchmark/profile，现有 causal-successor 规则继续重建真正受因果影响的后续地图；
- 派生期间元数据再次变化时，source snapshot 校验失败，不能提交错误血缘。

## 校准与展示警告

阵容模型至少满足以下条件后才有资格被称为通过校准：

- settled support 不低于 100；
- Brier score `< 0.25`；
- log loss `< ln(2)`；
- 五箱 ECE 和 bootstrap 上界满足批准门槛；
- 数据模式和目标版本一致。

汇总报告中的 `validation_status` 规则为：

- `unsupported`：样本不足或没有可结算点；
- `failed`：样本足够但 Brier/log loss 门槛失败；
- `provisional`：点估计通过，但报告本身未包含完整 bootstrap ECE gate，不能显示为 passed。

截至 2026-07-15 的主数据库快照只有 `reconstructed_walk_forward` 数据。pure/context 两类模型在五个 horizon 的十个切片均未通过 Brier/log-loss 门槛；没有 prospective 样本。因此 UI 必须显示“历史重建且校准未通过”，不能展示“模型准确”“实时胜率可靠”或任何投注优势结论。

## 完成判据

本交付只有同时满足以下条件才算完成：

1. 元数据变化能通过 `profile_context_hash` 使受影响地图进入 pending，并完成必要的因果后续重建；
2. 报告包含当前选手排名、球队画像、局势分布和分离的阵容指标；
3. API 对缺表、空数据和旧版本安全退化，并保持只读；
4. 前端能从 API 查看上述四类信息，且显示版本、样本与限制；
5. 页面不再把击杀比分称为比赛评分，也不创造未经设计的综合分；
6. 自动测试覆盖版本隔离、元数据失效、标签行/比赛数语义和 reconstructed/prospective 分离。
7. 比赛详情同时展示 OpenDota 原始选手表现和当前版本评分，缺少原始字段时保持明确空值而不补造数据。
