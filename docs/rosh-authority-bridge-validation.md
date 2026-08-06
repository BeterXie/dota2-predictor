# R.O.S.H. Authority Bridge Validation

Date: 2026-08-06
Base: `master` merge commit `36bb0bd8a20dc32c80126bcce1f8d2f5c1007bb7`
Branch: `codex/rosh-authority-bridge`

## Scope

This change only addresses the historical R.O.S.H. authority bridge. It does
not modify Draft features, Cluster policy, Team Rating, Prematch fitting,
calibration gates, or Deployment.

The bridge is deliberately one-way:

```text
legacy historical score
  -> exact formal map and role authority
  -> existing succeeded official R.O.S.H. run
  -> archived request/response artifacts
  -> active scorer/profile identity
  -> cutoff-legal replay
  -> immutable bridge ledger and Prematch R.O.S.H. snapshot
```

Legacy rows remain append-only and `backtest_eligible=0`. The bridge never
creates an official run by copying a legacy score. A row without a complete
lineage envelope remains unavailable.

## Source Database Read-Only Funnel

The source database was inspected with the default read-only mode of
`scripts/run_rosh_authority_bridge.py`. No migration or write was run against
the source database.

| Stage | Support |
| --- | ---: |
| legacy rows | 694 |
| formal map linked | 694 |
| ten heroes complete | 694 |
| expected positions complete | 561 |
| player coverage complete | 0 |
| scorer/profile available | 0 |
| input artifact available | 0 |
| response artifact available | 0 |
| cutoff legal | 0 |
| exact replay passed | 0 |
| final eligible | 0 |

Missing reasons:

| Stage | Reason | Support |
| --- | --- | ---: |
| expected positions complete | `expected_positions_incomplete` | 133 |
| player coverage complete | `player_coverage_incomplete` | 561 |

The existing full formal replay diagnostic remains consistent with the prior
acceptance report: 2,655 exact-position targets were attempted and all lacked
an available official run (`run_unavailable`). The strict bridge stops earlier
when no legacy row has complete player coverage, so it produces zero bridge
records and does not run an OOS comparison.

The source counts relevant to this work were unchanged before and after the
read-only run:

```text
historical_rosh_lineup_scores: 694
rosh_analysis_runs:             20
rosh_run_match_links:           11
```

## Bridge Contract

Migration `20260806_0030` adds the append-only
`rosh_authority_bridge_records` table. Each record binds:

- legacy `score_key`, `match_id`, prediction cutoff, ten heroes and positions;
- player coverage, active R.O.S.H. profile/formula/scorer hashes;
- request and response artifact hashes;
- `generated_at`, `available_at`, source match identity and map number;
- canonical authority JSON and replayed Prematch R.O.S.H. snapshot JSON;
- a content hash and a derived bridge key.

The table has foreign keys to the legacy score, official run, and formal map
ingest status. It also has unique legacy/run/source-match identities and
database-level append-only triggers. Bridge inserts advance the Prematch
dependency revision; a failed transaction rolls back both the run-match link
and the bridge record.

## Isolated PostgreSQL Validation

`scripts/verify_rosh_authority_bridge.py` created a temporary PostgreSQL
database, upgraded it to Alembic head `20260806_0030`, and deleted it after the
check. The source database was not used as a write target.

| Check | Result |
| --- | ---: |
| 20-row final eligible audit | 20 |
| interrupted 20-row transaction | rolled back: 0 records / 0 links |
| first 20-row write | 20 inserted |
| repeated 20-row write | 20 unchanged |
| 100-row final eligible audit | 100 |
| expansion from 20 to 100 | 80 inserted, 20 unchanged |
| final bridge records / links | 100 / 100 |
| first and last snapshot replay | `available` / `available` |
| tampered content hash | rejected before replay |
| append-only DELETE | rejected |
| source database mutation | none |

The isolated positive rows use the real official scorer, profile identity,
request archive, response archive, result hash, and replay path. Their cutoff
is intentionally after the archived historical response so this validation
tests storage and replay mechanics; it is not evidence that historical
responses were available before a real map start. The real source funnel above
remains the authority for Prematch eligibility.

## Model Decision

Final legal support in the source database is `0`. Therefore this branch does
not run Team Rating-only versus Team Rating + R.O.S.H. paired OOS metrics, and
it does not calculate or publish Brier, log loss, ECE, AUC, or bootstrap CIs for
an unsupported cohort. No model, probability, calibration, or Deployment
state is changed.

## Reproduction

Read-only source audit:

```powershell
$env:DATABASE_URL = "postgresql+psycopg://dota2:dota2_local@localhost:5432/dota2_predictor"
python scripts/run_rosh_authority_bridge.py `
  --artifact-root data/rosh-analysis-artifacts `
  --markdown-output dogfood-output/rosh-bridge-readonly.md
```

Isolated 20/100 validation:

```powershell
python scripts/verify_rosh_authority_bridge.py `
  --database-url $env:DATABASE_URL `
  --json-output dogfood-output/rosh-authority-bridge-validation.json
```

The `--persist` flag on `run_rosh_authority_bridge.py` is intentionally
explicit. It is for a separately approved target database after the read-only
funnel has identified final eligible rows; it must not be used against the
source database for this validation.
