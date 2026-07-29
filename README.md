# Dota 2 Match Predictor

Predict Dota 2 match outcomes using historical match data from OpenDota. Data
is stored in SQLite for model training, strict event intelligence, live shadow
analysis, and the web interface.

`DESIGN.md` is the legacy module baseline, not the current acceptance source.
Use the 
[historical intelligence delivery design](docs/historical-intelligence-delivery-design.md)
for current behavior, and [live_betting/README.md](live_betting/README.md) for
live operations.
The full Chinese monitoring-console guide is at
[docs/monitoring-console-operations-manual.md](docs/monitoring-console-operations-manual.md).

## Project Structure

```
dota2-predictor/
|-- fetch/               # Historical OpenDota ingestion and metadata
|-- event_intelligence/  # Strict Tier-1 event registry and causal profiles
|-- features/            # Offline feature generation
|-- train/               # Model training and walk-forward evaluation
|-- predict/             # Prematch prediction
|-- live_betting/        # RayBet collection and shadow-only strategy
|-- edge-extension/      # Passive Edge market monitor (Manifest V3)
|-- vision/              # Dota broadcast clock, draft, and side recognition
|-- contracts/           # Versioned live-observation contracts
|-- scripts/             # Ingestion, observation, labeling, and reporting CLIs
|-- web/                 # FastAPI server and match browser
`-- data/                # Database, raw snapshots, evidence, and reports (ignored)
```

## Quick Start

```powershell
Copy-Item .env.template .env
python -m pip install -r requirements.txt

# Fetch historical match data into SQLite
python -m fetch.main

# Build features and train the offline model
python -m features.main
python -m train.main

# Generate a prematch prediction
python -m predict.main --radiant 9247354 --dire 10150538

# Use the current cutover database for both long-running processes.
$database = "D:\dota2-predictor-cutovers\20260718-043023\restore\dota2.db"

# Terminal 1: start the service supervisor. Historical Rosh is managed by
# default; no --start-* flag is required for it.
python scripts/run_dota_shadow_service.py --database $database

# Terminal 2: start the match browser and monitoring console.
python -m web.main --database $database
```

After building `web/frontend`, open the live console at
`http://127.0.0.1:8000/monitor`. It includes history replay, freshness-derived
health, allowlisted process controls, exact-mapping audit, and persistent alerts.
The Web server and service supervisor are separate processes and both are part
of the normal project start. Running `web.main` alone serves the page but does
not run historical Rosh analysis.

The recurring supervisor starts the historical Rosh worker by default. It does
not start odds collection, vision, notifications, or the paper strategy unless
their existing `--start-*` flags are supplied. Use
`--disable-historical-rosh` only as an emergency operational override. The
supervisor's `--once` mode, including `--migrate --once`, does not start the
historical worker or make historical Rosh network requests.

The standard production paper mode is direct-only. It starts the required
collector, Vision, and shadow workers without the browser companion:

```powershell
$draftDeploymentKey = "<approved frozen draft deployment SHA-256>"
python scripts/run_dota_shadow_service.py --database $database `
  --start-collector --start-vision --start-shadow `
  --start-strict-ingest --start-postmatch `
  --draft-deployment-key $draftDeploymentKey
```

`--start-companion` is optional and reserved for browser audit/compare runs.
When omitted, health reports `stopped / not_started_by_supervisor` as
informational; it does not reduce direct-only readiness. Service reports and
monitor snapshots expose `market_source_policy=direct_primary`.

### STRATZ API credentials

`STRATZ_API_TOKEN` is the canonical STRATZ credential. `STRATZ_TOKEN` remains a
deprecated fallback for existing installations. Keep real credentials only in
an ignored local `.env` file or a deployment platform's secret store; never
commit them to Git, bake them into an image, or expose them in frontend assets.

STRATZ entry points read the service process environment. For local PowerShell
sessions, inject the credential before starting the supervisor so its historical
Rosh child inherits it:

```powershell
$env:STRATZ_API_TOKEN = Read-Host -MaskInput "STRATZ API token"
python scripts/run_dota_shadow_service.py --database $database
```

For production, create a secret named `STRATZ_API_TOKEN` in the host,
container orchestrator, or CI/CD platform and configure every STRATZ worker to
receive it as an environment variable. Restart the worker after changing the
secret so the new process inherits it. Copying `.env.template` alone does not
inject values into a running process.

## Live Shadow Workflow

All Dota 2 live-market code runs from this repository. The workflow is
strictly read-only against RayBet and can create hypothetical shadow orders
only. There is no real betting endpoint.

```powershell
$database = "D:\dota2-predictor-cutovers\20260718-043023\restore\dota2.db"
$rawDir = "D:\dota2-predictor-cutovers\20260718-043023\restore\live_betting\raw-v2"
$visionJsonl = "D:\dota2-predictor-cutovers\20260718-043023\restore\live_betting\live_observations"

# Read-only RayBet odds collection
python -m live_betting.monitor --database $database `
  --raw-dir $rawDir --interval 6 --list-interval 30

# Visual observation supervisor
python scripts/supervise_raybet_streams.py --database $database

# Shadow strategy (paper orders only)
python scripts/run_comeback_shadow.py --database $database `
  --vision-jsonl $visionJsonl

# Strict approved-event scheduler
python scripts/run_strict_event_ingest.py --database $database
```

RayBet rows with provider `status=1` are sampled at most once per match per
hour and retained as audit-only prematch transports.  The normal high-frequency
odds cadence starts immediately when RayBet reports `status=2`; prematch
transports cannot become strategy, watermark, successor, or fill inputs.

The Edge extension is at `edge-extension/`. See
[edge-extension/README.md](edge-extension/README.md) for local companion
setup. It connects directly to the localhost companion, passively captures
sanitized Dota 2 market events, and cannot submit a wager.

Fresh signed HLS URLs are process-local capabilities. Do not put them in
commands, logs, health details, SQLite, artifacts, or Web responses. Watcher
diagnostics identify stream failures only by error category and unsigned
host/path.

## Database

The current runtime match data lives in
`D:\dota2-predictor-cutovers\20260718-043023\restore\dota2.db`. The web API and
live shadow workers share that database. RayBet raw response artifacts live in
the paired
`D:\dota2-predictor-cutovers\20260718-043023\restore\live_betting\raw-v2` tree.
Runtime artifacts under the source checkout's `data/` directory are ignored by
Git. The migration, offline compaction, and self-contained bundle runbook is in
[`live_betting/README.md`](live_betting/README.md#database-migration-compaction-and-bundles).
