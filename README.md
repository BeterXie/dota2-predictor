# Dota 2 Match Predictor

Predict Dota 2 match outcomes using historical match data from OpenDota. Data
is stored in SQLite for model training, strict event intelligence, live shadow
analysis, and the web interface.

`DESIGN.md` is the legacy module baseline, not the current acceptance source.
Use the [current specifications](docs/superpowers/specs/) together with the
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

# Start the match browser and monitoring console
python -m web.main
```

After building `web/frontend`, open the live console at
`http://127.0.0.1:8000/monitor`. It includes history replay, freshness-derived
health, allowlisted process controls, exact-mapping audit, and persistent alerts.

## Live Shadow Workflow

All Dota 2 live-market code runs from this repository. The workflow is
strictly read-only against RayBet and can create hypothetical shadow orders
only. There is no real betting endpoint.

```powershell
# Read-only RayBet odds collection
python -m live_betting.monitor --database data/dota2.db `
  --raw-dir data/live_betting/raw-v2 --interval 6 --list-interval 30

# Visual observation supervisor
python scripts/supervise_raybet_streams.py --database data/dota2.db

# Shadow strategy (paper orders only)
python scripts/run_comeback_shadow.py --database data/dota2.db `
  --vision-jsonl data/live_betting/live_observations

# Strict approved-event scheduler
python scripts/run_strict_event_ingest.py --database data/dota2.db
```

The Edge extension is at `edge-extension/`. See
[edge-extension/README.md](edge-extension/README.md) for local companion
setup. It connects directly to the localhost companion, passively captures
sanitized Dota 2 market events, and cannot submit a wager.

## Database

All match data lives in `data/dota2.db`. The web API and live shadow workers
share that database. RayBet raw response artifacts live in the paired
`data/live_betting/raw-v2` tree. Runtime artifacts under `data/` are ignored by
Git. The migration, offline compaction, and self-contained bundle runbook is in
[`live_betting/README.md`](live_betting/README.md#database-migration-compaction-and-bundles).
