# Dota 2 Live Lineup Predictor

This project has one product path:

```text
RayBet live match
-> signed stream or match link
-> HLS frame capture
-> HUD recognition or manual correction
-> locked LiveDraftMapping with ten heroes and positions 1-5
-> Team Rating P0
-> frozen pure-lineup R.O.S.H. P1 when STRATZ evidence is available
-> immutable prediction
-> authoritative result binding and settlement
-> historical result view
```

PostgreSQL is the only runtime database. Odds remain visible in match details but
are not inputs to P0 or P1. The prediction path does not consume kills, economy,
experience, towers, Roshan state, score, game clock, or any other in-game state.

## Runtime

Use PowerShell 7 from the repository root:

```powershell
python -m pip install -r requirements.txt

$env:DATABASE_URL = Read-Host "PostgreSQL DATABASE_URL"
python -m alembic upgrade head
```

Start RayBet collection and the local Web application in separate terminals:

```powershell
$env:DATABASE_URL = Read-Host "PostgreSQL DATABASE_URL"
python -m live_betting.monitor --raw-dir data/live_betting/raw-v2 --interval 6 --list-interval 30

$env:DATABASE_URL = Read-Host "PostgreSQL DATABASE_URL"
python -m web.main
```

Build the frontend, then open `http://127.0.0.1:8000/monitor`:

```powershell
Set-Location web/frontend
npm install
npm run build
```

The operations view controls only the RayBet collector. It also shows current
service health, strict mappings, and operational alerts.

## Stream And HUD

Signed HLS URLs are process-local capabilities. Do not store them in commands,
logs, database rows, artifacts, health details, or API responses.

The stream supervisor discovers RayBet matches, refreshes signed HLS access,
captures frames, and publishes HUD observations:

```powershell
$env:DATABASE_URL = Read-Host "PostgreSQL DATABASE_URL"
python scripts/supervise_raybet_streams.py
```

A single stream can be inspected directly:

```powershell
python scripts/watch_raybet_stream.py --help
python scripts/capture_raybet_stream.py --help
```

The retained Vision path includes hero slots, game clock, kills, net worth,
Radiant/Dire orientation, pause state, screen state, OCR, confidence diagnostics,
multi-frame evidence, the frame registry, evidence retention, and manual
correction. HUD values are display and audit evidence only; P0/P1 never read
them.

## Prediction

In the live match detail:

1. Confirm the two canonical teams.
2. Confirm ten unique heroes and positions 1-5.
3. Lock a `LiveDraftMapping`.
4. Select **生成实时阵容预测** while the map has no authoritative end/result.
5. Review immutable P0/P1 evidence.

Team Rating P0 restores the frozen prospective seed and chronologically replays
only authoritative results available before the mapping lock time. It excludes
the target map and future results.

P1 uses candidate
`84c4506f63b7c5b745b32373b0cb405383f837c60eae3231cc3d688a0b36e09d`
with profile `legacy-dematus-pure-rosh-prospective-v1`. STRATZ request and
response bytes are stored as content-addressed gzip artifacts and replayed
offline with the frozen legacy pure-lineup scorer. Missing or invalid R.O.S.H.
evidence produces P0-only with a stable reason.

Saving a corrected lineup creates a new mapping version. Existing predictions
remain bound to the version that created them.

## Results

Formal results are ingested through the approved OpenDota event registry:

```powershell
$env:DATABASE_URL = Read-Host "PostgreSQL DATABASE_URL"
python scripts/run_strict_event_ingest.py
python scripts/run_postmatch_labeler.py
```

The postmatch labeler requires strict map identity and consistent RayBet and
OpenDota winners before writing `map_results`. It then appends live prediction
settlements. Predictions are never updated during settlement.

## Verification

```powershell
$ruff = (Get-Command ruff).Source
& $ruff check .

$env:DATABASE_URL = Read-Host "PostgreSQL DATABASE_URL"
python -m pytest -q --ignore=tests/integration/postgres
python -m pytest tests/integration/postgres -q

Set-Location web/frontend
npm test
npm run build
```
