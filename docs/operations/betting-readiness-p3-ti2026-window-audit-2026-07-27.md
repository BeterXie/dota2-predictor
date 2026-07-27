# P3 首个 Tier-1 监测窗口审计 — The International 2026

生成时间：`2026-07-27T01:38:05Z` / `2026-07-27T10:38:05+09:00`

## 决定

将 `The International 2026` 登记为 P3 的首个受控监测窗口，但不登记为 P3 candidate，也不启动 ADR-0013 的 14 天监测时钟。

当前 gate：

- Window registration：`VALID`
- Candidate readiness：`BLOCKED`
- Monitoring clock：`NOT STARTED`
- P3 exit review：`NO-GO`
- P4 live canary：`NO-GO`
- Production deployment/database mutation：`NO-GO`

## 已核实的公开事件事实

- Valve 的官方赛事公告确认 The International 2026 在上海举办，并说明争夺 Aegis 的现场阶段为 8 月 20–23 日。
- BLAST 赛事页列出的完整赛事窗口为 2026 年 8 月 13–23 日，奖金池为 1,600,000 美元，参赛队伍为 16 支。
- BLAST 页面在本次审计时仍标明直播频道尚未公布。

这些事实足以登记“值得继续监测的 Tier-1 窗口”，但不足以批准 strict event seed 或某一场具体 map。

## 为什么还不是候选

P3 candidate 必须绑定 exact series/map。当前无法确认以下事实：

1. strict event registry 的正式 event ID 和 OpenDota league ID；
2. 逐场 series、map number 和 UTC 开赛时间；
3. RayBet `/match` 与 `/odds` 的 exact match/team/map identity；
4. exact match HLS、broadcast channel 和运行时 layout；
5. frozen draft deployment key、Rosh lineup、team/player profiles 和 model refs；
6. single managed writer、database/raw pair 和运维候选批准。

本执行环境对 RayBet 域名的 DNS 解析不可用，因此没有把“无法探测”错误解释为“provider 没有比赛”。窗口文件明确记录为 `environment_dns_unavailable_no_provider_claim`。

## Layout 证据边界

仓库中已有 `STANDARD_DOTA_HUD` 的固定正向 live crop、highlights negative crop、Vision 测试和 layout 源码哈希；这些只证明静态仓库资产存在，不证明 TI 2026 的直播 overlay 与其兼容，也不证明 runtime capability healthy。

因此：

- 不填写候选模板中的 runtime evidence SHA；
- 不启动 14 天 layout monitoring clock；
- 不增加新 overlay；
- 不记录 layout-only missed window。

## 下一次复核触发条件

任何一项触发后都应重新执行窗口审计；只有所有 P3.1 依赖同时满足，才复制正式 candidate 模板：

- 官方发布 exact series/map schedule；
- strict event audit 确认 OpenDota league ID；
- RayBet direct feed 出现 exact match 和 odds identity；
- broadcast/HLS 可按 exact match ID 刷新；
- frozen draft 与 Rosh/team/player/model 输入 ready；
- `STANDARD_DOTA_HUD` 对该直播的正向、负向和固定真实帧 runtime evidence ready。

## 交付文件

- `betting-readiness-p3-ti2026-window-2026-07-27.json`
- `betting-readiness-p3-ti2026-window-preflight-result-2026-07-27.json`
- `betting-readiness-p3-candidate-registry-2026-07-27.json`
- `tools/p3_window_preflight.py`
- `tests/test_p3_window_preflight.py`
