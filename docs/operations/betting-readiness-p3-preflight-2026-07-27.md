# P3 候选准备与 Preflight — 2026-07-27

## 当前状态

- P2 dependency：`go_m1_only`
- P3 preparation：`GO`
- 已登记候选：`0`
- Candidate monitoring clock：`not_started`
- P3 exit：`not_achieved`
- P4 live canary：`NO-GO`
- Target mode：`M1-only`

监测时钟没有被人为回填。按照 ADR-0013，只有 mapping、draft、Rosh、model/provider 上游依赖 ready 且 `STANDARD_DOTA_HUD` runtime capability healthy 的首次记录，才可以启动 14 天时钟。

## 已冻结的静态身份

- Current commit：`2237d5f120ded13eb4e393a0c6a4251b096085df`
- Current tree：`84ecdfa62c5e465860495c231623d7ccef939619`
- Strategy version：`comeback-shadow-v5-executable-contract`
- Evaluator version：`comeback-shadow-canonical-evaluator-v2`
- Evaluator hash：`c2d2f741e3b172b1fda1ca161619961e597070388d46d97848391b3f2f91ad24`
- Policy hash：`6e0c8a278378ee4c070f5d11204ca23397f54c7b6b703b544adaf105a259d696`
- Serialization：`rfc8785-jcs-v1`
- Preferred layout：`STANDARD_DOTA_HUD`

这些只是静态仓库身份，不代表某场直播已经具备 draft/model/Vision/runtime readiness。

## 候选必须填写的事实

候选登记必须包含并通过离线 validator：

1. 人工批准的 Tier-1 正赛和 strict event registry 覆盖；
2. exact RayBet match/team/map identity，且 `/match`、`/odds` 身份可用；
3. frozen 64-hex draft deployment key，可加载且 lineage 完整；
4. Rosh lineup score、team/player profile 和 model refs 可用；
5. exact match ID 的 fresh HLS refresh；signed URL 不得持久化；
6. `STANDARD_DOTA_HUD` runtime healthy，并绑定正向 live、replay/highlights negative 和固定真实 frame 的 SHA-256 evidence；
7. strategy/evaluator/policy identity 与本包冻结值完全一致；
8. paper-only、browser companion 未配置、single managed writer 和 database/raw pair 计划已冻结；
9. 运维明确批准该候选；P4 approval 必须仍为 false。

## M2 边界

当前 `m2_readiness=blocked`，所以候选只能使用 `target_mode=m1_only`。如果填写 `target_mode=m1_and_m2`，validator 必须 fail closed，直到新的 P2.5 confirmed witness 和独立 closeout 更新将 M2 置为 ready。

## Layout 扩展边界

当前：

- layout-only missed windows：0；
- 14 天监测时钟：未启动；
- active layout expansion：无；
- expansion allowed：否。

不能因“目前没有候选”立即开发新 overlay。只有两个预先登记且 layout 为唯一 blocker 的 Tier-1 missed windows，或有效监测时钟连续 14 天无候选，才可单独批准一个最高频 overlay。

## 使用方法

复制 `templates/P3_CANDIDATE_TEMPLATE.json`，填入一场候选的脱敏事实，然后运行：

```powershell
python tools/p3_candidate_preflight.py `
  --candidate .\candidate.json `
  --registry .\P3_CANDIDATE_REGISTRY.json `
  --output .\p3-candidate-preflight-result.json
```

该工具只读取 JSON，不访问网络、不连接数据库、不启动服务。返回 `ready_for_p3_exit_review` 也只代表可以进入 P3 exit 人工评审，绝不等于 P4 GO。

## 2026-07-27 首个监测窗口更新

`The International 2026` 已登记为 `registered_watch_window_not_candidate`：

- 完整赛事窗口：2026-08-13 至 2026-08-23；
- Tier-1 公开证据：奖金池 1,600,000 美元、16 支队伍；
- exact series/map schedule：未确认；
- strict event/OpenDota league ID：未确认；
- RayBet match/odds identity：未验证；
- broadcast/HLS：未公布或未验证；
- candidate readiness：`blocked`；
- monitoring clock：仍为 `not_started`。

使用以下命令验证窗口登记完整性：

```powershell
python tools/p3_window_preflight.py `
  --window docs/operations/betting-readiness-p3-ti2026-window-2026-07-27.json `
  --registry docs/operations/betting-readiness-p3-candidate-registry-2026-07-27.json `
  --output docs/operations/betting-readiness-p3-ti2026-window-preflight-result-2026-07-27.json
```

返回 `valid_registered_watch_window` 只授权继续监测；它不授权 P3 exit、P4 canary 或生产变更。
