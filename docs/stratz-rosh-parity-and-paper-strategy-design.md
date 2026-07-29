# STRATZ R.O.S.H. 精确复刻与纸面策略接入程序设计

| 项目 | 内容 |
|---|---|
| 状态 | Contract corrected；v2 bundle parity candidate |
| 日期 | 2026-07-29 |
| 适用范围 | STRATZ R.O.S.H. 历史/赛前分析、direct-only shadow/paper 策略 |
| 不适用范围 | 真实资金下注、自动提交投注、绕过 Cloudflare 或真人验证 |
| 关联治理 | [ADR-0010](adr/0010-versioned-executable-strategy-contract.md)、[ADR-0004](adr/0004-m3-calibration-and-economic-cohorts.md)、[领域术语表](betting-decision-domain-glossary.md) |

## 1. 决策摘要

本设计将当前 dematus 口径的 Rosh 实现与“STRATZ 官方网页口径”并行保留，新增一个不可变的 Rosh parity profile，并用录制的 GraphQL 响应执行纯函数计算。已冻结但未激活的 Rosh parity v1 不原地改义；修正后的 v2 是唯一 bundle parity candidate。旧 dematus 实现和旧 v4 决策也不重写。

“百分百复刻”在本项目中的可验收定义是：

> 对同一个已冻结 parity profile、完全相同的 GraphQL 查询变量和完全相同的 GraphQL 响应，服务端输出的英雄分、双方队伍分、相对优势分和分钟曲线在官方展示精度上逐项相等。

它不表示 STRATZ 将来修改前端公式、查询窗口、数据源或舍入方式后，旧 profile 仍自动等同于新版本。上游发生变化时必须冻结旧 profile、创建新 profile 并重新跑黄金样例，不能静默修改旧版本。

新策略只把 Rosh 当作阵容方向证据：

- 正分表示 Radiant 方向，负分表示 Dire 方向；
- Rosh score 和 Rosh minute score 都不是胜率；
- 禁止使用 (50 + score) / 100；
- 禁止直接用分数大小计算 edge 或 stake；
- 概率必须来自单独版本化、经前向样本验证的 calibration artifact；
- 策略继续保持 direct-only shadow/paper，不新增真实下注端点。

## 2. 已确认差异

当前代码 [prematch/stratz_rosh.py](../prematch/stratz_rosh.py) 复刻的是 dematus 版本，不是 2026-07-28 观察到的 STRATZ 网页算法。主要差异包括：

| 维度 | 当前本地实现 | 官方网页 profile |
|---|---|---|
| 英雄基础分 | 500 场先验、队均值、额外权重 | position day rows 从新到旧整行纳入，累计到至少 1000 后停止，再计算 win rate - 50 和可靠性收缩 |
| 时间曲线 | 三分钟窗口、tempo 相对基础分、500 场先验 | 累计行差分为 bucket；按相邻行窗口计算，rank exact bucket 以 1000 为 fallback 阈值 |
| Synergy | 四周全部样本加权后收缩，另有总幅度 cap | 周行从新到旧整行纳入；每周合并归一化到两位，累计到至少 100 后停止，不足 100 再收缩 |
| 玩家修正 | 可加入当前 playerHeroHighlight | 该历史比赛 profile 不使用当前玩家修正 |
| 最终语义 | 被包装成“概率” | 有符号阵容优势分，不是概率 |

对 match 8904419709：

- 当前本地 synergy 净值约为 +13.58，最终为 +13.3；
- STRATZ 官方相对优势为 +5.8；
- 当前 [web/app.py](../web/app.py) 又将 +13.3 映射为 63.3%，该映射没有官方或校准依据。

因此不能只调一个常量。查询 profile、归一化、公式、舍入、持久化语义和策略消费方式都必须一起版本化。

## 3. 领域模型

### 3.1 Rosh score

Rosh score 是某一套十英雄、位置、统计窗口和 parity profile 下的有符号阵容优势分。正数指向 Radiant，负数指向 Dire，零表示该 profile 下无方向优势。

它不是：

- Radiant 胜率；
- 模型概率；
- 相对市场的 edge；
- 可直接用于 stake sizing 的强度。

### 3.2 Rosh minute score

Rosh minute score 是指定分钟 bucket 的有符号方向分。实时策略只能选择当前比赛已经到达的最新 bucket：

~~~text
selected_minute = max(m for m in available_minutes if m <= floor(game_clock_seconds / 60))
~~~

不存在满足条件的 bucket 时状态为 unavailable。禁止读取未来 bucket，也禁止用未来 bucket 插值。

### 3.3 Rosh parity profile

Rosh parity profile 是一份不可变的可执行身份，至少绑定：

~~~text
rosh_profile_id
formula_version
request_profile_hash
upstream_bundle_hash
scorer_source_hash
canonical_profile_hash
serialization_version
~~~

其中：

- rosh_profile_id 是人类可读且全局唯一的版本名；
- formula_version 指向服务端纯函数 scorer 的版本；
- request_profile_hash 覆盖 GraphQL operation 顺序、query 文本和变量规划规则；
- upstream_bundle_hash 绑定本次逆向核对的 STRATZ 前端 bundle；
- scorer_source_hash 绑定实现该公式合同的 scorer 源工件；
- canonical_profile_hash 是不含自身字段的完整 profile identity canonical projection 的 SHA-256；
- serialization_version 固定 canonical JSON 和 hash 规则。

任一字段变化都创建新 profile。禁止让同一个 rosh_profile_id 对应多组 hash；缺少实际 scorer_source_hash 或 canonical_profile_hash 的 profile 不能进入运行时。

### 3.4 Calibrated probability

Calibrated probability 是独立模型根据冻结的 feature schema、训练 cutoff、前向 cohort 和 calibration artifact 产生的 [0, 1] 概率。Rosh 原始分只有在该 artifact 明确包含、验证并版本化此特征时，才可间接参与概率。

## 4. 目标与非目标

### 4.1 目标

1. 精确重放被固定的 STRATZ 官方 Rosh profile。
2. 对每次运行保留查询、响应、公式和结果的完整 lineage。
3. 历史比赛、手工赛前阵容和实时已确认阵容共用一个 scorer。
4. UI 正确展示队伍分、相对优势、英雄拆分和分钟曲线。
5. 以新策略版本把 Rosh 接成 underdog 方向 gate/证据。
6. 旧 dematus scorer 和 comeback v4 可按原合同重放。
7. 所有新行为仅进入 shadow/paper 链。

### 4.2 非目标

1. 不承诺自动跟随 STRATZ 未来更新。
2. 不把 Rosh 分数解释为胜率。
3. 不从 STRATZ 网页 DOM、Chrome session、Cloudflare cookie 或截图取得生产输入。
4. 不实现或调用真实资金 betting endpoint。
5. 不用本次历史黄金样例训练概率或调下注阈值。
6. 不将重建历史样本伪装成 prospective M3 样本。

## 5. 总体架构

~~~mermaid
flowchart LR
    A["历史 match / 已确认 draft"] --> B["Official request planner"]
    B --> C["STRATZ authenticated GraphQL transport"]
    C --> D["Immutable sanitized response artifacts"]
    D --> E["Official response normalizer"]
    E --> F["Pure official scorer"]
    F --> G["RoshAnalysisRun"]
    G --> H["Prematch detail API/UI"]
    G --> I["RoshDirectionEvidence"]
    J["Direct live clock + team side + draft hash"] --> I
    I --> K["Versioned shadow strategy contract"]
    L["Independent calibrated probability"] --> K
    M["Direct odds / Vision / authority gates"] --> K
    K --> N["Strategy decision"]
    N --> O["Paper order only"]
~~~

权威边界：

- STRATZ GraphQL API 响应是 Rosh 数据权威；
- 已捕获的 compiled bundle 与逐字 GraphQL fixture 是公式的上游证据；
- 本版本化设计、profile 和 executable strategy contract 是 production authority，scorer 必须实现它们且以 hash 绑定；
- direct transport 是赔率权威；
- 已确认 Vision/live observation 是比赛时钟和局面权威；
- 浏览器观察只用于建立或审核 profile，不可进入运行时 decision lineage。

## 6. Profile 与请求规划

### 6.1 冻结 v1 与 v2 candidate

以下 v1 identity 已冻结：

~~~text
rosh_profile_id      = stratz-rosh-web-2026-07-28-v1
formula_version      = stratz-official-rosh/2026-07-28-v1
request_profile_hash = 9dee18e1e74b14bb08761ade1db59b9c408abc03d8c4e797b0116bf45bc0fceb
state                = frozen, unactivated, superseded-for-implementation
~~~

v1 只允许用于审计已捕获工件。不得修改或复用其 ID、formula_version、request_profile_hash 或任何既有 hash 来承载本次修正；runtime request planning、normalize 和 score 收到 v1 都必须 fail closed。

修正后的唯一 bundle parity candidate 是：

~~~text
rosh_profile_id = stratz-rosh-web-2026-07-28-v2
formula_version = stratz-official-rosh/2026-07-28-v2
state           = candidate；D4 前不得 active-for-scoring
~~~

v2 固定 `serialization_version=rfc8785-jcs/v1`、`presentation_rounding=js-number-to-fixed/1`、position/synergy/time 阈值 1000/100/1000，并引用第 8 节同一组 captured request/response/bundle/expected 工件。D2 完成 scorer 修正后，D3 才能根据实际源工件计算并绑定 scorer_source_hash 和新的 canonical_profile_hash；本文不预填、猜测或复用这些 hash。两者未绑定或不匹配时 v2 必须 fail closed。

Registry 启动时验证：

- profile artifact 可按 RFC 8785/JCS 重算；
- request query 文本和变量规划 artifact 的 hash 匹配；
- scorer source artifact/hash 匹配；
- canonical_profile_hash 可从完整 identity projection 重算；
- profile ID 不存在冲突注册；
- hash 缺失或不匹配时 fail closed。

### 6.2 输入模式

Request planner 接受两个互斥输入：

~~~text
historical_match:
  match_id
  date_time
  bracket_ids

explicit_draft:
  radiant[5] = hero_id + position_id
  dire[5]    = hero_id + position_id
  date_time
  bracket_ids
~~~

约束：

- date_time 是 Unix UTC 秒，并进入 request identity；
- bracket_ids 在本 v2 profile 中只允许 ["IMMORTAL"]；
- 每方必须恰好五个不同英雄；
- 每方 position_id 必须恰好覆盖 1..5；
- 两方英雄不能重复；
- team side 只允许 RADIANT/DIRE；ROGUE 等未知 side 或总计第 11 个 slot 必须在 normalize/score 前拒绝；
- historical_match 从 GetMatchPicksBans 解析阵容后执行同样验证；
- replay 必须使用原始 date_time，不能替换为 datetime.now()；
- 运行时计算出的 skip/week anchors 必须被持久化，不能在重放时重新按当前日期计算。

### 6.2.1 Rosh analysis attempt 与 run creation boundary

Rosh analysis attempt 是从请求开始到 canonical draft 被完整验证以前的尝试。pre-draft failure 不是 failed RoshAnalysisRun。RoshAnalysisRun 只有在绑定合法的 10-slot canonical draft 与其 draft_hash 后才成立，并且是不可变的 succeeded/failed 终态分析记录。

- historical_match 只有在 GetMatchPicksBans 阵容通过恰好十个英雄、双方 position_id 1..5 完整、side 仅为 RADIANT/DIRE、全局英雄不重复的验证后，才跨过 run creation boundary；
- explicit_draft 在 request plan 建立前完成同一套 canonical draft 验证，因此后续 transport 失败可以形成 failed run；
- historical_match 在跨界前发生 transport、HTTP、GraphQL、JSON、阵容非法或阵容不完整失败时，只返回脱敏的结构化错误并发送运维 metric/log；不得形成 RoshAnalysisRun，不得伪造 null、空或 partial draft，也不得使用全零 hash；
- historical_match 建立合法 canonical draft 后，后续可归类的 normalizer、scorer、artifact 或 repository 失败可以形成 failed run；该 run 必须绑定真实 draft、request/profile identity、已有的脱敏 manifest 与稳定 error_code，并且没有 result 或 hero/minute children；
- 此边界澄清不改变 schema v11。未来若需要长期持久化 pre-draft attempts，必须作为独立 ADR/扩展提出，不能塞入 RoshAnalysisRun。

### 6.3 官方 operation profile

query 文本不得手工改写或“等价简化”。P0 从已核对 bundle/网络请求逐字冻结 query document、operation 顺序、变量类型和 null/omitted 行为。

| Operation | 固定输入/行为 | 用途 |
|---|---|---|
| GetMatchPicksBans | matchId | 历史阵容、位置和比赛上下文 |
| HeroesMetaPositions | bracketIds=["IMMORTAL"], take=7, skip=days_ago | 七日 position 基础数据 |
| GetMatchCountPreviousWeekDay | 精确使用 profile 的 dateTime/日界线规则 | 解析周窗口锚点 |
| Synergy | bracketBasicIds="DIVINE_IMMORTAL", matchLimit=0, take=200，当前周加前三周 | 同队和对手 synergy |
| GetHeroStatsByTime | 不带 rank filter 的 all-rank 请求 | 小样本 fallback |
| GetHeroStatsByTime | bracketBasicIds=["DIVINE_IMMORTAL"] | 高分段分钟数据 |

days_ago 的计算也属于 request profile。实现必须：

1. 使用 profile 声明的 UTC 日界线算法；
2. 将最终整数 skip 和所有 week 参数写进 request_json；
3. 黄金重放直接读取冻结变量；
4. 为 UTC 00:00 前后和夏令时无关性写边界测试。

v1 的既有请求 identity 保持冻结，不追溯改义。v2 对 GraphQL batch 的数组顺序、显式 index、operationName、逐字 query 文本、query_sha256 和 variables 使用以下 canonical projection 计算 request_hash；示例中的 query 仅为省略展示，实际 canonical projection 包含完整 UTF-8 文本：

~~~json
{
  "endpoint": "https://api.stratz.com/graphql",
  "operations": [
    {
      "index": 0,
      "operation_name": "...",
      "query": "... exact captured UTF-8 text ...",
      "query_sha256": "...",
      "variables": {}
    }
  ]
}
~~~

Authorization、cookie、User-Agent 和临时 request headers 不进入 request_hash，也不得落盘。query、query_sha256、request_hash、variables、operation 数组顺序或 index 任一漂移都必须 fail closed，不能以“语义等价”为由继续执行。

### 6.4 Transport

在 [live_betting/stratz_rosh_client.py](../live_betting/stratz_rosh_client.py) 中复用现有 token 解析、超时和脱敏错误能力，但新增 official-profile 方法，不改变旧方法语义。

要求：

- token 只从 STRATZ_API_TOKEN 或兼容环境变量读取；
- 只访问 profile 注册的 HTTPS endpoint；
- 显式 timeout；
- 429/5xx 按有上限的指数退避和 Retry-After 重试；
- GraphQL errors 即使 HTTP 200 也必须分类；
- 一个 operation 缺失不能用空对象冒充成功；
- 相同 request_hash 可做并发合并；
- cache key 至少包含 profile ID、date_time、draft_hash 和 request_hash；
- 不在日志、异常、artifact 或 API 响应中输出 token/cookie。

## 7. 官方归一化与算法

### 7.1 归一化原则

normalizer 只负责 schema 校验和字段投影，不做业务打分。输出统一使用有限数值，并保留整数 matchCount/winCount。

完整 operation 缺失、GraphQL errors、非有限数、负样本数、winCount > matchCount、重复位置或阵容不完整时，整次分析失败。

以下情况按官方行为处理，而不是误判为 transport 失败：

- 一个合法完整 synergy 响应里没有某一英雄对：该 pair 值为 0；
- rank-specific exact bucket matchCount 不足 1000：回退到对应 all-rank 相邻行 window；
- rank 和 all-rank 对应项都不存在：该分钟点不可完整计算，点状态为 unavailable；
- position 样本低于 1000：按线性可靠性收缩，不回退为其他位置。

### 7.2 Position 基础分

对每个 hero_id + position_id，将 day rows 按 newest-to-oldest 排序。仅在纳入该行前 cumulative matchCount < 1000 时加入下一整行的 winCount 和 matchCount；某行使累计跨过 1000 时不截断该行，纳入后停止读取更旧行：

~~~text
cumulative_wins = 0
cumulative_matches = 0

for day in newest_to_oldest:
    if cumulative_matches >= 1000:
        break
    cumulative_wins += day.winCount
    cumulative_matches += day.matchCount
~~~

因此 900 场后遇到 200 场的 day row，结果是完整 1100 场，不是 1000 场。这也不同于无条件“汇总全部七日”；阈值已经由较新的整行达到时，更旧 day rows 不参与。每个英雄位置的基础分：

~~~text
position_win_rate_diff = cumulative_wins / cumulative_matches * 100 - 50
position_reliability   = min(cumulative_matches / 1000, 1)
position_base_diff     = position_win_rate_diff * position_reliability
~~~

matchCount 为 0 时该位置输入无效，不允许除零或默认为 50%。

### 7.3 Synergy 四周聚合

对每个有方向的 hero pair 和类型（with/vs），从最新周向最旧周处理。只有进入下一周前的 cumulative matchCount 已经 >= 100 才停止；否则加入下一整周 row，跨过 100 的 row 不截断。每次周合并后立即按 bundle 的 `Math.round(100 * x) / 100` 归一化到两位；所有可用周处理完而最终累计仍不足 100 时，才按 cumulative matchCount / 100 收缩，并再次执行同一两位归一化：

上游证据是冻结 bundle 的 module 78066（文件偏移约 56868）中的 `if(r.matchCount>=100)return`、整周 `r.matchCount+n.matchCount` 加权合并和 `j=e=>Math.round(100*e)/100`。本节是该证据的 production contract，不允许运行时重新解释 minified code。

~~~text
pair_synergy = 0.0
cumulative_count = 0

for week in newest_to_oldest:
    if cumulative_count >= 100:
        break
    combined_count = cumulative_count + week.match_count
    pair_synergy = js_round_2(
        pair_synergy * (cumulative_count / combined_count)
        + week.synergy * (week.match_count / combined_count)
    )
    cumulative_count = combined_count

if cumulative_count < 100:
    pair_synergy = js_round_2(pair_synergy * cumulative_count / 100)
~~~

其中 `js_round_2(x)` 必须执行 JavaScript Number 的 `Math.round(100 * x) / 100`，不是十进制定点 half-up，也不是 Python `round`。`Math.round` 的负半值向正无穷取整，并可能产生 negative zero；例如 `Math.round(-0.5) / 100` 为 `-0`。实现和测试必须保留这套 IEEE-754/JavaScript 中间计算语义，不能预先把 `-0` 改成正零或改用绝对值舍入；最终 JavaScript/JCS 序列化显示为 `0` 不构成改变中间公式的许可。

- 跨阈值的整周 row 全量纳入，累计 count 可以大于 100；
- 不足 100 场时才按 cumulative_count / 100 收缩；
- 完全无样本时为 0。

例如 `[(synergy=10, count=99), (synergy=-10, count=100)]` 的 bundle 结果必须是 pair synergy `-0.05`、累计 count `199`。不得先把四周全部平均后再 cap，不得把 crossing row 切片，也不得使用当前 dematus 的全量加权结果。

每个英雄：

~~~text
same_team_synergy(hero) =
    sum(pair_with_synergy(hero, teammate) for every other teammate)

opponent_matchup_synergy(hero) =
    sum(pair_vs_synergy(hero, opponent) for all five opponents)

hero_score(hero) =
    position_base_diff(hero, position)
    + same_team_synergy(hero)
    + opponent_matchup_synergy(hero)
~~~

缺失 pair 为 0，但缺失整个 Synergy operation 为失败。

### 7.4 队伍分和相对优势

~~~text
radiant_team_score = sum(radiant hero_score)
dire_team_score    = sum(dire hero_score)
relative_advantage = radiant_team_score - dire_team_score
~~~

relative_advantage > 0 指向 Radiant；relative_advantage < 0 指向 Dire。

除第 7.3 节规定的 Synergy 每周合并与最终小样本收缩必须执行 `js_round_2` 外，hero/team/relative 不做其他逐步舍入。它们从公式结果独立格式化为一位小数，不能用十个已经显示为一位小数的字符串反推。

### 7.5 分钟分数与 rank fallback

rank-specific 和 all-rank 分别对同一 hero + position 的 cumulative rows 按 minute 升序排列。每个索引的 exact bucket 由当前累计行减下一累计行得到；最后一行直接使用自身 counts：

~~~text
bucket[i].win_count   = cumulative[i].win_count - cumulative[i + 1].win_count
bucket[i].match_count = cumulative[i].match_count - cumulative[i + 1].match_count

window = bucket_rows.slice(max(0, i - 1), min(length, i + 2))
window_win_rate = sum(window.win_count) / sum(window.match_count)
~~~

`slice(i-1, i+2)` 表示按排序后的相邻 row index 取前一行、当前行和后一行，并在首尾截边；不是按 `abs(other.minute - minute) <= 1` 选分钟。即使分钟缺号也仍按相邻 row：只有 minute 20 和 22 时，两者必须互为相邻 window rows，不能因为相差 2 而排除。

fallback 阈值只检查当前索引的 exact bucket matchCount，不检查 cumulative count 或 window matchCount：

~~~text
if divine_immortal_exact_bucket.match_count >= 1000:
    selected_window = divine_immortal_window
    source = "DIVINE_IMMORTAL"
else:
    selected_window = all_rank_window_for_same_minute
    source = "ALL_RANK_FALLBACK"

time_win_rate_diff = selected_window.win_count / selected_window.match_count * 100 - 50
~~~

阈值 1000 包含端点。exact bucket 999 必须 fallback，1000 必须使用 rank-specific；对应 all-rank minute/window 缺失时该 slot unavailable。

先计算不含 position_base_diff 的 synergy delta：

~~~text
radiant_synergy_total =
    sum(radiant same_team_synergy + radiant opponent_matchup_synergy)

dire_synergy_total =
    sum(dire same_team_synergy + dire opponent_matchup_synergy)

synergy_delta = radiant_synergy_total - dire_synergy_total
~~~

每个可用分钟：

~~~text
minute_score =
    (
        sum(radiant time_win_rate_diff)
        - sum(dire time_win_rate_diff)
    ) / 10
    + synergy_delta
~~~

分钟输出独立舍入为一位小数。每个点同时保留十个 slot 的 source、exact_bucket_match_count 和 window_match_count，供 fallback 与窗口审计；fallback 是数据来源标签，不是概率置信度。

### 7.6 舍入

profile 的输出展示舍入规则为 STRATZ bundle 观察到的 JavaScript Number 展示语义：

~~~text
js-number-to-fixed/1
~~~

实现一个唯一的 profile_round(value) 输出 helper，并用正负半边界、浮点邻界和黄金样例测试。第 7.3 节的 `js_round_2` 是公式内部唯一允许的逐周归一化，不得被 profile_round 取代。禁止在各模块分别调用 Python round、PHP half-up 或前端二次计算。API 返回 number 和 display_value；前端直接展示 display_value，不再自行重算。

## 8. 黄金样例

固定 fixture：

~~~text
match_id   = 8904419709
bracketIds = ["IMMORTAL"]
dateTime   = 1784485548
captured fixture label = stratz-rosh-web-2026-07-28-v1 (frozen audit evidence only)
parity candidate       = stratz-rosh-web-2026-07-28-v2
~~~

v2 引用以下同一组冻结工件；引用不改变其中的 v1 candidate 标签，也不把 v1 激活：

~~~text
bundle file          = upstream-bundle-7473.55187c1bd3991522.js
bundle sha256        = 9f11c70b970bab3de71f517c36551dca2cee143d176d86c649f3542a2fe90357
request body sha256  = 280f11b38a29c87751c4f36c74d95d4b89bf087f00b766331fbbe379f551971f
response body sha256 = 2afbe95c420676d34b87737138133443673a8d8c9e7d2bf10069712e799e70e7
expected sha256      = 743b67ec2c5628934cea6834ee6832a951634179bdafcdbcdfa8b139d6d7305b
page-assets sha256   = ec5f08ca6c54779ee3a76a5f81401761d668908acc68d5b08d715b8e9634b70e
manifest sha256      = b4c14b14ed283d78123aa5ed9724f56db7e2a055e393dcde0a14d620820fb0fb
~~~

捕获到的精确变量：

| Operation | Variables |
|---|---|
| GetMatchPicksBans | {"matchId": 8904419709} |
| HeroesMetaPositions | {"bracketIds":["IMMORTAL"],"take":7,"skip":8} |
| GetMatchCountPreviousWeekDay | {"bracketIds":["IMMORTAL"]} |
| Synergy | {"bracketBasicIds":"DIVINE_IMMORTAL","matchLimit":0,"take":200} |
| GetHeroStatsByTime (all rank) | {"week":1784485548} |
| GetHeroStatsByTime (rank) | {"bracketBasicIds":"DIVINE_IMMORTAL","week":1784485548} |

精确 query 文本以 fixture 的 requests.json 为准；bundle 中动态嵌入的周窗口和英雄过滤条件不得从上表的 variables 缺省项推断。

英雄结果：

| 队伍 | 英雄 | 官方分数 |
|---|---|---:|
| Radiant | Lifestealer | 12.4 |
| Radiant | Pangolier | -11.1 |
| Radiant | Slardar | -1.2 |
| Radiant | Keeper of the Light | -1.6 |
| Radiant | Hoodwink | -3.4 |
| Dire | Kez | -10.9 |
| Dire | Invoker | -3.4 |
| Dire | Centaur Warrunner | 5.1 |
| Dire | Shadow Demon | -3.5 |
| Dire | Disruptor | 2.0 |

汇总结果：

~~~text
Radiant team score = -4.9
Dire team score    = -10.7
Relative advantage = +5.8
~~~

分钟结果：

| Minute | 官方分数 |
|---:|---:|
| 20 | -7.0 |
| 30 | -5.7 |
| 36 | -5.5 |
| 37 | -5.5 |
| 40 | -5.6 |
| 50 | -5.8 |
| 60 | -6.0 |

黄金测试必须读取冻结的 sanitized GraphQL fixture，不能请求在线 STRATZ 后再拿动态数据与静态期望比较。在线测试是单独的 drift/integration job，不得替代 deterministic test。黄金输出和上述 hash 保持不变是必要条件，但不能替代第 17 节的非黄金反例、身份拒绝和 drift tests。

## 9. 数据合同

### 9.1 RoshAnalysisRun

~~~json
{
  "schema": "rosh-analysis-run/v1",
  "run_id": "sha256:...",
  "status": "succeeded",
  "mode": "historical_match",
  "match_id": 8904419709,
  "date_time": 1784485548,
  "draft_hash": "sha256:...",
  "draft": {
    "radiant": [
      {"hero_id": 54, "position_id": 1},
      {"hero_id": 120, "position_id": 2},
      {"hero_id": 28, "position_id": 3},
      {"hero_id": 90, "position_id": 4},
      {"hero_id": 123, "position_id": 5}
    ],
    "dire": [
      {"hero_id": 145, "position_id": 1},
      {"hero_id": 74, "position_id": 2},
      {"hero_id": 96, "position_id": 3},
      {"hero_id": 79, "position_id": 4},
      {"hero_id": 87, "position_id": 5}
    ]
  },
  "profile": {
    "rosh_profile_id": "stratz-rosh-web-2026-07-28-v2",
    "formula_version": "stratz-official-rosh/2026-07-28-v2",
    "request_profile_hash": "sha256:...",
    "upstream_bundle_hash": "sha256:...",
    "scorer_source_hash": "sha256:...",
    "canonical_profile_hash": "sha256:...",
    "serialization_version": "rfc8785-jcs/v1"
  },
  "request_hash": "sha256:...",
  "response_artifacts": [
    {
      "operation_name": "GetMatchPicksBans",
      "request_artifact_hash": "sha256:...",
      "response_artifact_hash": "sha256:..."
    }
  ],
  "result": {
    "radiant_team_score": -4.9,
    "dire_team_score": -10.7,
    "relative_advantage": 5.8,
    "hero_scores": [],
    "minute_points": []
  },
  "collected_at": "2026-07-29T00:00:00Z",
  "evidence_hash": "sha256:..."
}
~~~

合同要求：

- draft 按 team side、position_id 1..5 canonical 排序；
- 上述省略号只表示文档未编造 D3 实际值；成功运行必须携带 D3 绑定的完整 scorer_source_hash 和 canonical_profile_hash，placeholder/null 均非法；
- run_id 由 profile identity、request_hash、response artifact hashes 和 result canonical projection 派生；
- status 只能是 succeeded 或 failed 的终态；只有跨过 run creation boundary 的重试才创建新 run，且不修改旧结果；
- failed run 用结构化 error_code，不持久化未经脱敏的异常文本，也不携带 result 或 hero/minute children；
- number 必须有限，JSON 禁止 NaN/Infinity；
- evidence_hash 覆盖所有影响结果的输入和输出。

### 9.2 HeroScore

~~~json
{
  "hero_id": 54,
  "team_side": "RADIANT",
  "position_id": 1,
  "position_base_diff": 0.0,
  "same_team_synergy": 0.0,
  "opponent_matchup_synergy": 0.0,
  "raw_score": 0.0,
  "display_score": 12.4
}
~~~

raw_score 用于重放；display_score 由 profile_round 唯一生成。英雄名称是展示元数据，不进入 scorer identity。

### 9.3 MinutePoint

~~~json
{
  "minute": 36,
  "radiant_time_delta": 0.0,
  "dire_time_delta": 0.0,
  "synergy_delta": 0.0,
  "raw_score": 0.0,
  "display_score": -5.5,
  "rank_source_counts": {
    "DIVINE_IMMORTAL": 6,
    "ALL_RANK_FALLBACK": 4
  },
  "slots": []
}
~~~

### 9.4 RoshDirectionEvidence

~~~json
{
  "schema": "rosh-direction-evidence/v1",
  "analysis_run_id": "sha256:...",
  "draft_hash": "sha256:...",
  "rosh_profile_id": "stratz-rosh-web-2026-07-28-v2",
  "game_clock_seconds": 2196,
  "selected_minute": 36,
  "radiant_score": -5.5,
  "underdog_side": "DIRE",
  "underdog_direction_score": 5.5,
  "direction": "supports_underdog",
  "completeness": "complete",
  "evidence_hash": "sha256:..."
}
~~~

方向转换：

~~~text
if underdog_side == RADIANT:
    underdog_direction_score = radiant_score
else:
    underdog_direction_score = -radiant_score

direction =
    supports_underdog  when underdog_direction_score > 0
    opposes_underdog   when underdog_direction_score < 0
    neutral            when underdog_direction_score == 0
~~~

direction 是分类证据。5.5 不等于 55.5%，也不代表比 2.0 应下更大 stake。

## 10. 持久化

使用 append-only terminal records。建议迁移增加：

~~~sql
CREATE TABLE rosh_analysis_runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
    mode TEXT NOT NULL CHECK (mode IN ('historical_match', 'explicit_draft')),
    match_id INTEGER,
    date_time INTEGER NOT NULL,
    draft_hash TEXT NOT NULL,
    rosh_profile_id TEXT NOT NULL,
    formula_version TEXT NOT NULL,
    request_profile_hash TEXT NOT NULL,
    upstream_bundle_hash TEXT NOT NULL,
    scorer_source_hash TEXT NOT NULL,
    canonical_profile_hash TEXT NOT NULL,
    serialization_version TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    radiant_team_score REAL,
    dire_team_score REAL,
    relative_advantage REAL,
    result_json TEXT,
    response_manifest_json TEXT NOT NULL,
    evidence_hash TEXT NOT NULL UNIQUE,
    error_code TEXT,
    collected_at TEXT NOT NULL,
    CHECK (
        (status = 'succeeded' AND result_json IS NOT NULL AND error_code IS NULL)
        OR
        (status = 'failed' AND result_json IS NULL AND error_code IS NOT NULL)
    )
);

CREATE INDEX idx_rosh_runs_match_profile
ON rosh_analysis_runs(match_id, rosh_profile_id, date_time);

CREATE INDEX idx_rosh_runs_draft_profile
ON rosh_analysis_runs(draft_hash, rosh_profile_id, date_time);

CREATE TABLE rosh_hero_scores (
    run_id TEXT NOT NULL REFERENCES rosh_analysis_runs(run_id),
    team_side TEXT NOT NULL CHECK (team_side IN ('RADIANT', 'DIRE')),
    position_id INTEGER NOT NULL CHECK (position_id BETWEEN 1 AND 5),
    hero_id INTEGER NOT NULL,
    raw_score REAL NOT NULL,
    display_score REAL NOT NULL,
    components_json TEXT NOT NULL,
    PRIMARY KEY (run_id, team_side, position_id)
);

CREATE TABLE rosh_minute_points (
    run_id TEXT NOT NULL REFERENCES rosh_analysis_runs(run_id),
    minute INTEGER NOT NULL CHECK (minute >= 0),
    raw_score REAL NOT NULL,
    display_score REAL NOT NULL,
    radiant_time_delta REAL NOT NULL,
    dire_time_delta REAL NOT NULL,
    synergy_delta REAL NOT NULL,
    source_audit_json TEXT NOT NULL,
    PRIMARY KEY (run_id, minute)
);
~~~

写入要求：

- run、hero rows 和 minute rows 在一个事务中落库；
- 先在内存完成验证，再插入终态记录；
- 不允许 UPDATE 改分数/profile/hash；
- 相同 evidence_hash 幂等返回已有 run；
- 跨过 run creation boundary 后的可归类失败可形成 failed run，但不能带 result 或半套 minute/hero rows；
- schema migration 必须进入现有 database protocol 的 expected contract。

GraphQL artifacts 使用现有 content-addressed raw evidence 目录，manifest 至少保存 operation、request hash、response hash、采集时间和相对路径。请求头只保留 allowlist 中的非敏感诊断字段；Authorization、cookie 和浏览器 session 数据永不落盘。

## 11. 模块拆分

### 11.1 新增模块

| 文件 | 职责 |
|---|---|
| prematch/stratz_official_profile.py | profile dataclass、registry、query documents、request planner、canonical hash |
| prematch/stratz_official_score.py | response normalizer、position/synergy/time 纯函数、profile_round |
| live_betting/rosh_parity.py | run orchestration、artifact manifest、cache、持久化 adapter |
| live_betting/rosh_evidence.py | 当前分钟选择、team-side 转换、RoshDirectionEvidence |

### 11.2 修改模块

| 文件 | 修改 |
|---|---|
| live_betting/stratz_rosh_client.py | 增加 official batch transport；旧 dematus fetch 方法不改义 |
| live_betting/storage.py | migration、append-only run/hero/minute repository |
| web/schemas.py | 新 request/response Pydantic schema |
| web/app.py 或独立 router | 新 rosh-analysis endpoint；旧 endpoint 保持兼容 |
| web/frontend/src/api.ts | 新 API client 和类型 |
| web/frontend/src/components/PrematchWorkspace.tsx | 展示分数、拆分、曲线、profile/data status |
| live_betting/strategy_contract.py | 在独立 registry 注册新 v6 official-Rosh evaluator/policy artifact，不改变已有 v5 registry |
| live_betting/official_rosh_shadow_strategy.py | v6 只产出 direction candidate/rejection；v4/v5 执行路径保持原样 |

### 11.3 核心接口

实现名称可以按现有代码风格微调，但依赖方向和输入输出不能合并回旧 scorer：

~~~python
def build_official_request_plan(
    analysis_input: RoshAnalysisInput,
    profile: RoshParityProfile,
    *,
    request_started_at: datetime,
) -> RoshRequestPlan: ...

def normalize_official_responses(
    plan: RoshRequestPlan,
    responses: Mapping[str, Mapping[str, Any]],
) -> NormalizedRoshInputs: ...

def score_official_rosh(
    inputs: NormalizedRoshInputs,
    profile: RoshParityProfile,
) -> OfficialRoshResult: ...

def execute_rosh_analysis(
    analysis_input: RoshAnalysisInput,
    profile: RoshParityProfile,
    *,
    transport: StratzGraphqlTransport,
    artifacts: RoshArtifactStore,
    repository: RoshRunRepository,
) -> RoshAnalysisRun: ...

def build_rosh_direction_evidence(
    run: RoshAnalysisRun,
    *,
    observation_draft_hash: str,
    game_clock_seconds: int,
    underdog_side: TeamSide,
) -> RoshDirectionEvidence: ...
~~~

约束：

- request planner 是唯一能计算 skip/week variables 的模块；
- normalizer 不依赖数据库、网络、时钟或 UI；
- scorer 是确定性纯函数；
- orchestration 负责 transport/artifact/transaction，不复制公式；
- direction evidence 不调用 STRATZ，也不把 score 变成概率。

### 11.4 明确禁止

- 不在 prematch/stratz_rosh.py 中把 dematus 常量改成官方常量；
- 不让新 scorer import 旧 score_rosh_lineups 后局部修补；
- 不让前端计算队伍分、相对优势、概率或分钟选择；
- 不复用旧 ROSH_FORMULA_VERSION 标识新公式；
- 不在保持 comeback-shadow-v4-controlled-entry 名称不变的情况下改变 predicate。

## 12. API 设计

### 12.1 新建分析

~~~http
POST /api/prematch/rosh-analysis
Content-Type: application/json
~~~

历史模式：

~~~json
{
  "mode": "historical_match",
  "match_id": 8904419709,
  "date_time": 1784485548,
  "bracket_ids": ["IMMORTAL"],
  "rosh_profile_id": "stratz-rosh-web-2026-07-28-v2"
}
~~~

手工/实时阵容模式：

~~~json
{
  "mode": "explicit_draft",
  "date_time": 1784485548,
  "bracket_ids": ["IMMORTAL"],
  "rosh_profile_id": "stratz-rosh-web-2026-07-28-v2",
  "radiant": [
    {"hero_id": 54, "position_id": 1},
    {"hero_id": 120, "position_id": 2},
    {"hero_id": 28, "position_id": 3},
    {"hero_id": 90, "position_id": 4},
    {"hero_id": 123, "position_id": 5}
  ],
  "dire": [
    {"hero_id": 145, "position_id": 1},
    {"hero_id": 74, "position_id": 2},
    {"hero_id": 96, "position_id": 3},
    {"hero_id": 79, "position_id": 4},
    {"hero_id": 87, "position_id": 5}
  ]
}
~~~

成功返回完整 RoshAnalysisRun projection。错误状态：

| HTTP | error_code | 含义 |
|---:|---|---|
| 400 | invalid_request | 英雄、位置、日期或 bracket 非法 |
| 404 | source_match_not_found | STRATZ 无该历史比赛 |
| 409 | source_draft_mismatch | 历史源阵容与调用方已绑定 draft 不同 |
| 409 | profile_drift | registry/query/scorer hash 不匹配 |
| 422 | source_data_incomplete | 完整响应无法生成所需分数 |
| 429 | upstream_rate_limited | 重试后仍被限流 |
| 503 | upstream_unavailable | token、网络、5xx 或 GraphQL 故障 |

对外 detail 必须是结构化 error_code + 可脱敏 message，不返回上游响应原文。

### 12.2 读取已有分析

~~~http
GET /api/prematch/rosh-analysis/{run_id}
~~~

只读，不触发在线刷新。需要新数据必须 POST 产生新的 immutable run。

### 12.3 旧接口

POST /api/prematch-predict 暂时保留兼容，继续明确绑定 dematus formula version。新 UI 和新策略不得消费它。

不得在旧响应字段 win_probability 下悄悄放入官方 Rosh score。后续废弃时先增加 deprecation telemetry，再单独移除。

## 13. 前端设计

详情页使用以下语义：

- 主指标：相对优势 +5.8，方向 Radiant；
- 辅助指标：Radiant 队伍分 -4.9、Dire 队伍分 -10.7；
- 英雄表：position、英雄、基础分、同队 synergy、对手 matchup、合计；
- 曲线：纵轴“Rosh 阵容优势分”，0 为中线；
- 数据状态：profile、date_time、rank/all-rank fallback 数量、采集时间；
- 明确不显示 55.8%、63.3% 等伪概率；
- loading、unavailable、profile drift 和 source incomplete 分开显示；
- 后端提供的 display_value 原样展示，不由浏览器重算。

颜色只表达方向：Radiant、Dire、neutral。正负号和文字方向必须同时存在，不能只靠颜色。

## 14. 策略接入

### 14.1 新策略身份

建议初始版本：

~~~text
comeback-shadow-v6-official-rosh-direction
~~~

它是新 executable strategy contract，不是 v4 patch。合同新增：

- rosh_profile_id 和完整 profile identity；
- RoshDirectionEvidence schema/hash；
- 最新已到达 minute bucket 规则；
- underdog side 转换规则；
- direction gate；
- probability/calibration artifact identity；
- Rosh 不参与 stake sizing 的显式 policy 字段。

现有 `comeback-shadow-v5-executable-contract` 已经存在并绑定旧 evaluator，因此不得复用或静默改义；官方 Rosh 方向策略使用上述 v6 身份和独立 executable contract registry。

### 14.2 v6 Rosh gate

按顺序 fail closed：

1. analysis run succeeded；
2. profile identity 与策略合同一致；
3. analysis draft_hash 与 live observation draft_hash 一致；
4. team side 已确认；
5. 当前时间已有不晚于当前分钟的 minute point；
6. direction evidence 可重算且 hash 匹配；
7. underdog_direction_score > 0。

新增 rejection reasons：

~~~text
rosh_profile_mismatch
rosh_analysis_unavailable
rosh_lineup_draft_mismatch
rosh_minute_score_unavailable
rosh_direction_neutral
rosh_direction_opposes_underdog
rosh_evidence_hash_mismatch
~~~

reason precedence 必须写进 evaluator artifact。

### 14.3 概率、edge 与注额

v6 中：

~~~text
Rosh score -> direction gate only
Rosh score -X-> probability
Rosh magnitude -X-> stake multiplier
rank fallback ratio -X-> confidence probability
~~~

策略需要的 model_probability 必须来自独立、版本化 calibration artifact。若当前其他模型仍可提供合法概率，artifact 必须证明其 feature lineage 中没有 (50 + rosh_score) / 100。若没有合法 artifact：

- 仍记录 v6 shadow candidate 和 Rosh direction；
- calibrated_probability = null；
- reason = calibrated_probability_unavailable；
- 不创建 paper order，不声明 edge。

将来训练出合法 calibration artifact 会改变 eligible 集合，必须创建新的策略版本，不能原地修改 v6。

stake 在本阶段固定为现有 paper policy 允许的非 Rosh 基准，或因 calibration unavailable 为 0。不得继续使用当前 match_percentage 或 Rosh 分数决定 stake_multiplier。

### 14.4 仍需共同考量的策略输入

Rosh 只覆盖阵容方向，金如意/逆转型 paper 策略仍需独立检查：

| 类别 | 必需事实 |
|---|---|
| 身份 | strict event、series/map、队伍 side、draft hash 不冲突 |
| 比赛状态 | 已开赛、时钟可信、未暂停、局面数据新鲜 |
| 局面 | underdog directed 的经济差、击杀差、塔/肉山等可控性 |
| 市场 | direct odds、市场状态、更新时间、连续两快照稳定、overround/价格边界 |
| 队伍/选手 | cutoff 前数据、来源时间、覆盖率、避免赛后泄漏 |
| 模型 | calibration artifact、feature/model version、out-of-sample 指标 |
| 执行模拟 | first processed direct successor、slippage、拒单和 fill lineage |
| 风险 | 每 map 一次、paper stake cap、event exposure、drawdown |

这些 predicate 的任何变化同样需要新 strategy version 和 policy hash。

### 14.5 v4/v5 兼容

- v4/v5 历史 decision 继续用各自原 evaluator、原 Rosh artifact 和原错误映射重放，以保持证据可复现；
- 报表必须标注旧 dematus/pseudo-probability 语义；
- 不用新官方结果重写 v4/v5 决策或 M3 cohort；
- v6 在无独立 calibration artifact 时只记录 `shadow_candidate` 或 rejection，`calibrated_probability`、`edge`、`stake_multiplier` 和 `paper_order` 均为 null；
- v6 的 M3-C candidate 记录与 M3-E execution 记录保持分离，当前不会创建 M3-E 记录；
- v4、v5 与 v6 cohort 必须分开，禁止 pooling。

### 14.6 v6 运行记录

- shadow worker 只读取 `collected_at` 不晚于当前 direct transport、draft hash 与 active profile 均匹配的 succeeded Rosh run；
- 每次 v6 评估以 candidate hash、Raybet match/map、transport、decision time、draft hash、source run 和 strategy version 组成不可变 `evaluation_key`；
- candidate/rejection 通过 additive schema v12 追加写入独立的 `official_rosh_shadow_evaluations`，不得塞入要求 probability/edge 非空的旧 `strategy_decisions`；
- v6 表和报表强制 `calibrated_probability`、`edge`、`stake_multiplier`、`paper_order` 与 M3-E cohort 为 null；
- 重放同一 evaluation identity 必须幂等，任何同 key 不同内容、确认 draft 冲突或 contract drift 均失败关闭。

## 15. 失败关闭与降级

| 场景 | 行为 |
|---|---|
| STRATZ token 缺失 | 分析 failed；策略 rejection |
| GraphQL 部分成功 | 整次 run failed，不用旧 cache 拼接 |
| pair 行缺失但 operation 完整 | 按官方规则 pair=0 |
| rank exact bucket matchCount <1000 | 使用对应 all-rank 相邻行 window，并记录 source |
| rank/all-rank 都缺失 | 对应 minute unavailable |
| 当前分钟 <首个 bucket | 等待，不使用未来数据 |
| draft/side 冲突 | sticky mismatch，人工核对 |
| query/query_sha256/request_hash/variables/order/index 任一漂移 | profile_drift，normalize/score 前 fail closed |
| v1 请求 normalize/score 或 v2 identity 尚未完成 D3 绑定 | profile_drift，不返回分数 |
| 上游 bundle 改版 | 旧 profile 保留；创建候选新 profile，重新验收 |
| 浏览器/Cloudflare 不可访问 | 不影响已冻结 fixture 重放；不能用人工验证 cookie 作服务凭据 |
| API 动态数据变化 | 新 run 新 hash，不覆盖旧 run |

只有 profile 明确定义的 fallback 可以降级。其余“尽量出一个数”的行为全部禁止。

## 16. 可观测性

指标：

~~~text
rosh_analysis_runs_total{profile,status,error_code}
rosh_graphql_requests_total{operation,status}
rosh_graphql_latency_seconds{operation}
rosh_profile_drift_total{profile}
rosh_time_fallback_slots_total{profile,source}
rosh_direction_evidence_total{profile,direction}
rosh_strategy_rejections_total{strategy_version,reason}
~~~

结构化日志只记录：

- run_id/request_hash/evidence_hash 的短前缀；
- profile ID、operation、HTTP/GraphQL 分类；
- latency、retry count、cache hit；
- match_id/draft_hash；
- 不记录 token、cookie、完整 Authorization header。

告警：

- profile drift > 0 立即告警并阻止新 v6 eligibility；
- 连续 upstream auth failure 告警，但不得回显 token；
- 黄金 parity CI 任何一项变化阻止合并；
- all-rank fallback 比例突变只触发数据质量告警，不自动改策略阈值。

## 17. 测试设计

### 17.1 Profile/请求测试

文件：tests/test_stratz_official_profile.py

- v1 identity 原值可重算、保持 frozen/unactivated，并被 runtime 拒绝；
- D3 后 v2 的 scorer_source_hash 和 canonical_profile_hash 可从实际工件重算；
- query、query_sha256、request_hash、variables、operation 顺序或显式 index 任一变化均 fail closed；
- null 与 omitted、数组顺序进入 hash；
- date_time replay 不依赖 wall clock；
- UTC 日界线边界；
- 非 IMMORTAL profile fail closed；
- ROGUE side 和第 11 个 slot 在 request planning/normalize 前拒绝；
- Authorization 不进入 artifact/hash。

### 17.2 Scorer 单元测试

文件：tests/test_stratz_official_score.py

- position 样本 0/1/999/1000/1001，以及 crossing day row 整行纳入后停止且不汇总全部七日；
- synergy 0/1/99/100/101 场，crossing week 整行纳入且累计 count 可大于 100；
- `[(10,99),(-10,100)]` 精确得到 pair `-0.05`、count `199`；
- 每周合并后的 progressive `Math.round(100*x)/100`、最终不足 100 的收缩舍入和 negative zero；
- with 和 vs 分开；
- 缺失 pair 为 0、缺失 operation 为 error；
- exact bucket rank 999 回退、1000 不回退；
- minute 20/22 缺号时仍按相邻 rows 进入各自 `slice(i-1,i+2)` window；
- 交换 Radiant/Dire 后 relative 和 minute score 反号；
- 输入顺序改变但 position identity 相同，结果不变；
- NaN/Infinity/负 matchCount 拒绝；
- profile_round 正负边界。

### 17.3 黄金 parity 测试

fixture 目录：

~~~text
tests/fixtures/stratz_official_rosh/8904419709/
  manifest.json
  requests.json
  responses.sanitized.json
  expected.json
~~~

断言：

- 十个英雄分逐项等于第 8 节；
- team scores 为 -4.9 / -10.7；
- relative 为 +5.8；
- 指定七个 minute points 逐项相等；
- result/evidence hash 稳定；
- scorer 输出不等于 legacy +13.3；
- fixture 中不存在 Authorization、Bearer、cookie；
- 所有黄金 hash 不变不能替代第 17.1/17.2 节的非黄金反例和 fail-closed tests。

### 17.4 Repository/API 测试

文件：

~~~text
tests/test_rosh_parity_storage.py
tests/test_web_rosh_analysis.py
~~~

覆盖事务原子性、幂等、append-only、失败 run、结构化错误、历史阵容 mismatch、读取不刷新和 secret redaction。

### 17.5 策略测试

文件：

~~~text
tests/test_rosh_direction_evidence.py
tests/test_rosh_direction_evidence.py
~~~

必须断言：

- 36:59 选择 minute 36，不选择 37；
- Dire underdog 对 Radiant -5.5 得到 +5.5 supports；
- score=0 为 neutral rejection；
- draft/profile/evidence hash mismatch fail closed；
- v1 analysis/profile 在 runtime 被拒绝；
- 只有完成 D3/D4 的 v2 可 active-for-scoring；
- v6 代码路径不存在 (50 + score) / 100；
- v6 不从 Rosh magnitude 或 match_percentage 产生 stake；
- calibration unavailable 不创建 paper order；
- v4 golden replay bit-for-bit 不变；
- v4/v5/v6 strategy contract identity 和 cohort 分开。

### 17.6 集成与 UI

- 使用 fake GraphQL transport 完成 API -> DB -> UI；
- 页面显示 +5.8，不显示 55.8%；
- 1440px 和 320px 无重叠，曲线 0 线与方向正确；
- loading/error/profile drift 状态可见；
- 可选在线 STRATZ smoke test 只报告 drift，不更新 fixture；
- 不要求通过 Cloudflare 真人验证完成自动化测试。

## 18. 开发阶段

本次交付链中 D1 是本文合同修正，D2 对应 P1 的 scorer 实现；D3 绑定实际 identity，D4 才允许 v2 active-for-scoring。

### P0：冻结 profile 和合同

交付：

- 保持第 8 节脱敏 request/response/bundle/manifest/expected 工件原字节不变；
- 冻结 v1 identity 为 unactivated、superseded-for-implementation；
- 记录 v2 profile/formula ID 和修正后的公式/请求合同；
- 明确 D2 -> D3 -> D4 的绑定和激活顺序。

退出条件：

- v1 profile/formula/request hash 和全部 fixture hash 均未改变；
- v1 runtime normalize/score 合同为 fail closed；
- v2 在实际 hash 尚未由 D3 绑定时保持 candidate 且不可评分；
- fixture 可作为上游证据离线重放；
- secret scan 通过；
- 文档合同包含 crossing-row、相邻-row 和逐周舍入的非黄金反例。

### P1：实现官方纯函数 scorer

交付：

- normalizer；
- position/synergy/time 计算；
- `js_round_2` 和 profile_round；
- 十英雄和分钟 component audit。

退出条件：

- 黄金样例全部逐项相等，但不以黄金 hash 不变代替非黄金测试；
- crossing day/week rows、`[(10,99),(-10,100)] -> (-0.05,199)`、progressive per-week rounding、negative zero、minutes 20/22 相邻 window、exact bucket 999/1000 tests 全部通过；
- ROGUE/第 11 slot、身份漂移、边界/对称/非有限输入测试通过；
- 不 import 旧 dematus scorer。

#### D3：v2 identity 绑定退出要求

D3 只能在 D2 scorer 修正完成后执行：

- 从实际 scorer 源工件计算并绑定 scorer_source_hash，不接受手填、预测或临时值；
- 从完整 v2 identity canonical projection 计算并绑定新的 canonical_profile_hash；
- v2 registry 中不存在 placeholder，两个 hash 均可独立重算；
- v1 的 ID、formula_version、request_profile_hash 和既有 hash 逐项保持不变；
- 第 8 节 manifest/request/response/expected/page-assets/bundle SHA-256 逐项保持不变。

#### D4：v2 active-for-scoring 退出要求

- v1 normalize/score 必须拒绝；
- 只有 D3 identity 完整匹配的 v2 才可 active-for-scoring；
- query、query_sha256、request_hash、variables、order、index 任一漂移都在评分前 fail closed；
- ROGUE/第 11 slot 和第 17.2 节全部非黄金反例通过；
- 黄金结果与冻结 hash 保持不变，但该结果不能豁免上述反例；
- active-for-scoring 只授权有符号 Rosh 评分，不授权 strategy activation、stake 逻辑或真实下注。

### P2：运行、持久化、API 和 UI

交付：

- official transport orchestration；
- immutable artifacts/run tables；
- 新 API；
- PrematchWorkspace 切换到 score 语义。

退出条件：

- fake transport E2E 通过；
- +5.8 正确展示且无伪概率；
- secret/redaction/append-only 测试通过；
- 旧 endpoint 兼容测试通过。

### P3：v6 official-Rosh shadow 策略

交付：

- RoshDirectionEvidence；
- v6 evaluator/policy artifact；
- reason precedence；
- v4/v5/v6 分离报表。

退出条件：

- v4/v5 replay 不变，v6 只消费完成 D3/D4 的 v2 evidence；
- 无合法 calibration 时只记录 candidate/rejection；
- 有合法非 Rosh 伪概率 artifact 时才能创建 paper order；
- 所有订单仍为 paper order。

### P4：前向校准与评审

交付：

- 冻结 v6 prospective M3-C cohort；
- outcome label coverage；
- calibration candidate；
- 独立 M3-E paper execution cohort；
- promotion report。

退出条件：

- 严格遵守 M3-C/M3-E 分离；
- 历史重建样本不计为 prospective；
- 通过已有 ADR gate 后只提出新策略版本；
- 不自动部署、不扩大 stake。

### P5：上游漂移监控

交付：

- bundle/query profile 定期观察；
- drift report；
- 新 profile 建立流程。

退出条件：

- drift 不会自动改旧 profile；
- 浏览器观察不成为 production decision authority；
- 新 profile 必须重复 P0-P3 验收。

## 19. 风险与控制

| 风险 | 控制 |
|---|---|
| STRATZ 更新前端算法 | profile + bundle hash；新版本重新黄金验收 |
| GraphQL 动态统计导致同 match 新结果不同 | date_time/request/response 全量 identity；旧 run 不覆盖 |
| 舍入或聚合差异被黄金样例掩盖 | Synergy progressive `js_round_2` + 输出 profile_round + 非黄金反例 |
| 位置解析错误 | 两队 1..5 完整性和 draft hash |
| 小样本被误解为概率置信度 | source audit 只做质量信息，不用于 stake |
| Rosh 分数继续伪装为胜率 | 新 API schema 无 win_probability；v6 静态/行为测试 |
| v4 历史被重写 | 独立 scorer、独立 formula/profile/strategy version |
| token/cookie 泄漏 | env-only、headers allowlist、fixture secret scan |
| Cloudflare 验证依赖 | production 只调用授权 GraphQL API |
| 法务/使用条款风险 | 上线前确认 STRATZ API 条款、速率限制、缓存和署名要求 |
| 样本内调参 | preregistered cutoff、prospective cohort、独立 promotion |

## 20. 完成定义

只有同时满足以下条件，才可称“官方 Rosh 复刻开发完成”：

1. v2 profile/query/bundle/scorer 均有不可变且可重算的实际 hash；
2. match 8904419709 的全部黄金值逐项一致，且六个冻结 P0/raw 工件 hash 不变；
3. crossing row、相邻 row、progressive rounding、negative zero、身份/请求漂移、ROGUE/第 11 slot、fallback、对称和 secret tests 通过；
4. API/UI 不再把 Rosh score 展示为概率；
5. 数据库可从 artifact + profile 离线重放相同 evidence_hash；
6. Rosh parity v1 保持 frozen/unactivated 且被 runtime 拒绝；v4 历史 replay 无变化；
7. v6 使用 Rosh direction evidence，不使用线性伪概率或 Rosh stake sizing；
8. v6 与旧 v4/v5 cohort 完全隔离；
9. 不存在真实下注调用路径；
10. 上游 drift 时能够 fail closed，而不是静默产生不同语义的结果。

本设计完成的是一个可复现的评分和 shadow/paper 决策证据系统。任何“实盘”“提高注额”或“自动跟投”都属于新的治理和安全范围，不由本设计授权。
