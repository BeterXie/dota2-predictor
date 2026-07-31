# Games of the Future 2026 现场链路修复总结

日期：2026-07-31
仓库：`BeterXie/dota2-predictor`
现场样本：RayBet `38416120`（Team Resilience vs PlayTime）

## 结论

本轮已恢复该赛事的 Vision 采集、跨地图状态恢复、Draft 独立证据累计和 Team Side 识别，并将 Games of the Future 2026 纳入正式赛事 authority。Draft publisher 已完成历史依赖审计重绑定和语料哈希性能优化，模型及生产输入均未发生漂移。

当前关键安全结果：

```text
Draft 已锁定槽位：7
正确锁定槽位：7
错误锁定槽位：0
draft_ready：false（未满 10 个正确槽位，继续 fail closed）
```

## 现场问题与修复

### 1. 新转播 HUD 无法识别

Games of the Future 2026 使用了此前未支持的 WXC HUD。新增布局识别与坐标定义后，Vision 可以进入实际 HUD 读取链路，而不是停在未知布局。

相关提交：

```text
07470fc feat(vision): support WXC GOTF 2026 live HUD
```

### 2. `capturing_partial` 证据没有持续刷新

watcher 现在会周期性刷新部分采集证据，使 Heartbeat 和监控页面能够反映仍在工作的采集链路，避免旧证据看起来像采集已经停止。

相关提交：

```text
93e8efd fix(vision): refresh periodic partial capture evidence
```

### 3. watcher 重启后地图编号回退

RayBet 的 stale `currentIndex=1` 曾覆盖数据库中已经确认的 map 进度。现在 watcher 会从 PostgreSQL 恢复 map 和时钟状态，并保护已确认的跨地图 rollover：

- settled map 不会被旧的 manual map 覆盖；
- watcher 重启后继续使用数据库中的当前 map/clock；
- 系列赛结束后不会回退到过时的地图编号；
- map 变化仍会重置本地图的 Draft 槽位证据。

第二局被误标的数据没有原地改写，而是从以下时间开始 append-only invalidation：

```text
2026-07-31T11:02:47.566395+00:00
失效 observation：88
其中 confirmed：0
```

相关提交：

```text
d9802e1 fix(vision): preserve map rollover across watcher restarts
```

### 4. Draft 和 Team Side 长期无法 ready

Draft 独立证据现在要求同时满足：

```text
足够时间间隔
+ 不同源帧
+ 游戏时钟推进
```

静态英雄头像 crop 的近似 pHash 不再永久阻止有效证据累计。Replay 或切镜只暂停累计，不会清空同一地图已经获得的证据；地图切换仍强制 reset。

真实第二局 78 帧复验：

```text
best_candidate_accuracy = 82.69%
accepted_precision = 95.98%
final_locked_slots = 7
final_correct_locked_slots = 7
wrong_lock_count = 0
draft_ready = false
```

Team Side Logo 下载增加了内容校验：

- 拒绝 `text/html` 等伪图片响应；
- Steam CDN 的 `application/octet-stream` 只有在 OpenCV 成功解码时才接受；
- Heartbeat 增加 `team_side.recognizer_status`；
- Team Resilience 和 PlayTime 的 canonical Logo 已写入 PostgreSQL。

相关提交：

```text
11697f2 fix(vision): restore draft and team-side readiness
```

### 5. 赛事未进入正式 authority

Games of the Future 2026 已加入正式赛事注册表：

```text
event_id = games-of-the-future-2026
leagueid = 19917
Dota 2 日期 = 2026-07-31 ～ 2026-08-05
奖金 = US$1M
```

官方依据：<https://gofuture.games/news/item/dota-2-returns-for-gotf-2026-with-1m-prize-pool/>

OpenDota 真值和 strict mapping：

```text
Team Resilience = 10207984
PlayTime = 10207983

map 1 = OpenDota 8922133239，mapping_id 13
map 2 = OpenDota 8922211678，mapping_id 14
```

相关提交：

```text
24518aa feat(events): approve Games of the Future 2026
```

## Draft publisher 审计重绑定

旧 Draft deployment 绑定历史依赖 revision `1445`。数据库历史推进到 revision `3271` 后，publisher 按 fail-closed 规则不会直接复用旧 deployment。

本轮完成 audited rebase：

1. 逐字节验证五个模型文件没有变化；
2. 对 572 张生产正式地图比较旧、新 `source_input_hash`；
3. 确认 `mismatch_count = 0`；
4. 生成绑定 revision `3271` 的新 deployment key。

```text
旧 deployment key:
baadee56757f3813ae7670aca2537297d0e5e60f19fdae3c3a587f43030d991e

新 deployment key:
b9f715c14af7840c9a3e8468ba1b3c1311f74cf88476ced7024c508772b1cba6
```

重启后确认：

```text
draft_publisher_worker = healthy
history_dependency_revision = 3271
last_error = null
```

该操作没有重新训练或修改模型；它证明了新历史 revision 下的模型字节和实际输入均无漂移，因此允许已审核模型继续运行。如果任一模型字节或输入哈希不一致，就不能执行重绑定。

## Draft corpus 性能优化

`load_draft_corpus` 原先逐地图重复计算比赛级输入哈希，生产数据上耗时超过 5 分钟。现在按 match 分组计算并复用结果，耗时降至约 7.6 秒。

优化前后对 572 张正式地图的 `source_input_hash` 完全一致：

```text
mismatch_count = 0
```

相关提交：

```text
58281f1 perf(draft): group corpus hashes by match
```

## 验证结果

```text
Vision + evaluation：93 passed
Map focused tests：5 passed
PostgreSQL event authority integration：passed
Ruff：passed
compileall：passed
git diff --check：passed
```

旧 SQLite event/stream 测试仍属于历史测试债务。生产实现保持 PostgreSQL-only，不应为了旧测试恢复 SQLite 兼容。

## 尚未完成：赔率快速通道

赔率监控仍存在独立性能问题：`collect_once()` 串行请求所有 live matches，一轮约需 110～168 秒。即使配置 `--interval=3`，下一轮仍需等待全量请求完成，因此 Shadow 的 15 秒 transport freshness 大部分时间无法满足。

后续应增加 strict-mapped live match 的优先赔率刷新通道，同时保留较慢的全量审计：

```text
priority odds interval：约 8 秒
full odds sweep interval：约 120 秒
MAX_ODDS_TRANSPORT_AGE：继续保持 15 秒，不放宽
```

该项不包含在上述提交中，需要在下一场 active 且 strict-mapped 的比赛上完成端到端验证。

## 本轮提交

```text
07470fc feat(vision): support WXC GOTF 2026 live HUD
93e8efd fix(vision): refresh periodic partial capture evidence
d9802e1 fix(vision): preserve map rollover across watcher restarts
11697f2 fix(vision): restore draft and team-side readiness
24518aa feat(events): approve Games of the Future 2026
58281f1 perf(draft): group corpus hashes by match
```
