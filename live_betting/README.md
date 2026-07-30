# Dota 2 Live Shadow Betting

This package records RayBet Dota 2 odds and creates hypothetical orders only.
It has no real betting endpoint. PostgreSQL is the only runtime database.

## Current Capability

- RayBet Dota 2 discovery (`game_id=151`) and immutable odds transports
- Winner and derivative market normalization and settlement
- Complete-outcome-group de-vigging
- Next-snapshot fills, slippage rejection, and idempotent shadow orders
- Causal visual-clock alignment with no future-frame interpolation
- Strict exact mapping, draft authority, and official Rosh evidence
- Explainable comeback decisions with one shadow attempt per map
- Append-only research predictions, notifications, and audit records

## Database Setup

Set the one runtime authority and apply Alembic migrations before starting any
worker:

```powershell
$env:DATABASE_URL = "postgresql+psycopg://dota2:dota2_local@localhost:5432/dota2_predictor"
docker compose up -d postgres
python -m alembic upgrade head
```

Workers verify the installed Alembic and runtime contracts; they do not create,
repair, back up, or replace the schema at startup.

## Commands

One read-only collection pass:

```powershell
python -m live_betting.monitor --once
```

Continuous collection with explicit artifact storage:

```powershell
python -m live_betting.monitor `
  --raw-dir data/live_betting/raw-v2 `
  --interval 6 --list-interval 30
```

Start the visual supervisor and inspect retention before deleting unprotected
evidence:

```powershell
python scripts/supervise_raybet_streams.py
python scripts/cleanup_vision_evidence.py
python scripts/cleanup_vision_evidence.py --delete
```

All visual watchers share `data/live_betting/live_evidence/` as a
content-addressed evidence root. Frames used by decisions, orders, settlements,
research predictions, draft anchors, or conflicts are protected by PostgreSQL
lineage.

Run the paper strategy and exact post-match labeling:

```powershell
python scripts/run_comeback_shadow.py `
  --vision-jsonl data/live_betting/live_observations
python scripts/run_postmatch_labeler.py --all
```

Run supporting jobs and reports:

```powershell
python scripts/backfill_team_profiles.py --team-id 5014799 --limit 30
python -m live_betting.report --output data/live_betting/shadow_report.json
python scripts/run_notification_worker.py --once
```

The notification worker uses `smtp.qq.com:465` with certificate-verified TLS.
Missing SMTP configuration reports `mail_degraded` without stopping collection
or shadow evaluation. Every message states that no real wager was placed.

Official R.O.S.H. request and response evidence is stored under
`ROSH_ANALYSIS_ARTIFACTS_DIR` (default `data/rosh-analysis-artifacts`). A
completed run is eligible only for odds transports whose `observed_at` is at or
after the run's `collected_at`.

## Supervisor

The recurring supervisor verifies PostgreSQL and starts historical Rosh by
default. Other components require explicit flags:

```powershell
$deploymentKey = "<deployment_key from the offline rebuild output>"
python scripts/run_dota_shadow_service.py `
  --start-collector --start-shadow --start-vision `
  --start-strict-ingest --start-postmatch --start-draft-publisher `
  --draft-deployment-key $deploymentKey `
  --vision-jsonl data/live_betting/live_observations
```

Use `--start-companion` only for browser audit/compare runs and `--start-mail`
only after SMTP is configured. `--once` runs one PostgreSQL health/report cycle
without starting the recurring historical Rosh worker.

The supervisor uses a PostgreSQL advisory lock for singleton ownership. Child
processes inherit `DATABASE_URL`; there are no SQLite file locks, writer scans,
WAL checkpoints, online backups, cutovers, compaction, or database bundles.

## One-Time SQLite Import

SQLite remains supported only as a read-only historical source:

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

The formal import truncates target business tables, imports in dependency and
authority-trigger order, repairs sequences, and fails closed on row-count,
primary-key range, critical hash/key, decision, order, settlement, or alert
count mismatches. It never writes the SQLite source and does not make a backup.

## Verification

```powershell
python -m alembic heads
python -m pytest tests/integration/postgres -q
python -m ruff check database fetch scripts/migrate_sqlite_to_postgres.py tests/integration/postgres
```

The integration suite uses disposable PostgreSQL databases and covers schema
constraints, append-only triggers, transactions, idempotency, concurrent worker
claims, storage services, monitoring APIs, vision retention, and the importer.

`strategy_decisions` retains rejected decisions and their reasons. Results are
descriptive below 100 settled shadow orders and provisional below 500.
Post-match fields are written only after RayBet marks the series completed and
are never used by a decision from that series.
