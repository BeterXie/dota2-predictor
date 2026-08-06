# R.O.S.H. Historical Walk-Forward Validation

Date: 2026-08-06

Base: `master` merge commit `676e427133ae2b33ceb1b5f672dc7fe864060e56`

Branch: `codex/rosh-historical-walk-forward-reconstruction`

## Decision

The 20-map temporal-semantics gate failed. The STRATZ aggregate responses do
not expose the included match IDs or source timestamps for any of the three
queried operations. Consequently, this audit cannot prove that `week` is a
strict historical upper bound or that target and future maps are excluded.

Per the stop rule, this branch does not run the 100-map pilot, full historical
reconstruction, or Team Rating-only versus Team Rating + reconstructed
R.O.S.H. OOS. It does not modify Team Rating, Draft, Cluster, Prematch,
Calibration, Deployment, or prospective state.

## Boundary and Data Contract

This path is permanently labelled:

```text
reconstructed_walk_forward
research_only
not_prospective
not_deployment_eligible
```

Each observation records three distinct timestamps:

| Field | Meaning |
| --- | --- |
| `queried_at` | Actual wall-clock time at which the STRATZ request was sent. |
| `statistics_cutoff` | Historical upper bound passed through the STRATZ `week` variables. |
| `prediction_cutoff` | Prematch cutoff for the target map, derived from its stored start time. |

Every request enforces `statistics_cutoff <= prediction_cutoff`. The audit does
not query a target match, does not read its result or postmatch statistics, and
does not use historical match `endDateTime`, the current time, a legacy minute
table, or a saved final score as scorer input.

For every map, it issues one batch at `prediction_cutoff - 7 days`, one at
`prediction_cutoff`, and an exact repeat at `prediction_cutoff`. Each batch
contains:

- `heroes_meta_positions`;
- `hero_stats_by_time_bracket`;
- `synergy`.

Request and response bytes are stored unchanged in a content-addressed gzip
artifact store. Canonical normalized statistics are stored separately. Offline
replay loads and verifies the archived gzip and content hashes, then invokes
the repository scorer using only the ten expected-position heroes and the
archived normalized statistics. It does not read the current official-v2
profile or current hero statistics.

## Sample Coverage

The deterministic sample contains 20 maps from 11 events, patches 56 through
60, and prediction cutoffs from `2025-01-31T13:49:57Z` through
`2026-05-16T13:25:09Z`.

| Selection case | Maps |
| --- | ---: |
| same-series Map 1/2/3 | 3 |
| week boundary | 4 |
| multiple patches | 5 |
| multiple events | 1 additional selected map |
| consecutive matches | 7 |

The same-series sequence is series `945280`, maps `8153368922`, `8153470879`,
and `8153569636`. The full sample spans:

```text
1win-essence-i-2026
clavision-snow-ruyi-2025
dreamleague-s25-2025
dreamleague-s26-2025
dreamleague-s28-2026
dreamleague-s29-2026
esl-one-raleigh-2025
ewc-dota2-2025
fissure-playground-1-2025
pgl-wallachia-s4-2025
the-international-2025
```

## Real STRATZ Run

The configured STRATZ token was used for the phase-one run. Large artifacts
and the JSON report remain under ignored `dogfood-output` paths and are not
committed.

| Result | Support |
| --- | ---: |
| audited maps | 20 |
| archived request artifacts | 60 |
| archived response artifacts | 60 |
| archived normalized-statistics artifacts | 60 |
| successful offline exact replays | 9 observations |
| maps with all three replays successful | 3 |
| maps with incomplete replay | 17 |
| repeated raw-response changes | 0 |
| maps with complete temporal provenance | 0 |
| maps passing the gate | 0 |

All 20 maps failed with:

```text
aggregate_response_lacks_temporal_match_provenance = 20
```

Seventeen maps additionally returned normalized aggregates that could not
produce a score at any of the three points:

```text
offline_exact_replay_incomplete = 17
archived normalized statistics produced no score = 17 per observation label
```

Only these maps produced a score from all three archived normalized inputs:

| Match | Event | Patch | Cutoff |
| ---: | --- | ---: | --- |
| 8698613744 | dreamleague-s28-2026 | 59 | 2026-02-20T14:26:00Z |
| 8802892505 | 1win-essence-i-2026 | 60 | 2026-05-08T16:37:37Z |
| 8813239365 | dreamleague-s29-2026 | 60 | 2026-05-16T13:25:09Z |

For those three maps, the archived normalized input changed between the
seven-day-earlier point and the prediction cutoff, while the repeated cutoff
response was byte-identical. This demonstrates that historical query values
can vary by `week`; it does not prove that the value is bounded by that time.

## Temporal-Semantics Findings

The aggregate response shapes provide only aggregate hero/position/time-bracket
and synergy values. They do not identify the matches or timestamps included in
those aggregates. Therefore the run cannot establish any of these required
invariants:

- the target match is absent from its own statistics;
- later maps do not change an earlier cutoff input;
- series Map 1 excludes Map 2 and Map 3;
- each operation excludes every match after `statistics_cutoff`.

The implementation fails closed when any operation lacks both match and time
provenance. It also rejects an explicit source timestamp after its statistics
cutoff and records changes in repeated requests using the exact archived
response-byte hash rather than only the normalized payload.

## Stop Decision

The `week` parameter cannot be certified as a strict historical cutoff from
the evidence returned by these endpoints. The 20-map gate is therefore
`false`. No 100-map pilot, full reconstruction, paired OOS comparison, model
training, calibration change, or deployment action is authorized by this
result.

## Reproduction

```powershell
$env:DATABASE_URL = "postgresql+psycopg://dota2:dota2_local@localhost:5432/dota2_predictor"
$env:STRATZ_API_TOKEN = "<token>"
python scripts/audit_rosh_historical_walk_forward.py `
  --artifact-root dogfood-output/rosh-historical-walk-forward-artifacts `
  --json-output dogfood-output/rosh-historical-walk-forward-20-map.json
```

The source query runs inside an explicit PostgreSQL `READ ONLY` transaction.
There is no persistence, pilot, full-rebuild, training, calibration, or
deployment flag.
