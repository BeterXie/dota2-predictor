# Dota 2 滚球监控台操作手册

本文档适用于项目内置的本机运维控制台。它用于查看 RayBet 赛事、赔率历史、可信视觉时钟、纸面策略、进程状态、exact 映射和告警。系统不会提交真实投注。

## 1. 安全边界

- Web 服务默认只监听 `127.0.0.1`，不要改成 `0.0.0.0` 暴露到局域网或公网。
- RayBet 采集是只读的；策略只生成纸面订单。
- 页面不能提交任意命令，只能控制四个后端固定白名单进程。
- 停止进程前会核对 PID、进程创建时间和命令哈希。身份不一致时会拒绝停止。
- 页面不能编辑原始赔率、实时调参、执行真实投注或接受 fuzzy/name-only 映射。
- exact 映射、失效、替代关系、进程控制和告警确认都有数据库审计记录。

## 2. 环境要求

推荐环境：

- Windows 10/11
- Python 3.10
- Node.js 18 或更高版本
- npm 9 或更高版本
- Edge 或 Chrome

本机当前可用的 Python 3.10 路径：

```powershell
$python = "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe"
& $python --version
```

不要使用缺少项目依赖的 Python 3.11 环境。

## 3. 首次安装

在 PowerShell 中进入项目目录：

```powershell
Set-Location C:\Users\59908\dota2-predictor
$python = "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe"
& $python -m pip install -r requirements.txt
```

安装并构建前端：

```powershell
Set-Location web\frontend
npm ci
npm run build
Set-Location ..\..
```

前端构建结果写入 `web/frontend/dist/`。生产页面由 FastAPI 直接提供，不需要单独运行 Vite。

## 4. 启动与访问

前台启动，适合查看日志：

```powershell
Set-Location C:\Users\59908\dota2-predictor
$python = "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe"
& $python -m web.main
```

看到以下内容表示启动成功：

```text
Uvicorn running on http://127.0.0.1:8000
```

浏览器访问：

```text
http://127.0.0.1:8000/monitor
```

接口快速检查：

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/monitor
Invoke-RestMethod http://127.0.0.1:8000/api/monitor/bootstrap
```

两项均应返回 HTTP `200`。

## 5. 后台启动 Web 服务

```powershell
Set-Location C:\Users\59908\dota2-predictor
$python = "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe"
$stdout = (Resolve-Path data).Path + "\web-monitor.stdout.log"
$stderr = (Resolve-Path data).Path + "\web-monitor.stderr.log"

$process = Start-Process `
  -FilePath $python `
  -ArgumentList "-m", "web.main" `
  -WorkingDirectory (Get-Location).Path `
  -WindowStyle Hidden `
  -RedirectStandardOutput $stdout `
  -RedirectStandardError $stderr `
  -PassThru

$process.Id
```

当前配置位于 `web/config.yaml`：

```yaml
server:
  host: "127.0.0.1"
  port: 8000
  reload: false
```

## 6. 页面区域

### 6.1 顶栏

- `实时监控`：优先选择滚球中、降级或即将开始的赛事。
- `历史复盘`：优先选择已结束赛事。
- `SSE 实时`：服务端事件流正常。
- `轮询降级`：SSE 中断后自动每 5 秒轮询，不代表数据库不可用。
- `声音`：本地提示音开关，第一次打开会播放短测试音。
- `系统通知`：请求浏览器 Notification 权限。
- 铃铛数字：当前尚未确认的持久化告警数量。

### 6.2 摘要条

- `滚球确认`：生命周期判定为 live 的赛事数量。
- `数据降级`：有赔率但关键证据或工作进程未就绪的赛事数量。
- `即将开始`：尚未进入滚球的赛事数量。
- `异常进程`：按心跳新鲜度重新计算后的异常工作进程数量。

### 6.3 左侧赛事栏

赛事按 `滚球中 / 数据降级 / 即将开始 / 已结束` 分组。搜索框支持：

- RayBet match ID
- 赛事名称
- 任一队伍名称

每一行显示当前胜负盘赔率和最后采集年龄。红色年龄通常表示数据已经过期。

### 6.4 中间工作区

- 标题区：赛事、队伍、RayBet ID、BO 局数和数据年龄。
- 胜负盘：原始赔率与去水后的市场概率。
- 概率图：去水概率为纵轴。
- 策略判断：保留有信号和无信号决策及其原因。
- 证据摘要：赔率快照、视觉观测和画面识别状态。
- 其他盘口：默认折叠，包含总击杀、让分等当前报价。

## 7. 时间轴与历史复盘

单场复盘以真实采集时间为主轴。可信比赛时钟只作为辅助字段：

- 主时间：`received_at / observed_at / captured_at`
- 辅助时间：通过可信视觉观测对齐的 `game_clock_seconds`
- 超过 60 秒的采集空洞显示为断点，不会伪造中间数据。
- 图表只使用数据库中真实存在的完整胜负盘快照。
- 原始赔率保留在点位详情中，图表概率使用完整结果组去水后的值。
- 不可信、手工或缺失的比赛时钟不会替代采集时间。

复盘步骤：

1. 点击顶栏 `历史复盘`。
2. 在左侧选择已结束赛事，也可搜索 match ID。
3. 在概率图右上角选择第 1/2/3 局。
4. 对照曲线点的采集时间、原始赔率和可信比赛时钟。
5. 查看下方策略判断，确认 `eligible`、原因和输入证据。

## 8. 就绪链路

右侧就绪链路逐场显示：

- `赔率采集`：是否有新鲜的完整赔率快照。
- `赛事映射`：是否存在未失效的 strict exact 映射。
- `视觉观测`：是否有新鲜且确认过的视觉帧。
- `模型判断`：是否产生模型/研究判断。
- `纸面策略`：策略工作进程与输入是否就绪。

常见状态：

| 状态 | 含义 |
|---|---|
| `就绪` | 证据存在且在新鲜度窗口内 |
| `延迟` | 有数据，但已经超过警告时间 |
| `过期` | 数据超过允许时间，不可作为当前依据 |
| `无数据` | 尚未收到该类证据 |
| `未确认` | 视觉等证据存在，但可信度门槛未通过 |
| `无效` | 身份、证据或映射已经失效 |
| `异常` | 工作进程报告错误或心跳严重过期 |
| `已停止` | 对应工作进程未运行 |

## 9. 安全进程控制

页面打开时会从本机控制 API 获取短期会话和 CSRF 令牌。右侧只显示四个固定进程：

| 页面名称 | 固定命令 |
|---|---|
| 赔率采集 | `python -u -m live_betting.monitor --database data/dota2.db --raw-dir data/live_betting/raw-v2 --interval 6 --list-interval 30` |
| 纸面策略 | `python -u -m live_betting.shadow_monitor --database data/dota2.db --vision-jsonl data/live_betting/live_observations` |
| 视觉监控 | `python -u scripts/supervise_raybet_streams.py --database data/dota2.db` |
| 邮件投递 | `python -u scripts/run_notification_worker.py --database data/dota2.db` |

按钮说明：

- 三角形：启动。
- 双竖线：停止。
- 圆形箭头：重启。

每次操作都有二次确认。后端不会接收来自页面的命令文本。

控制日志位于：

```text
data/live_betting/logs/managed/<component>.stdout.log
data/live_betting/logs/managed/<component>.stderr.log
```

审计表：

```text
monitor_process_registry
monitor_control_audit
```

若显示 `identity_mismatch`，表示登记的 PID 已被其他命令占用或命令发生变化。页面会拒绝终止该 PID。先检查：

```powershell
Get-CimInstance Win32_Process -Filter "ProcessId=<PID>" |
  Format-List ProcessId,ExecutablePath,CommandLine,CreationDate
```

不要通过修改数据库来绕过身份核对。

## 10. Exact 映射

### 10.1 规则

- `manual_exact` 必须通过 RayBet 队伍 ID、顺序、event、时区、赛程、阶段、BO 和 canonical team 的全部硬校验。
- `automatic_exact` 只能复用已经人工批准的同一证据哈希。
- candidate、fuzzy、队名相似或仅凭名称的结果永远不能进入策略。
- 原映射不可更新或删除。

### 10.2 页面操作

选择有映射的赛事后，`Exact 映射` 区显示每局记录：

1. 展开第 N 局，检查 event、canonical teams 和证据哈希。
2. 对有效的 `manual_exact` 映射点击印章图标，确认该证据可用于后续局自动 exact。
3. 批准后，如果 BO 系列还有缺失局，会出现 `登记第 N 局`。
4. 点击叉号使映射失效，必须输入至少 5 个字符的原因。

失效不会删除旧记录。系统追加：

- `strict_live_map_mapping_invalidations`
- `strict_live_map_mapping_supersessions`
- `strict_live_mapping_impacts`

依赖旧映射的研究判断、策略判断和纸面订单会被标记受影响，但原始记录保留。

### 10.3 CLI 人工登记

准备结构化 evidence JSON 后运行：

```powershell
& $python scripts\accept_strict_live_mapping.py `
  --database data\dota2.db `
  --evidence data\live_betting\mapping_evidence\example.json `
  --raybet-match-id 38408493 `
  --raybet-team-one-id 16122 `
  --raybet-team-two-id 37923526 `
  --canonical-team-one-id 10150538 `
  --canonical-team-two-id 9338413 `
  --event-id ewc-dota2-2026 `
  --source manual_event_team_audit `
  --actor operator-name `
  --map-number 1 `
  --map-number 2 `
  --map-number 3
```

不要用页面、SQL 或脚本直接修改 accepted mapping 行。

## 11. 告警

告警分两类：

- `operational`：标准 `*_worker` 工作进程异常。
- `paper_signal`：产生待处理纸面订单。

行为规则：

- 运维异常持续 30 秒才开告警，短暂抖动不会开事件。
- 纸面信号立即开告警。
- 相同 dedupe key 只保留一个活动事件。
- 持续状态最多每分钟追加一次 observed 审计。
- 条件消失后自动标记 recovered。
- 点击确认按钮只表示操作者已经看到，不等于问题恢复。

相关表：

```text
monitor_alert_candidates
monitor_alert_incidents
monitor_alert_audit
```

## 12. 浏览器与声音通知

### 浏览器通知

1. 点击 `系统通知` 开关。
2. 浏览器弹出权限请求时选择允许。
3. 新的未确认事件会使用事件 dedupe key 发送系统通知。

若曾选择拒绝，需要在浏览器地址栏的站点权限中重新允许 `127.0.0.1:8000`。

### 声音通知

打开 `声音` 后会播放短测试音。新告警使用 Web Audio 播放：

- warning：较低音高。
- critical：较高音高。

两个开关保存在当前浏览器的 localStorage，不会写入数据库。

## 13. 邮件通知

设置环境变量：

```powershell
$env:DOTA2_SMTP_SENDER = "your-account@qq.com"
$env:DOTA2_SMTP_AUTH_CODE = "QQ 邮箱 SMTP 授权码"
$env:DOTA2_ALERT_EMAIL_RECIPIENT = "receiver@example.com"
```

`DOTA2_SMTP_AUTH_CODE` 是 SMTP 授权码，不是邮箱登录密码。不要把授权码提交到 Git。

然后在页面启动 `邮件投递`，或手工运行：

```powershell
& $python -u scripts\run_notification_worker.py --database data\dota2.db
```

发送使用 `smtp.qq.com:465` 和证书校验的隐式 TLS。未配置邮件时：

- 页面、赔率采集、视觉和纸面策略继续运行。
- mail worker 报告 `configuration_missing`。
- 不会阻断监控页面。

SMTP 投递语义是“至少一次”：进程可能在邮件服务器已接收、但本地尚未来得及标记
`sent` 时崩溃，租约到期后会再次发送。稳定 `Message-ID` 用于追踪同一逻辑通知，
但不能保证收件服务器去重；运维侧应允许极少量重复邮件，不要把邮件数量当作事件数量。

## 14. 日志与数据

| 内容 | 路径或表 |
|---|---|
| 主数据库 | `data/dota2.db` |
| Web 标准输出 | `data/web-monitor.stdout.log` |
| Web 错误输出 | `data/web-monitor.stderr.log` |
| 管理进程日志 | `data/live_betting/logs/managed/` |
| 原始赔率响应 | `data/live_betting/raw-v2/` |
| 视觉观测 | `data/live_betting/live_observations/` |
| 视觉证据 | `data/live_betting/live_evidence/` |
| 视觉 watcher 日志 | `data/live_betting/watcher_logs/` |
| 策略判断 | `strategy_decisions` |
| 纸面订单 | `shadow_orders` |
| 研究预测 | `research_live_predictions` |
| 邮件队列 | `notification_outbox` |

查看 Web 错误：

```powershell
Get-Content data\web-monitor.stderr.log -Tail 100
```

持续查看：

```powershell
Get-Content data\web-monitor.stderr.log -Wait
```

## 15. 备份与恢复

在线备份通过 SQLite backup API 读取已提交的 WAL 内容，不要直接复制 `.db`、
`-wal` 或 `-shm` 文件：

```powershell
New-Item -ItemType Directory -Force data\backups | Out-Null
$stamp = Get-Date -Format yyyyMMdd-HHmmss
python scripts\backup_database.py `
  --database data\dota2.db `
  --output data\backups\dota2-$stamp.db
```

统一 supervisor 的日常启动只读校验 schema，不创建全库备份。安装包含 schema
变化的新代码后，先停止 Web 和所有 worker，再显式执行迁移；只有该模式会在
迁移前创建带时间戳的在线备份：

```powershell
python scripts\run_dota_shadow_service.py `
  --database data\dota2.db `
  --migrate `
  --once
```

```text
data/backups/dota2-before-service-<timestamp>.db
```

恢复前先列出实际存在的备份，不要依赖文档中的历史文件名：

```powershell
Get-ChildItem data\backups\dota2-before-service-*.db |
  Sort-Object LastWriteTime -Descending
```

恢复时必须先停止 supervisor、Web 和所有 worker。恢复命令会先在线保存当前
数据库；如果目标仍被进程占用，SQLite 独占锁检查会拒绝恢复：

```powershell
$stamp = Get-Date -Format yyyyMMdd-HHmmss
python scripts\restore_database.py `
  --database data\dota2.db `
  --backup data\backups\<backup>.db `
  --safety-backup data\backups\dota2-before-restore-$stamp.db
```

不要使用 `Copy-Item` 覆盖数据库。

## 16. 停止与重启

推荐顺序：

1. 页面停止邮件、纸面策略、视觉监控、赔率采集。
2. 查找 Web PID。
3. 停止 Web 服务。
4. 更新代码或重新构建。
5. 启动 Web 服务。
6. 按需要逐项启动 worker。

查找端口和 PID：

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen |
  Select-Object LocalAddress,LocalPort,OwningProcess
```

查看命令后再停止：

```powershell
$pid = (Get-NetTCPConnection -LocalPort 8000 -State Listen).OwningProcess
Get-CimInstance Win32_Process -Filter "ProcessId=$pid" |
  Format-List ProcessId,ExecutablePath,CommandLine
Stop-Process -Id $pid
```

## 17. 常见故障

### 页面全黑，只能向下露出一点内容

1. 按 `Ctrl+F5` 强制刷新。
2. 确认页面使用最新哈希资源。
3. 重新构建前端并重启 Web。

```powershell
Set-Location web\frontend
npm run build
Set-Location ..\..
```

### 页面返回 503

`web/frontend/dist/index.html` 不存在。运行 `npm ci` 和 `npm run build`。

### 端口被占用

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen |
  Select-Object OwningProcess
```

本机的其他 Java/RuoYi 服务可能占用 `8080`，本项目默认使用 `8000`。

### 页面无赛事

确认数据库存在：

```powershell
Get-Item data\dota2.db
```

启动赔率采集，等待一次 match list 刷新。默认列表刷新间隔为 30 秒。

### 图表为空

该局可能没有完整的 team_one/team_two 胜负盘组，或没有满足时间条件的真实快照。系统不会补造曲线点。

### SSE 变成轮询降级

先检查：

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/api/monitor/bootstrap
```

接口正常时页面仍能使用 5 秒轮询。重启 Web 可恢复 SSE 连接。

### 进程按钮不可用

- 必须通过 `127.0.0.1` 访问。
- 刷新页面重新获取本机会话和 CSRF。
- 确认 `/api/monitor/control/session` 返回 `200`。

### 邮件未发送

- 检查三个邮件环境变量。
- 确认使用授权码而不是登录密码。
- 查看 `mail_worker` 和 `notification_outbox`。
- 邮件失败不会阻断其他功能。

## 18. 验证命令

后端重点测试：

```powershell
& $python -m pytest `
  tests\test_monitoring_dashboard.py `
  tests\test_monitor_control.py `
  tests\test_monitor_alerts.py `
  tests\test_strict_live_eligibility.py `
  tests\test_notification_outbox.py -q
```

完整后端测试：

```powershell
& $python -m pytest -q
```

前端测试和构建：

```powershell
Set-Location web\frontend
npm test
npm run build
```

验收完成后，重新访问：

```text
http://127.0.0.1:8000/monitor
```
