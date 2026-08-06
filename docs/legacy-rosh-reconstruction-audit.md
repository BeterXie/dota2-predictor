# Legacy R.O.S.H. Reconstruction Audit

Date: 2026-08-06
Base: `master` merge commit `9b1d8729b9e8973b0e8ae827579a051a9a0000e6`
Branch: `codex/legacy-rosh-reconstruction-audit`

## Scope and Decision Boundary

This audit classifies the 561 legacy historical R.O.S.H. rows that have ten
heroes and complete expected positions but no strict official-v2 authority
lineage. It does not convert legacy evidence into official-v2 evidence and does
not modify the authority bridge, Team Rating, Draft, Cluster, Prematch,
Calibration, Deployment, or prospective state.

The audit is offline and read-only:

- the PostgreSQL transaction is explicitly `READ ONLY`;
- only persisted legacy rows, formal hero/position identity, and frozen
  repository code are consumed;
- no STRATZ, OpenDota, current official-v2 profile, current hero statistics, or
  network client is read;
- the source fingerprint before and after the run was identical:
  `296babc68a6f6d09ef89e0d8695d87f1322fc0c629d5378db4af469c2e6890d7`.

## Frozen Legacy Contract

| Item | Observed value |
| --- | --- |
| formula version | `dematus-rosh-0e1e6651dd932055dee69c4fb44435774f619793` (561/561) |
| evidence schema | unversioned legacy historical R.O.S.H. shape (561/561) |
| complete formula entry | `prematch.stratz_rosh.score_rosh_picks` |
| saved `pure_minute_table` | complete under the legacy storage contract (561/561) |
| saved raw normalized analysis | unavailable (0/561) |
| saved source material | response hashes only; response bodies/stat rows are absent |
| evidence hash | valid (561/561) |
| `source_week` and `source_as_of` | after prediction cutoff (561/561) |

The complete frozen entry requires position picks plus these normalized raw
analysis groups:

```text
heroes_meta_positions
hero_stats_by_time_bracket
synergy
```

The evidence stores a derived minute table and its component outputs, but not
the raw statistics that produced that table. Reading the final
`win_rate_graph` from the stored table would only replay a saved derivative;
it would not independently execute `score_rosh_picks`. This audit therefore
does not count the minute table as complete formula input and never reads the
stored `pure_lineup_score` as a replay result.

## Strict Funnel

| Stage | Support |
| --- | ---: |
| candidate | 561 |
| evidence hash valid | 561 |
| legacy formula available | 561 |
| required inputs complete | 0 |
| independent replay succeeded | 0 |
| cutoff safe | 0 |
| exact legacy replayable | 0 |

The first replay blocker is:

```text
raw_formula_inputs_unavailable = 561
```

An independent cutoff audit also found:

```text
source_as_of_after_prediction_cutoff = 561
source_week_after_prediction_cutoff = 561
```

## Classification

| Classification | Support | Interpretation |
| --- | ---: | --- |
| A `exact_legacy_replayable` | 0 | No row has both complete raw inputs and safe timing. |
| B `partially_replayable` | 0 final | All 561 have partial derived evidence, but D takes precedence. |
| C `score_only` | 0 | Every candidate has more than a scalar score and hash. |
| D `cutoff_unsafe` | 561 | Both saved source timestamps are later than the prediction cutoff. |

Ignoring cutoff classification only, all 561 rows are partially replayable:
their evidence hashes, formula version, response hashes, and derived minute
tables are intact. That is not sufficient for independent formula replay and
does not make them prematch OOS evidence.

## Per-Row Output

`scripts/audit_legacy_rosh_reconstruction.py` emits one JSON record per
candidate with:

- `match_id`, `score_key`, `formula_version`, evidence schema;
- prediction cutoff, `source_week`, `source_as_of`;
- evidence-hash, formula, minute-table, and required-input status;
- recomputed score, stored score, absolute difference;
- classification, missing reason, and unsafe reason;
- event and patch identifiers for a future positive-support scope report.

The local full JSON output is written to
`dogfood-output/legacy-rosh-reconstruction-audit.json` and is intentionally not
committed.

## Invariant Verification

The replay function accepts only one evidence object, frozen formula version,
and the two expected-position hero tuples. Tests verify that:

- deleting or changing the target result does not change replay;
- deleting or changing postmatch/current hero statistics does not change
  replay;
- appending future matches does not change replay;
- changing the current official-v2 profile does not change replay;
- a blocked socket connection is never invoked;
- repeated replay of the same evidence is deterministic;
- a synthetic evidence envelope with all raw inputs can execute the frozen
  formula exactly, while minute-table-only evidence remains partial;
- invalid hashes and late source timestamps fail closed.

## Stop Decision

Final A support is `0`. Per the stop rule, this branch does not run Team
Rating-only versus Team Rating + Legacy R.O.S.H. v1 OOS and does not train or
modify any model, probability, calibration, Deployment, or prospective state.

## Reproduction

```powershell
$env:DATABASE_URL = "postgresql+psycopg://dota2:dota2_local@localhost:5432/dota2_predictor"
python scripts/audit_legacy_rosh_reconstruction.py `
  --json-output dogfood-output/legacy-rosh-reconstruction-audit.json `
  --markdown-output dogfood-output/legacy-rosh-reconstruction-audit.md
```

There is no persistence flag. The command sets the database transaction to
read-only before loading any source evidence.
