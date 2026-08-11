# PostgreSQL migrations

Run migrations against the configured development database:

```powershell
$env:DATABASE_URL = "postgresql+psycopg://dota2:dota2_local@localhost:5432/dota2_predictor"
alembic upgrade head
```

The migration target is PostgreSQL-only runtime operation. Revision
`20260807_0035` is the current head. It includes the core match,
event-intelligence, live odds, strict mapping, draft, vision/Rosh, strategy,
runtime monitor, settlement, research, Team Rating, and prematch prediction
schemas with cutoff-aware lineage tracking and gate-failed calibration evidence.
`live_schema_version` is 12 and the runtime contract is version 1. Revision
`20260806_0030` adds the append-only R.O.S.H. authority bridge ledger, and
`20260806_0031` adds the independent prospective R.O.S.H. shadow ledger.
`20260807_0032` adds the operational prospective Team Rating producer,
authority, retry, settlement, and R.O.S.H. dependency ledgers.

SQLite is accepted only as the read-only source for the one-time importer. Run
a no-write inspection first:

```powershell
python scripts/migrate_sqlite_to_postgres.py `
  --sqlite data/dota2.db `
  --postgres $env:DATABASE_URL `
  --dry-run `
  --report data/postgres-import-dry-run.json
```

Then import directly into the configured PostgreSQL database:

```powershell
python scripts/migrate_sqlite_to_postgres.py `
  --sqlite data/dota2.db `
  --postgres $env:DATABASE_URL `
  --report data/postgres-import.json
```

The formal import upgrades Alembic to `head`, truncates PostgreSQL business
tables, imports in foreign-key and authority-trigger order, repairs identity
sequences, and fails if row counts, numeric primary-key ranges, critical
hash/key digests, or decision/order/settlement/active-alert counts differ.
It does not create a SQLite backup and never opens the source database for
writing.
