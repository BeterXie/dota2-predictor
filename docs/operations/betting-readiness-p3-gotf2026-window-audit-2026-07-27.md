# P3 首要监测窗口审计 — Games of the Future 2026

生成时间：`2026-07-27T01:50:23Z` / `2026-07-27T10:50:23+09:00`

## 决定

将 `Games of the Future 2026` 提升为 P3 的首要受控监测窗口，观察起点为 2026 年 7 月 31 日。`The International 2026` 保留为后备窗口。

本次只改变监测优先级，不登记 P3 candidate，不启动 ADR-0013 的 14 天监测时钟，也不授权 P4 live canary。

当前 gate：

- Window registration：`VALID`
- Priority：`PRIMARY`
- Candidate readiness：`BLOCKED`
- Monitoring clock：`NOT STARTED`
- P3 exit review：`NO-GO`
- P4 live canary：`NO-GO`
- Production deployment/database mutation：`NO-GO`

## 已核实的事件事实

1. Games of the Future 官方 Dota 2 公告给出的赛事窗口为 2026 年 7 月 31 日至 8 月 5 日，地点为哈萨克斯坦阿斯塔纳，奖金池为 100 万美元。
2. 官方抽签公告写明 MOBA PC 的主要赛段为 8 月 2–5 日，并同时说明两项 MOBA 的小组赛会在转入 Ushkempirov Martial Arts Palace 前，于 The Hood 封闭场地提前开始。
3. 官方观赛公告确认 Phygital+ 将提供每场比赛的直播与点播。
4. GosuGamers 在审计时列出了 7 月 31 日的若干 Dota 2 小组赛，包括 Virtus.pro–Execration、Rune Eaters–Midas Club、Vici Gaming–Amaru、Team Resilience–PlayTime 和 Enjoy–GLYPH。

这组证据支持把 7 月 31 日作为“提前观察窗口”，但官方页面之间对 7 月 31 日和 8 月 2 日的描述尚未完成逐场时间对账；第三方页面显示的时区也未确认。因此不能把这些时间写成 authoritative UTC schedule。

## 为什么还不是候选

P3 candidate 必须绑定 exact series/map。当前仍缺少：

1. 官方结果源对 7 月 31 日小组赛时间和时区的最终对账；
2. strict event registry 的 event ID 和经人工审计的 OpenDota league ID；
3. RayBet `/match` 与 `/odds` 的 exact match/team/map identity；
4. map number、UTC 开赛时间及 exact-match HLS；
5. frozen draft deployment、Rosh lineup、team/player profiles 和 model refs；
6. `STANDARD_DOTA_HUD` 对本届赛事直播的真实运行时证据；
7. single managed writer、database/raw pair 和运维候选批准。

## 7 月 31 日执行边界

7 月 31 日可以进行：

- 只读检查官方结果页、RayBet direct feed 和 Phygital+ 的公开元数据；
- 记录 exact series/team/map 候选映射；
- 获取不含持久化签名参数的 layout/HLS 证据；
- 离线运行 fail-closed preflight。

不得进行：

- 启动 live canary 或真实下注；
- 连接或修改生产数据库；
- 启动 browser companion；
- 将第三方显示时间直接当作 UTC；
- 在 RayBet、OpenDota 和直播 identity 未对齐前批准 P3 candidate；
- 提前启动 14 天 layout monitoring clock。

## 下一次复核触发条件

- 官方结果源可读并确认 7 月 31 日逐场时间及时区；
- RayBet direct feed 出现对应 series 和 odds identity；
- OpenDota league/event identity 可唯一审计；
- Phygital+ 或授权转播源提供 exact-match stream/HLS identity；
- frozen draft、模型输入和 `STANDARD_DOTA_HUD` runtime evidence ready。
