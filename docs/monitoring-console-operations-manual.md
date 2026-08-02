# Dota 2 滚球监控台操作手册

本文档适用于项目内置的本机监控台，用于查看 RayBet 赛事、赔率历史、OpenDota 赛事数据、exact 映射和运行告警。系统不会提交真实投注。PostgreSQL 是唯一运行数据库；SQLite 只作为一次性历史导入源。Vision、纸面策略、阵容发布和 post-match 仅保留为历史数据或手动研究能力，不属于监控台主流程。

## 1. 安全边界

- Web 默认监听 `127.0.0.1`，不要暴露到局域网或公网。
- RayBet 采集只读；监控台不提交任何投注或策略订单。
- 页面只能控制固定白名单进程，不能提交任意命令。
- 停止进程前核对 PID、创建时间和固定命令；身份变化时拒绝终止。
- 页面不能编辑原始赔率、实时调参、执行真实投注或接受 fuzzy/name-only 映射。
- exact 映射、失效、进程控制和告警确认都保留数据库审计。

## 2. 环境与安装

推荐 Windows 10/11、PowerShell 7、Python 3.11、Node.js 22 和 npm 10。

```powershell
Set-Location C:\Users\59908\dota2-predictor
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.template .env

docker compose up -d postgres
$env:DATABASE_URL = "postgresql+psycopg://dota2:dota2_local@localhost:5432/dota2_predictor"
& .\.venv\Scripts\python.exe -m alembic upgrade head
```

构建前端：

```powershell
Set-Location web\frontend
npm ci
npm run build
Set-Location ..\..
```

FastAPI 直接提供 `web/frontend/dist/`，日常运行不需要单独启动 Vite。

## 3. 启动与访问

Web 和 supervisor 是两个独立常驻进程，必须使用完全相同的 `DATABASE_URL`。worker 从 supervisor 环境继承该变量。

第一个 PowerShell 窗口：

```powershell
Set-Location C:\Users\59908\dota2-predictor
$python = (Resolve-Path .\.venv\Scripts\python.exe).Path
$env:DATABASE_URL = "postgresql+psycopg://dota2:dota2_local@localhost:5432/dota2_predictor"
$env:STRATZ_API_TOKEN = Read-Host -MaskInput "STRATZ API token"
& $python scripts\run_dota_shadow_service.py
```

无 flag 启动只管理默认的历史 Rosh worker，不会启动赔率或邮件。标准运行模式：

```powershell
& $python scripts\run_dota_shadow_service.py `
  --start-collector `
  --start-strict-ingest
```

SMTP 配置完成后才增加 `--start-mail`。OpenDota strict ingest 由 supervisor 常驻管理；Vision、阵容发布、纸面策略和 post-match 只保留历史数据与手动研究命令。

第二个 PowerShell 窗口：

```powershell
Set-Location C:\Users\59908\dota2-predictor
$python = (Resolve-Path .\.venv\Scripts\python.exe).Path
$env:DATABASE_URL = "postgresql+psycopg://dota2:dota2_local@localhost:5432/dota2_predictor"
& $python -m web.main
```

访问 `http://127.0.0.1:8000/monitor`，并用下面两个请求做快速检查：

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/monitor
Invoke-RestMethod http://127.0.0.1:8000/api/monitor/bootstrap
```

两项都应返回 HTTP `200`。

## 4. 后台启动

```powershell
$root = (Get-Location).Path
$python = (Resolve-Path .\.venv\Scripts\python.exe).Path
$env:DATABASE_URL = "postgresql+psycopg://dota2:dota2_local@localhost:5432/dota2_predictor"

$web = Start-Process `
  -FilePath $python `
  -ArgumentList "-m", "web.main" `
  -WorkingDirectory $root `
  -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $root "data\web-monitor.stdout.log") `
  -RedirectStandardError (Join-Path $root "data\web-monitor.stderr.log") `
  -PassThru

$service = Start-Process `
  -FilePath $python `
  -ArgumentList "scripts\run_dota_shadow_service.py" `
  -WorkingDirectory $root `
  -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $root "data\dota-shadow-service.stdout.log") `
  -RedirectStandardError (Join-Path $root "data\dota-shadow-service.stderr.log") `
  -PassThru

$web.Id
$service.Id
```

`Start-Process` 会继承当前 PowerShell 的 `DATABASE_URL` 和 STRATZ/SMTP 环境变量。

## 5. 页面与时间轴

赛事列表优先显示比赛、数据健康和赛事映射状态。详情首屏显示比分、当前胜负盘和映射结果，历史证据默认折叠。

历史复盘以真实采集时间为主轴：

- 主时间：`received_at / observed_at / captured_at`
- 历史视觉观测等证据只作为已采集数据回看，不参与监控台运行就绪判断。
- 超过 60 秒的采集空洞显示为断点，不补造数据。
- 图表只使用真实完整胜负盘快照。
- 不可信、手工或缺失的比赛时钟不会替代采集时间。

就绪链路逐场显示赔率采集和赛事映射。`延迟` 表示已有数据但超过警告时间；`过期`、`无效` 或 `异常` 不可作为当前监控依据。

## 6. 赔率采集规则

赔率采集器默认每 30 秒刷新比赛列表：

- `status=1` 的赛前比赛只在进入开赛前两小时窗口后采集一次。
- 赛前赔率以 `audit_only` transport 保存。
- 窗口外、重复轮询和计划开赛时间已过但状态未切换时不重复采集。
- RayBet 首次返回 `status=2` 时立即进入 `--interval` 指定的正式滚球频率。
- 赛前 transport 不会进入 strategy、watermark、successor 或 fill。

## 7. 安全进程控制

页面只允许以下固定命令；数据库连接由 `DATABASE_URL` 继承：

| 页面名称 | 固定命令 |
|---|---|
| 赔率采集 | `python -u -m live_betting.monitor --raw-dir data/live_betting/raw-v2 --interval 6 --list-interval 30` |
| 邮件投递 | `python -u scripts/run_notification_worker.py` |

OpenDota strict ingest 和历史 Rosh 不在 Web allowlist 中，由 supervisor 管理。每次控制操作都需要本机会话、CSRF 和二次确认，后端不接收页面提供的命令文本。

## 8. Exact 映射

`manual_exact` 必须通过队伍 ID、顺序、event、时区、赛程、阶段、BO 和 canonical team 的全部硬校验。`automatic_exact` 只能复用已人工批准的同一证据哈希；candidate、fuzzy 和仅凭名称的结果不能进入策略。

CLI 人工登记示例：

```powershell
& $python scripts\accept_strict_live_mapping.py `
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

失效不会删除旧记录。系统追加 mapping invalidation、supersession 和 impact 审计，并标记依赖旧映射的研究判断、策略判断和纸面订单。

## 9. 告警与通知

`operational` 告警表示数据库、赔率采集、OpenDota strict ingest、历史 Rosh 或邮件 worker 异常，持续 30 秒才开启；历史纸面订单若仍有待处理状态，会以 `paper_signal` 告警保留兼容读取。相同 dedupe key 只保留一个活动事件，条件消失后自动 recovered。

SMTP 配置：

```powershell
$env:DOTA2_SMTP_SENDER = "your-account@qq.com"
$env:DOTA2_SMTP_AUTH_CODE = "QQ 邮箱 SMTP 授权码"
$env:DOTA2_ALERT_EMAIL_RECIPIENT = "receiver@example.com"
& $python -u scripts\run_notification_worker.py
```

发送使用 `smtp.qq.com:465` 和证书校验的隐式 TLS。未配置邮件不会阻断页面或赔率采集。

## 10. 日志与数据

| 内容 | 路径或表 |
|---|---|
| 主数据库 | `DATABASE_URL` 指向的 PostgreSQL 数据库 |
| Web 日志 | `data/web-monitor.*.log` |
| 管理进程日志 | `data/live_betting/logs/managed/` |
| 原始赔率响应 | `data/live_betting/raw-v2/` |
| 视觉观测 | `data/live_betting/live_observations/` |
| 视觉证据 | `data/live_betting/live_evidence/` |
| 策略判断 | `strategy_decisions` |
| 纸面订单 | `shadow_orders` |
| 研究预测 | `research_live_predictions` |
| 邮件队列 | `notification_outbox` |

查看 Web 错误：

```powershell
Get-Content data\web-monitor.stderr.log -Tail 100
Get-Content data\web-monitor.stderr.log -Wait
```

## 11. Schema 与一次性迁移

Schema 只由 Alembic 管理。更新包含迁移的代码后，停止 Web 和 worker，执行：

```powershell
& $python -m alembic current
& $python -m alembic upgrade head
```

项目不再提供 SQLite 在线备份、WAL checkpoint、文件替换恢复、cutover、compaction 或 bundle 命令。

一次性历史导入先 dry-run，再正式执行：

```powershell
& $python scripts\migrate_sqlite_to_postgres.py `
  --sqlite data\dota2.db `
  --postgres $env:DATABASE_URL `
  --dry-run `
  --report data\postgres-import-dry-run.json

& $python scripts\migrate_sqlite_to_postgres.py `
  --sqlite data\dota2.db `
  --postgres $env:DATABASE_URL `
  --report data\postgres-import.json
```

导入器只读打开 SQLite，清空目标业务表，升级到 Alembic `head`，按依赖顺序导入、修复 identity sequence，并核对行数、主键范围、关键 hash/key、策略判断、订单、结算和活动告警。按当前项目决策，它不创建 SQLite 备份。

PostgreSQL 的物理备份和恢复应使用部署环境标准的 `pg_dump` / `pg_restore` 或托管数据库快照，不由应用 supervisor 执行。

## 12. 停止与重启

1. 页面停止邮件和赔率采集。
2. 停止 supervisor；OpenDota strict ingest 和默认历史 Rosh 会随之停止。
3. 停止 Web。
4. 更新代码，执行 `alembic upgrade head`，必要时重新构建前端。
5. 使用同一 `DATABASE_URL` 启动 supervisor 和 Web。

查找端口：

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen |
  Select-Object LocalAddress,LocalPort,OwningProcess
```

## 13. 常见故障

### 页面返回 503

`web/frontend/dist/index.html` 不存在。运行 `npm ci` 和 `npm run build`。

### 页面无赛事或 API 数据库错误

检查连接和 Schema：

```powershell
& $python -m alembic current
& $python scripts\run_dota_shadow_service.py --once
```

确认 Web 与 supervisor 的 `DATABASE_URL` 完全相同，再等待一次 match list 刷新。

### SSE 变成轮询降级

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/api/monitor/bootstrap
```

接口正常时页面仍可使用轮询；重启 Web 可恢复 SSE。

### 进程按钮不可用

- 必须通过 `127.0.0.1` 访问。
- 刷新页面重新获取本机会话和 CSRF。
- 检查 `/api/monitor/control/session` 是否返回 `200`。

## 14. 验证命令

```powershell
$env:DATABASE_URL = "postgresql+psycopg://dota2:dota2_local@localhost:5432/dota2_predictor"
& $python -m pytest tests\integration\postgres -q
& $python -m ruff check database fetch scripts\migrate_sqlite_to_postgres.py tests\integration\postgres

Set-Location web\frontend
npm test
npm run build
Set-Location ..\..
```

完成后重新访问 `http://127.0.0.1:8000/monitor`。
