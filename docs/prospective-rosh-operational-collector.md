# Operational prospective R.O.S.H. collector

## Status and scope

This change adds the operational evidence collector that sits between an already
valid prospective Team Rating P0 and the existing prospective R.O.S.H. shadow
ledger. It is research-only. It does not write the formal Prematch probability,
change Calibration or Deployment, or reach an order path.

The collector is ready for isolated and future-map validation. No historical map
is relabelled as prospective, and no missed cutoff can be backfilled. A real 5–10
map acceptance run still requires an operational PostgreSQL source containing
future formal maps, valid prospective Team Rating authorities, prospective role
assignments, and a STRATZ token.

## Existing capability audit

The implementation deliberately reuses the current components:

| Responsibility | Reused implementation |
| --- | --- |
| Frozen candidate loading and verification | `event_intelligence.prospective_rosh_candidate` |
| Valid prospective P0 authority | `ProspectiveTeamRatingRepository.load_rosh_team_rating_authority` |
| Stable missing-P0 lineage | `resolve_rosh_team_rating_authority` |
| Exact-byte gzip archive and content addressing | `ExactByteArtifactStore` |
| Request/response manifests | `archive_exact_artifacts` and `artifact_manifest_hash` |
| Frozen legacy normalization and pure lineup scoring | `build_prospective_rosh_evidence` |
| Offline exact replay | `replay_archived_pure_rosh` |
| Paired P0/P1 and P0-only records | `build_shadow_prediction` |
| Immutable prediction and settlement ledger | `ProspectiveRoshShadowRepository` |

The only transport gap was that the legacy three-operation client discarded raw
HTTP bytes. `fetch_legacy_lineup_batch` now sends canonical exact request bytes and
returns the exact response bytes without substituting the incompatible official-v2
batch path.

## Frozen identity

Operational collection fails closed unless every identity matches:

```text
candidate hash:
84c4506f63b7c5b745b32373b0cb405383f837c60eae3231cc3d688a0b36e09d

profile:
legacy-dematus-pure-rosh-prospective-v1

formula:
dematus-rosh-0e1e6651dd932055dee69c4fb44435774f619793

pure_lineup_only = true
player_identity_used = false
official_v2_compatible = false
```

Player IDs are not read by this collector. The scorer input is exactly five
position-ordered Radiant heroes and five position-ordered Dire heroes.

## Collection order and cutoff semantics

For each upcoming formal map, the collector performs these checks in order:

1. Load the formal match and its `prediction_cutoff`.
2. Resolve a Team Rating authority whose run is `prospective` and `trained`, whose
   prediction is `predicted`, and whose eventual result is still null.
3. Load ten distinct heroes and high-confidence prospective
   `expected_position` assignments. Each side must contain positions 1–5 exactly
   once, and role input/creation times must already be available.
4. Set `statistics_cutoff` to the request start time.
5. Request `heroes_meta_positions`, `hero_stats_by_time_bracket`, and `synergy`.
6. Set `available_at` to completion of the third validated response.
7. Archive each exact request and response body as deterministic gzip, with raw
   content hash, gzip hash, byte count, relative path, and manifest hash.
8. Recompute `pure_lineup_score` only from the archived responses and the frozen
   legacy scorer.
9. Persist paired P0/P1, or fail closed to P0-only with a stable reason.

The enforced temporal invariant is:

```text
statistics_cutoff <= available_at <= prediction_cutoff
```

If the batch completes after cutoff, its bytes may remain archived for diagnosis,
but no shadow prediction is written. A worker that first observes the map after
cutoff writes only a terminal collection failure. It never creates a post-hoc
prospective prediction.

## Stable fail-closed behavior

Before the finalization margin, missing prerequisites are retried. Near cutoff,
legal P0 is preserved as P0-only when R.O.S.H. is unavailable.

Stable reasons include:

```text
prospective_team_rating_unavailable
ten_heroes_incomplete
expected_positions_incomplete
formal_series_unavailable
stratz_network_failure
stratz_http_429
stratz_http_5xx
stratz_http_auth_failure
stratz_graphql_rate_limited
stratz_graphql_internal_server_error
rosh_evidence_invalid
statistics_cutoff_follows_availability
request_completed_after_cutoff
request_failed_after_cutoff
artifact_replay_completed_after_cutoff
cutoff_elapsed
target_result_already_available
```

Network retries are bounded at 15, 60, and 180 seconds, incorporate a sanitized
upstream `Retry-After`, and are clipped by the prediction cutoff and finalization
margin. Non-retryable failures produce P0-only while that can still be written
legally. They never produce P1-only.

## Persistence and immutability

Migration `20260807_0033` keeps the existing shadow ledger and adds:

- `prospective_rosh_collection_attempts`: append-only retry, terminal, Artifact,
  and idempotency evidence;
- `prospective_rosh_causal_audits`: one append-only post-settlement actual-start
  audit per shadow prediction.

The existing `(candidate_hash, match_id)` shadow identity remains unique. An
identical retry returns unchanged; different content for the same identity raises
an immutable conflict. UPDATE and DELETE are rejected for attempts, predictions,
settlements, and causal audits. Transactions remain rollback-safe.

## Settlement and actual-start causal audit

Settlement remains a separate append-only record and never updates the prediction.
Once an authoritative formal result exists, the operational pass stores the
existing shadow settlement, then compares:

```text
prediction created_at < authoritative actual map start
```

If the comparison fails, the prediction, Artifacts, and settlement are retained,
but `causal_eligible=false` with
`prediction_not_before_actual_start`. Such rows are excluded from prospective
paired support by `load_settled_rows`; they cannot enter a 20/100/200 evaluation.

## One-shot command

Dry-run checks PostgreSQL prerequisites without constructing a STRATZ client or
writing collection state:

```powershell
python scripts\run_prospective_rosh_collector.py `
  --database-url $env:DATABASE_URL `
  --dry-run `
  --acceptance-limit 5
if ($LASTEXITCODE -ne 0) { throw "prospective R.O.S.H. dry-run failed" }
```

Operational collection can scan a window or target one map:

```powershell
python scripts\run_prospective_rosh_collector.py `
  --database-url $env:DATABASE_URL `
  --match-id 1234567890 `
  --acceptance-limit 5 `
  --artifact-root dogfood-output\prospective-rosh-artifacts `
  --json-output dogfood-output\prospective-rosh-acceptance.json
if ($LASTEXITCODE -ne 0) { throw "prospective R.O.S.H. collection failed" }
```

## Independent worker

The worker is separate from the prospective Team Rating producer, odds collection,
and historical R.O.S.H. worker:

```powershell
python scripts\run_prospective_rosh_collector_worker.py `
  --database-url $env:DATABASE_URL `
  --batch-size 1 `
  --poll-seconds 30 `
  --acceptance-limit 5 `
  --artifact-root dogfood-output\prospective-rosh-artifacts
if ($LASTEXITCODE -ne 0) { throw "prospective R.O.S.H. worker failed" }
```

Restart recovery uses the append-only attempt and shadow identities. The worker
stops admitting new maps as soon as the configured 5–10 map cap is reached. It
continues only settlement and causal auditing for those admitted maps, then exits.
It never invokes or creates the 20-map gate.

## Per-map acceptance report

The one-shot and worker JSON include the first 5–10 maps in deterministic cutoff
order. Each row reports:

- whether P0 was created before cutoff;
- whether R.O.S.H. evidence was available before cutoff;
- paired or P0-only status;
- stable missing reason;
- request/response Artifact completeness;
- offline exact replay result;
- actual-start causal audit result;
- independent settlement status;
- idempotent retry result.

The acceptance run is complete only when the configured map count has been reached
and every admitted map has settlement, a completed causal audit, and an unchanged
idempotency retry. The collector does not treat these operational checks as model
effectiveness or deployment evidence.
