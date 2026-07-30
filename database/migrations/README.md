# PostgreSQL migrations

Run migrations against the configured development database:

```powershell
$env:DATABASE_URL = "postgresql+psycopg://dota2:dota2_local@localhost:5432/dota2_predictor"
alembic upgrade head
```

The migration target is PostgreSQL-only runtime operation. Revision
`20260730_0001` establishes the core match schema; event-intelligence and live
betting schemas still need their own PostgreSQL revisions before runtime
cutover. SQLite will then remain only as a source for the one-time import tool.
