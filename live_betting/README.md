# Dota 2 Live Shadow Betting

This package records RayBet Dota 2 odds and creates hypothetical orders only.
It has no real betting endpoint.

## Current Capability

- RayBet Dota 2 discovery (`game_id=151`) and immutable odds snapshots
- Winner, total kills, team total kills, kill handicap, race-to-kills, and
  duration market normalization and settlement
- Complete-outcome-group de-vigging
- Next-snapshot fills, slippage rejection, idempotent shadow orders
- Brier, log-loss, fill-rate, and shadow ROI helpers
- Explicit odds-source backoff
- Causal visual-clock alignment with no future-frame interpolation
- Team style, roster-change, player form, and draft timing profiles
- Explainable comeback decisions with one shadow attempt per map
- Exact-draft OpenDota post-match labeling and JSON evaluation reports
- Append-only research-only live predictions with successor-price and result labels

## Commands

One read-only collection pass:

```powershell
python -m live_betting.monitor --once
```

Continuous collection with the production-safe polling intervals:

```powershell
python -m live_betting.monitor --database data/dota2.db `
  --raw-dir data/live_betting/raw-v2 --interval 6 --list-interval 30
```

Run tests:

```powershell
python -m pytest -q
```

Content-addressed raw responses are written under `data/live_betting/raw-v2/`;
normalized rows and raw-artifact references use the existing `data/dota2.db`
database. Treat the database and this registered raw tree as one dataset.

All visual watchers share `data/live_betting/live_evidence/` as one
content-addressed evidence root. Frames are published as
`sha256/<prefix>/<sha256>.jpg`; JSONL observations bind the stable frame ref,
SHA-256, byte length, and physical path. Treat this registered tree and the
database as one dataset as well. Do not move registered frames by hand.

Start one visual watcher for every active RayBet match that exposes an HLS
stream. Observations, evidence frames, and watcher logs stay under the project
data directory:

```powershell
python scripts/supervise_raybet_streams.py --database data/dota2.db
```

The visual supervisor keeps frames used by decisions, orders, settlements,
research predictions, draft anchors/conflicts, and one audit frame per map per
10 game minutes. It skips active matches and hourly removes only other JPGs
older than 7 days or, after a one-hour ingestion grace period, beyond 2,000
unprotected frames per match. Inspect the same plan without deleting anything:

```powershell
python scripts/cleanup_vision_evidence.py --database data/dota2.db
```

Add `--delete` only for an explicit manual cleanup; the command is dry-run by
default and refuses incomplete lineage schema or an unsafe evidence root.

Run the strategy monitor against that observation directory:

```powershell
python scripts/run_comeback_shadow.py `
  --database data/dota2.db `
  --vision-jsonl data/live_betting/live_observations
```

For every strictly mapped map with a complete market surface and confirmed
ten-hero observation, the same worker also appends a non-actionable research
prediction. Missing deployment/calibration artifacts remain `null` with an
explicit gate reason. These rows never enter `shadow_orders`; manual-control
page time remains `diagnostic_untrusted` even after continuity checks.

For passive browser capture, start `python -m live_betting.browser_companion`
and load `edge-extension/` as an unpacked Edge extension. It connects directly
to the localhost companion; setup and safety details are documented in
`edge-extension/README.md`.

Refresh the local hero-recognition asset after the Dota hero roster changes:

```powershell
python scripts/fetch_hero_portraits.py
python scripts/build_hero_features.py
```

Run exact-draft post-match labeling for every completed observed series:

```powershell
python scripts/run_postmatch_labeler.py --database data/dota2.db --all
```

Backfill recent historical detail for a known OpenDota team ID:

```powershell
python scripts/backfill_team_profiles.py --team-id 5014799 --limit 30
```

Generate the current shadow report:

```powershell
python -m live_betting.report --database data/dota2.db `
  --output data/live_betting/shadow_report.json
```

The report includes research coverage, next-price movement, settled accuracy,
Brier score, and log-loss. Model metrics remain `null` until a deployable model
has produced raw probabilities and post-match labels exist.

Deliver simulation notifications from the transactional outbox (credentials
are read only from `DOTA2_SMTP_SENDER` and `DOTA2_SMTP_AUTH_CODE`, or an
installed OS keyring):

```powershell
python scripts/run_notification_worker.py --database data/dota2.db --once
```

The worker uses `smtp.qq.com:465` with certificate-verified implicit TLS. A
missing SMTP configuration reports `mail_degraded` and does not stop odds
collection or shadow evaluation. Every message states that no real wager was
placed.

After installing code that changes the database schema, stop Web and all workers,
then run the explicit migration phase. This is the only supervisor mode that
takes a full online backup:

```powershell
python scripts/run_dota_shadow_service.py --migrate --once --database data/dota2.db
```

The current protocol target is live schema `v8` and intelligence schema `v9`.
The live migration does not invent hashes for legacy visual evidence: a frame
without its registered SHA-256 and byte length remains audit-only and cannot
authorize a decision, order, notification, report score, or settlement.

Use the dedicated read-only command for routine schema, contract, and artifact
verification:

```powershell
python scripts/database_cutover.py verify-prepared `
  --database data/dota2.db `
  --odds-raw-root data/live_betting/raw-v2
```

The verifier opens SQLite with `mode=ro`, enables `query_only`, and never writes
`service_health`. The supervisor's `--once` mode runs one health/report cycle and
does write operational state; it is not a read-only verification command.

To run the complete passive shadow pipeline:

```powershell
python scripts/run_dota_shadow_service.py --database data/dota2.db `
  --start-collector --start-companion --start-shadow --start-vision `
  --start-strict-ingest --start-postmatch --start-draft-publisher `
  --vision-jsonl data/live_betting/live_observations
```

`--start-shadow` also starts the independent draft publisher. The publisher
builds or loads frozen model/calibration artifacts outside the 3-second shadow
loop. Failed or reconstructed calibration gates still publish immutable
research evidence, but can never authorize a shadow order.

## Database Migration, Compaction, And Bundles

Stop Web and every worker before an offline cutover. Run the explicit schema
phase once; it takes one verified online database snapshot only when migration
or repair is required. Set the following variables to new directories on a
volume with enough free space. Do not run this command repeatedly as a backup
loop. Keep all processes stopped after this point, including workers previously
started outside the supervisor:

```powershell
$backupDir = "X:\dota2-migration-backups"
$compactionDir = "X:\dota2-compaction"
$bundleDir = "X:\dota2-compacted-bundle"
$restoreDir = "X:\dota2-restored"

python scripts/run_dota_shadow_service.py --migrate --once `
  --database data/dota2.db --backup-dir $backupDir
```

`--migrate --once` is a write phase. After it exits, use the formal checkpoint
command. It acquires the same service lock as the supervisor, uses a non-blocking
`wal_checkpoint(TRUNCATE)`, prints the `busy`, `log`, and `checkpoint` triplet,
and exits successfully only for `(0, 0, 0)` with `wal_bytes=0`. A held service
lock, an active SQLite writer, or a non-empty WAL fails closed:

```powershell
python scripts/database_cutover.py checkpoint --database data/dota2.db

python scripts/database_cutover.py verify-prepared `
  --database data/dota2.db `
  --odds-raw-root data/live_betting/raw-v2
```

The service lock covers supervisor-managed writers. SQLite cannot identify a
different process that merely has an idle writable connection, so stopping Web
and every standalone worker remains a mandatory operator gate. Do not restart
anything between checkpoint and publication.

Keep the migration backup and any existing `data/live_betting/raw-v2` tree
unchanged. If `raw-v2` is absent, the verifier and compactor accept that absence
only when the database proves `odds_raw_artifacts` has zero registered rows. A
registered reference without its exact gzip fails; never create replacement or
placeholder artifact files.

Check free space on the destination volume before each phase. Let `L` be
`page_count * page_size` for the prepared source, `R` the registered compressed
raw bytes, `C` the compacted database bytes, `A` all bundle artifact bytes, and
`M = 512 MiB`. The code enforces these free-space floors in order:

1. Migration snapshot: at least `L` additional bytes in `$backupDir`.
2. Fresh compaction: at least `3L + R + M` free in `$compactionDir`'s volume.
3. Bundle creation: at least `C + A + M` free in `$bundleDir`'s volume.
4. Restore: at least `C + A + M` free in `$restoreDir`'s volume.

When every new output is retained on D:, a deliberately conservative initial
budget is `L + (3L + R + M) + (C + A + M) + (C + A + M)` additional free bytes,
excluding files already present on D:. Recalculate `C` and `A` from compaction
and bundle output before proceeding; do not rely only on the estimate.

The compactor rejects a source with a non-empty WAL and never modifies the
source database or source raw tree. Its destination must be a new directory:

```powershell
python scripts/compact_legacy_odds.py `
  --database data/dota2.db `
  --raw-root data/live_betting/raw-v2 `
  --destination-root $compactionDir
```

The result is `$compactionDir/dota2-compacted.db` paired with
`$compactionDir/live_betting/raw-v2`. A failed, target-matched checkpoint
resumes only with `--resume`; never point the destination at the production
directory.

Create and verify a self-contained publication bundle from that compacted pair.
The bundle destination must not already exist. Add one `--allow-source-root`
for each audited `raw_source_artifacts` root outside the database directory:

```powershell
python scripts/database_bundle.py create `
  --database (Join-Path $compactionDir "dota2-compacted.db") `
  --odds-raw-root (Join-Path $compactionDir "live_betting/raw-v2") `
  --bundle $bundleDir

python scripts/database_bundle.py verify --bundle $bundleDir

python scripts/database_bundle.py restore `
  --bundle $bundleDir `
  --destination $restoreDir `
  --database-name dota2.db

python scripts/database_cutover.py verify-prepared `
  --database (Join-Path $restoreDir "dota2.db") `
  --odds-raw-root (Join-Path $restoreDir "live_betting/raw-v2")
```

The bundle contains only artifacts registered by its database snapshot:
RayBet raw responses, completed-match source artifacts, and active visual
frames. Creation and verification recompute every artifact identity and reject
links, hardlinks, missing files, or byte mismatches. `restore` also requires a
new destination, restores visual frames under the shared
`live_betting/live_evidence` content-addressed root, appends audited relocation
records, and verifies every manifest byte. The final command above is the
read-only schema and artifact-authority preflight. Start Web against that exact
candidate through the single database authority:

```powershell
python -m web.main --database (Join-Path $restoreDir "dota2.db")
```

Web resolves database paths by the documented priority `--database`, then
`DATABASE_PATH`, then `web/config.yaml`, then the project default. Query and
prediction paths both consume the resolved `web.queries.DB_PATH`. Retain the
original database, its migration snapshot, and its raw tree until the restored
service passes integrity, schema, artifact-authority, and application smoke
checks.

The supervisor takes its single-instance lock and verifies exact schema versions,
required tables, and migration-critical columns before starting workers. Routine
starts never create a backup or mutate the schema. `--migrate` creates a verified
SQLite online backup under `data/backups` before additive migrations;
`--backup-dir` can place that migration snapshot on another local volume. The
supervisor reports companion reachability plus independent strict ingest and
post-match worker health. Components remain stopped until their `--start-*`
flags are supplied. Add `--start-mail` only after SMTP is configured.

`strategy_decisions` retains rejected decisions and their reasons. A result is
descriptive below 100 settled shadow orders and remains provisional below 500.
Post-match fields are written only after RayBet marks the series completed and
are never used by a decision from that series.
