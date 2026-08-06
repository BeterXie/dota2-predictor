# Prematch Prediction Model v1 Design Contract

Status: PR-0 design contract. No production model is activated by this document.

Baseline commit: `0cf34756a42487c6dbe87c56705eccdc4b789e3b`

## Purpose

This contract defines the semantic, temporal, versioning, replay, lineage, and
validation boundaries for the first version of the new prematch prediction
pipeline. Later PRs must add a parallel pipeline. They must not reinterpret or
silently replace the existing Draft v3, official R.O.S.H., legacy prematch, or
live shadow paths.

The model exposes three distinct concepts:

```text
P_team
  Radiant win probability from pre-match team strength while the draft is unknown.

P_prematch
  Radiant win probability after all ten heroes and positions are known, with
  team strength as the offset and separately measured draft and R.O.S.H. deltas.

P_live(t)
  A future probability that also uses state observed after the map starts.
```

Only `P_team` and `P_prematch` are in the first delivery sequence. `P_live(t)`
is a separate future project. Existing 10/20/30/40/50-minute survival slices
must not be renamed or presented as a live win-probability model.

All directions use Radiant as positive:

```text
probability > 0.5      supports Radiant
probability < 0.5      supports Dire
feature value > 0      supports Radiant
feature value < 0      supports Dire
logit delta > 0        increases Radiant probability
logit delta < 0        decreases Radiant probability
```

The intended prematch composition is:

```text
team_base_logit
  + draft_residual_delta
  + official_rosh_delta
  + optional_cluster_delta
  = raw_prematch_logit
  -> out-of-sample calibrator
  = calibrated_prematch_probability
```

The official R.O.S.H. score is an input signal, not a probability. In
particular, `(50 + score) / 100` is forbidden, as is any fixed hand-written
percentage blend of independent scores.

## Frozen Existing Contracts

The following identities describe behavior that the parallel pipeline must not
change:

| Contract | Current identity |
| --- | --- |
| Draft feature version | `draft-features-v3` |
| Draft feature artifact | `draft-feature-artifact-v2` |
| Legacy Draft feature artifact | `draft-feature-artifact-v1` (audit only) |
| Draft model | `draft-logistic-l2-v1` |
| Draft model artifact | `draft-model-artifact-v2` |
| Legacy Draft model artifact | `draft-model-artifact-v1` (audit only) |
| Draft feature schema | `draft-feature-schema-v1` |
| Draft backtest | `strict-draft-walk-forward-v1` |
| Draft validation | `draft-input-lineage-v4` |
| Frozen Draft deployment | `frozen-pure-draft-deployment-v2` |
| Active R.O.S.H. profile | `stratz-rosh-web-2026-07-28-v2` |
| Active R.O.S.H. formula | `stratz-official-rosh/2026-07-28-v2` |
| R.O.S.H. serialization | `rfc8785-jcs/v1` |
| R.O.S.H. direction evidence | `rosh-direction-evidence/v1` |
| R.O.S.H. shadow candidate | `official-rosh-shadow-candidate/v1` |
| R.O.S.H. shadow strategy | `comeback-shadow-v6-official-rosh-direction` |
| Role assignment base | `role-assignment-v1` |
| Reconstructed roles | `role-assignment-v1-reconstructed-walk-forward` |
| Prospective roles | `role-assignment-v1-prospective` |

The active R.O.S.H. identity also binds the request profile hash, upstream
bundle hash, scorer source hash, canonical profile hash, and serialization
version. A source or identity mismatch must fail closed.

`prematch/scorer.py` remains a legacy heuristic probability path. None of the
new modules under `event_intelligence` may import it. The new model must not
reuse that module as an offset, fallback, calibrator, or missing-data default.

## New Version Registry

The first parallel implementation must use these exact identities:

```python
TEAM_RATING_VERSION = "team-rating-elo-v1"
TEAM_RATING_ARTIFACT_VERSION = "team-rating-artifact-v1"

DRAFT_RESIDUAL_FEATURE_VERSION = "draft-residual-features-v1"
ROSH_FEATURE_VERSION = "official-rosh-features-v1"
PREMATCH_FEATURE_VERSION = "prematch-features-v1"

PREMATCH_MODEL_VERSION = "prematch-offset-logistic-l2-v2"
PREMATCH_MODEL_ARTIFACT_VERSION = "prematch-model-artifact-v1"

PREMATCH_CALIBRATION_VERSION = "prematch-platt-v2"
PREMATCH_BACKTEST_VERSION = "prematch-walk-forward-v1"
PREMATCH_DEPLOYMENT_VERSION = "prematch-frozen-deployment-v1"
PREMATCH_VALIDATION_VERSION = "prematch-input-lineage-v1"
```

A formula, input schema, missing-data rule, training rule, or parameter-selection
change requires the corresponding version to change.

## Time And Cutoff Contract

All internal datetimes must be timezone-aware and normalized to UTC. Persisted
times use UTC ISO-8601. Naive datetimes are invalid.

Every input and result must distinguish these times:

| Field | Meaning |
| --- | --- |
| `observed_at` | When the underlying observation was made |
| `settled_at` | When a prediction outcome became settled |
| `first_usable_at` | Earliest time the exact stored fact could legally be consumed |
| `prediction_cutoff` | Latest legal input time for one target prediction |
| `training_cutoff` | Latest legal result and feature time for one training invocation |

For every target input:

```text
input.first_usable_at <= prediction_cutoff
```

For every training result:

```text
result_usable_at <= training_cutoff
```

In addition:

- The target match must never enter its own feature support or training corpus.
- Historical rows must be completed strictly before the target cutoff.
- A row at the cutoff is not an earlier row.
- Data added after the cutoff must not change an earlier snapshot or prediction.
- A later map in a series may use an earlier map only after the earlier result is
  actually usable.
- Parameter selection, calibration fitting, and evaluation must obey the same
  chronological boundary as model fitting.
- Missing timestamps or unverifiable availability fail closed. They do not
  become a neutral score or probability.

## Reconstructed And Prospective Isolation

`reconstructed_walk_forward` and `prospective` are different evidence modes,
not labels for the same data:

| Rule | `reconstructed_walk_forward` | `prospective` |
| --- | --- | --- |
| Purpose | Historical research and causal replay | Forward-collected validation and deployment evidence |
| Role version | Reconstructed suffix only | Prospective suffix only |
| Missing real archive time | May use the explicitly versioned reconstruction rule | Rejected |
| Target inputs | Earlier-only reconstructed facts | Facts genuinely usable at the target cutoff |
| Can authorize live use | Never by itself | Only after every prospective gate passes |

The modes must remain distinct enum values and persisted identities. A loader
must reject an assignment version whose suffix does not match its mode.
Reconstructed calibration may pass numerical calibration thresholds, but it
must still report that it cannot authorize a live strategy.

Official R.O.S.H. shadow behavior remains separate from the new prematch model.
The current shadow path can emit direction evidence or a rejection. It must not
manufacture calibrated probability, edge, stake multiplier, paper order, or
real-money execution.

### Cluster Evidence Route

The selected Cluster route is **prospective shadow only**. The published 7.41
static resource may be attached only to prospective targets at or after its
declared publication time. It must remain unavailable in
`reconstructed_walk_forward`, so historical M6 Cluster support is expected to
be zero.

Historical Cluster OOS evaluation is deferred unless a separately versioned
walk-forward resource is rebuilt from evidence available before every target
cutoff. A current static resource must never be projected backward merely to
increase reconstructed support.

## Schema, Artifact, Replay, And Lineage

Every new feature snapshot and artifact must have:

- an explicit version and fixed schema;
- canonical JSON under a declared serialization contract;
- SHA-256 identities for authoritative inputs and stored artifacts;
- `prediction_cutoff`, support, coverage, and an explicit missing reason;
- deterministic ordering and duplicate/conflict rejection;
- enough authoritative information to replay, or a claim that is verified by
  reloading the authoritative inputs;
- runtime/library versions where they can affect deterministic training.

The existing Draft contracts illustrate the required separation:

- Draft v3 feature artifacts store bounded claims and require external target
  and history authority for verification.
- Draft model v2 artifacts embed a canonical training corpus and are refit
  during verification; derived parameters must match canonical bytes.
- Legacy v1 feature and model artifacts remain audit-only and are not
  deployable.
- Frozen deployments bind all five horizon model hashes, calibration hashes,
  training cutoff, dependency fingerprint, and dependency revision.

New database-backed artifacts must be immutable and idempotent. An exact repeat
is unchanged; a conflicting write for the same identity fails. Lineage must
bind the input snapshot hash, artifact fingerprint, dependency fingerprint,
dependency revision, and validation version. A relevant dependency change at
or before a prediction cutoff invalidates the proof. A change wholly after the
cutoff must not retroactively alter an earlier prediction.

PR-0 adds no tables or migrations. These persistence requirements apply only
when the later PR that owns the corresponding schema is implemented.

## Model And Validation Gates

The pipeline is evaluated chronologically out of sample. Complexity or an
official label is not evidence of predictive value.

The required ablation set is:

```text
M0 constant_50
M1 radiant_prior
M2 team_only
M3 team_plus_draft
M4 team_plus_rosh
M5 team_plus_draft_rosh
```

Report support, eligible and insufficient targets, coverage distribution,
Brier score, log loss, ECE, AUC, and accuracy for each slice. Incremental
comparisons use paired bootstrap samples clustered by `series_id`:

```text
M3 - M2
M4 - M2
M5 - M3
M5 - M4
```

A component may enter the default model only when either its log-loss delta or
Brier delta is below zero and that metric's paired 90% confidence interval has
an upper bound below zero. Otherwise it remains explanatory only and is marked
`no_incremental_value`.

The base calibration gate is:

```text
evaluation support >= 100
Brier < 0.25
log loss < ln(2)
ECE <= 0.10
ECE 90% bootstrap upper <= 0.15
```

A default deployment candidate must also have a paired delta in log loss or
Brier, relative to `team_only`, whose 90% confidence interval is wholly below
zero.

Before `prospective_validated`, all of these must hold:

```text
prospective settled support >= 200
at least 5 formal events
at least 2 patches
no single event contributes more than 40%
base calibration gate passes
incremental gate relative to team_only passes
```

Until then, the only allowed states are `unsupported`, `failed`, `provisional`,
`reconstructed_only`, and `shadow_collecting`.

## Non-Goals And PR Boundaries

The first model delivery does not include:

- automatic or real-money betting, Kelly sizing, or stake multipliers;
- a live-state probability model;
- post-start inputs in a prematch model;
- global-history feature computation followed by a later train/test split;
- a neural-network replacement for the interpretable baseline;
- legacy `prematch/scorer.py` as part of the new probability path;
- replacement of Draft v3 with Draft v4;
- presentation of reconstructed evidence as prospective validation;
- claims of accuracy or betting advantage before all gates pass.

PR-0 itself is limited to this contract, boundary tests, and a documentation
index link. It does not implement Team Rating, alter a prediction formula,
change the official R.O.S.H. scorer, change Draft v3 output or its model
artifact, add a database migration, modify the frontend, or modify live betting.

The next stage is the pure Team Rating algorithm and its replayable artifact.
Database persistence, walk-forward reporting, Draft Residuals, R.O.S.H. feature
projection, model fusion, calibration, UI, and prospective shadow collection
remain owned by their later stages.
