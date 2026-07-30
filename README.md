# Dota 2 Match Predictor

Predict Dota 2 match outcomes from OpenDota history and operate a read-only
RayBet monitoring and paper-strategy pipeline. PostgreSQL is the only runtime
database. SQLite is accepted only as the source of a one-time historical import.

`DESIGN.md` is the legacy module baseline, not the current acceptance source.
See [live_betting/README.md](live_betting/README.md) for live operation and
[docs/monitoring-console-operations-manual.md](docs/monitoring-console-operations-manual.md)
for the Chinese monitoring-console guide.

## Project Structure

```text
dota2-predictor/
|-- database/            # SQLAlchemy Core engine and Alembic migrations
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
`-- data/                # Raw snapshots, evidence, logs, and reports (ignored)
```

## Quick Start

Use PowerShell 7 from the repository root:

```powershell
Copy-Item .env.template .env
python -m pip install -r requirements.txt
docker compose up -d postgres

$env:DATABASE_URL = "postgresql+psycopg://dota2:dota2_local@localhost:5432/dota2_predictor"
python -m alembic upgrade head

# Fetch historical matches and build the offline model.
python -m fetch.main
python -m features.main
python -m train.main

# Build the pre-match model consumed by predict.main.
python -m prematch.train

# Generate a prematch prediction.
python -m predict.main --radiant 9247354 --dire 10150538
```

Start the two normal long-running processes in separate PowerShell windows.
Both inherit the same `DATABASE_URL`:

```powershell
# Window 1: service supervisor
$env:DATABASE_URL = "postgresql+psycopg://dota2:dota2_local@localhost:5432/dota2_predictor"
$env:STRATZ_API_TOKEN = Read-Host -MaskInput "STRATZ API token"
python scripts/run_dota_shadow_service.py

# Window 2: Web and monitoring console
$env:DATABASE_URL = "postgresql+psycopg://dota2:dota2_local@localhost:5432/dota2_predictor"
python -m web.main
```

After building `web/frontend`, open `http://127.0.0.1:8000/monitor`.
The recurring supervisor manages historical Rosh by default. Odds collection,
Vision, notifications, and the paper strategy start only when their explicit
`--start-*` flags are supplied.

The standard direct-only paper mode is:

```powershell
$draftDeploymentKey = "<approved frozen draft deployment SHA-256>"
python scripts/run_dota_shadow_service.py `
  --start-collector --start-vision --start-shadow `
  --start-strict-ingest --start-postmatch `
  --draft-deployment-key $draftDeploymentKey
```

`--start-companion` is optional and reserved for browser audit/compare runs.
The system never exposes a real betting endpoint.

## Live Shadow Workflow

All commands use `DATABASE_URL`; `--database-url` is available when an explicit
override is needed.

```powershell
$env:DATABASE_URL = "postgresql+psycopg://dota2:dota2_local@localhost:5432/dota2_predictor"
$rawDir = "data/live_betting/raw-v2"
$visionJsonl = "data/live_betting/live_observations"

# Read-only RayBet odds collection
python -m live_betting.monitor `
  --raw-dir $rawDir --interval 6 --list-interval 30

# Visual observation supervisor
python scripts/supervise_raybet_streams.py

# Shadow strategy (paper orders only)
python scripts/run_comeback_shadow.py --vision-jsonl $visionJsonl

# Strict approved-event scheduler
python scripts/run_strict_event_ingest.py
```

RayBet rows with provider `status=1` are sampled once after entering the
two-hour prematch window and retained as audit-only transports. High-frequency
collection starts when RayBet first reports `status=2`; prematch transports
cannot become strategy, watermark, successor, or fill inputs.

Fresh signed HLS URLs are process-local capabilities. Do not put them in
commands, logs, health details, database rows, artifacts, or Web responses.

## Database

PostgreSQL schema changes are managed only by Alembic:

```powershell
python -m alembic current
python -m alembic upgrade head
```

To import the historical SQLite database once, first run a no-write inspection,
then perform the direct import:

```powershell
python scripts/migrate_sqlite_to_postgres.py `
  --sqlite data/dota2.db `
  --postgres $env:DATABASE_URL `
  --dry-run `
  --report data/postgres-import-dry-run.json

python scripts/migrate_sqlite_to_postgres.py `
  --sqlite data/dota2.db `
  --postgres $env:DATABASE_URL `
  --report data/postgres-import.json
```

The importer opens SQLite read-only, upgrades PostgreSQL to Alembic `head`,
imports in dependency order, repairs identity sequences, and verifies row
counts, critical hashes, decisions, orders, settlements, and active alerts.
It does not create a SQLite backup.

## Verification

```powershell
$env:DATABASE_URL = "postgresql+psycopg://dota2:dota2_local@localhost:5432/dota2_predictor"
python -m ruff check .
python -m pytest -q -m "not legacy_sqlite" --ignore=tests/integration/postgres
python -m pytest tests/integration/postgres -q

Set-Location web/frontend
npm test
npm run build
```

Tests that still construct SQLite runtime files or exercise retired SQLite
operations are explicitly marked `legacy_sqlite`. They remain discoverable and
must be rewritten against the PostgreSQL integration harness before the marker
is removed.

Dogfood logs, screenshots, `*.tsbuildinfo`, and local import reports are
generated evidence and must not be staged.
