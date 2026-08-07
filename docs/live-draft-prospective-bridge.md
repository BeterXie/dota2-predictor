# Live Draft Prospective Bridge

## Status

This change connects the existing live match detail page to an immutable,
research-only Team Rating/R.O.S.H. shadow. It does not add a page, route in the
frontend router, hero selector, OCR upload flow, scorer, calibration path,
deployment path, or order path.

The operational database was not migrated. It remains at `20260807_0033`; the
new unique repository head is `20260807_0034` and was exercised only in an
isolated PostgreSQL database.

## Existing path audit

`MatchWorkspace` loads a RayBet match detail and renders the existing
`LiveDataControls`. That control already owns the complete manual draft flow:

```text
RayBet/live match
-> existing Vision/OCR context and canonical team resolution
-> existing hero picker and optional player selection
-> POST /api/monitor/.../draft-mapping
-> append-only LiveDraftMapping version
```

`live_draft_mappings` stores ten rows under
`raybet_match_id + map_number + version`. Each version records side, expected
position, canonical hero/team identity, nullable player identity, lock state,
operator, and `created_at`. The table rejects updates/deletes. A correction is a
new version, never a mutation.

The existing Vision pipeline supplies captured frames and recognized live state.
It can assist the operator, but none of its kills, economy, XP, tower, Roshan,
clock, odds, or gameplay observations are model inputs. Player IDs remain
optional and are not R.O.S.H. pure-lineup inputs.

Before this change, `MatchWorkspace` automatically called
`/api/prematch/rosh-analysis` with profile
`stratz-rosh-web-2026-07-28-v2` after a mapping was locked. That official-v2
analysis is not the frozen prospective candidate. The automatic call has been
removed. The prematch endpoint itself remains available for its existing
workspace; the live draft bridge reuses its lower-level STRATZ transport,
exact-byte artifact store, legacy normalizer/scorer, and exact replay code.

Official postmatch identity is already represented by
`strict_live_map_mappings`, `settlement_reconciliations`, and `map_results`.
The bridge prediction never stores a RayBet ID as a Dota match ID. Settlement is
appended only after `map_results` provides the existing strict mapping and
official Dota identity.

## Locked mapping contract

A prediction requires one locked version with exactly ten slots, positions 1-5
once per side, ten unique canonical heroes, and two distinct canonical teams.
The canonical mapping payload is content hashed and bound to the prediction.
`created_at` is preserved as both mapping creation time and
`operator_locked_at`; `created_by` is the operator identity.

The page requires the operator to confirm:

> 本次预测只使用已锁定阵容，未使用击杀、经济、经验、防御塔、肉山或其他游戏内状态。

The confirmation time and identity are persisted. Repeating the same mapping
version returns the existing prediction. A changed draft creates a new mapping
version and cannot overwrite the old prediction.

## Team Rating P0

The P0 cutoff is `operator_locked_at`. The live-specific builder does not create
a fake formal target or a reconstructed prediction. For every P0 it rebuilds
from the frozen seed authority plus every post-seed formal result whose
authoritative `first_usable_at` is no later than the lock time. It uses an empty
current roster so heroes, players, OCR, and live state cannot affect P0.

The accepted fixed configuration is the last causally selected fixed config,
not a prospectively validated config and not the most frequent walk-forward
grid choice:

```json
{
  "config_version": "team-rating-elo-v1",
  "inactivity_half_life_days": 180.0,
  "initial_rating": 1500.0,
  "k_factor": 24.0,
  "radiant_side_logit": 0.041210268646663106,
  "roster_carry_power": 2.0,
  "scale": 200.0
}
```

The tracked artifact is
`event_intelligence/resources/team_rating_accepted_config_v1.json`; its
canonical configuration hash is
`b527319ab1035d6cae6550820cd0854b467f845537d033909b4f2e45e706c19a`.

Operational lineage inspection found exactly one reconstructed run with that
config hash: run
`30142d714950c803d13ef70cfebce40de1dc6263dda35b3e8fa406e5a278652c`.
It is the latest trained reconstructed selection, with target/training cutoff
`2026-08-04T16:36:58Z`. Its last ordered training source was available at
`2026-08-04T16:35:10.684150Z`; the target result became available only at
`2026-08-04T17:50:57.346903Z`. This establishes causal parameter-selection
lineage only. It does not establish prospective effectiveness.

### Authority order and rating replay order

The seed and every prospective P0 keep two different canonical orders:

```text
authority manifest:
result_usable_at -> started_at -> match_id

Team Rating replay:
started_at -> completed_at -> match_id
```

The authority manifest proves that every included result was available by the
cutoff. Only after that validation is complete is the same set reordered for
Elo replay. Replay starts from an empty state and passes one uniform seed or P0
cutoff to the unchanged Team Rating math. It never substitutes availability for
match time, clamps timestamps, skips a late backfill, or admits a result that
became available after the cutoff.

The seed artifact stores both manifests and both hashes. Exact verification
rechecks cutoff eligibility, canonical replay order, per-team chronology, the
empty-state replay, config identity, final state hash, and content-addressed seed
hash. A team's next map must not start before its previous map completed;
violations fail closed as `overlapping_team_match_chronology`. Simultaneous maps
between different teams are valid.

Prospective runs use the simple full-rebuild path. They combine the immutable
seed source manifest with the complete cutoff-eligible post-seed authority set,
then replay the cumulative set chronologically from an empty state. Previous P0
snapshots are not incremental bases. Consequently, a newly available historical
result that predates an existing state automatically causes a deterministic full
rebuild and cannot silently corrupt the rating clock.

### Operational read-only seed dry-run

The operational database remains at `20260807_0033` and still contains zero
prospective Team Rating seeds. A `READ ONLY` transaction at cutoff
`2026-08-07T09:22:08.309653Z` produced:

```text
available authority results: 3932
excluded after cutoff: 0
duplicates: 0
adjacent availability-order chronology inversions: 480
replay maps: 3932
distinct teams: 102
team chronology overlaps: 0
first replay match: 8142981335 at 2025-01-24T09:00:52Z
last replay match: 8930736914 at 2026-08-05T14:55:22Z
source manifest hash: 25c820cda4839f04240e87bfc5c91d6033df74fb9cfc41f0c68e5878c9c5cd19
replay order hash: 1303f6f4f5a0204eaad7c8ae0914033d0fe3268cb6f6187e1734d6037c6d5b7f
final state hash: 7148c4b27525a894541099487f26ab32d05b74874420992acdd8d7a1368f1259
prospective seed hash: 3c2bd8d45ceefae1818789acf5467b5f70f4feb04ea9fae765ea2b79c0378f3f
repeated replay identical: true
database writes: 0
```

Matches `8784700248` and `8784597508` are present in both manifests with their
original timestamps. Input 97 no longer fails, and neither match is skipped.
Reversing the source input produced the same manifest, replay, state, and seed
hashes. The 480 inversion count is the number of adjacent rows in availability
order whose chronological key moves backward; it is diagnostic only and does
not change authority eligibility.

No seed was frozen. Until a separate explicit freeze occurs, the page still
returns `prospective_team_rating_seed_unavailable` and never falls back to a
reconstructed P0.

## Frozen R.O.S.H. P1

Once P0 exists, the bridge strictly loads:

```text
candidate hash: 84c4506f63b7c5b745b32373b0cb405383f837c60eae3231cc3d688a0b36e09d
profile: legacy-dematus-pure-rosh-prospective-v1
formula: logit(P1)=logit(P0)+beta_rosh*standardized_pure_rosh_score
official_v2_compatible: false
```

It requests `heroes_meta_positions`, `hero_stats_by_time_bracket`, and
`synergy` at the lock cutoff; archives exact request/response bytes as canonical
gzip artifacts; hashes both manifests; and calls the existing frozen normalizer,
pure scorer, and offline replay. It stores pure score, standardized score,
positive-beta logit contribution, P0, and P1. A transport, artifact, or replay
failure persists P0-only with
`prospective_rosh_evidence_unavailable`; P1-only is impossible.

## Causal semantics

The draft lock is the statistics/model cutoff, not a scheduled prematch time.
Initial causal status also records the operator confirmation, optional game
clock, optional Vision frame time, draft/game-state marker, and whether live
state was used:

- `eligible`: explicit draft/pre-game marker, no positive clock, and no live
  state input;
- `unverified`: no live input is declared, but timing evidence is incomplete;
- `ineligible`: gameplay/live state is present or a positive clock is observed.

After official mapping and result authority exist, settlement appends an
independent causal check against actual map start. That check can exclude a
late prediction, but cannot by itself promote an initially unverified record.
Only records that pass both draft-lock evidence and the post-settlement check
can enter 20/100/200 prospective evaluation.

## Why migration 0034 is required

Migrations 0031-0033 require an official numeric `match_id` and `series_id`.
They cannot represent a prediction created from only a RayBet live map and a
manual mapping version without inventing an official identity. Migration 0034
therefore adds only:

- `live_draft_prospective_predictions`;
- `live_draft_prospective_settlements`.

The prediction table binds mapping, Team Rating seed/config/state/result
manifests, frozen candidate, exact R.O.S.H. evidence, confirmation, causal
evidence, and canonical artifact hash. The settlement table links later through
the existing strict mapping and `map_results`. Both are append-only. PostgreSQL
insert guards validate the ten-slot locked mapping/hash, seed and candidate
identity, artifact hash, official-v2 incompatibility, and settlement authority.

## Page behavior

The existing live match page now shows:

- unlocked: `请先确认并锁定阵容`;
- locked/unpredicted: the fixed confirmation plus `生成阵容预测`;
- predicted: immutable mapping version, P0, optional P1, pure score, causal
  status, and creation time;
- corrected mapping: an explicit notice that the old prediction remains bound
  to the old version.

No new frontend route, workspace, selector, or OCR UI was introduced.

## Verification

- Ruff: full repository passes;
- non-PostgreSQL CI suite: 830 passed, 17 skipped, 1,201 deselected;
- PostgreSQL integration suite: 130 passed;
- frontend suite: 224 passed;
- production frontend build passes;
- prospective replay tests cover availability/chronology agreement, late
  backfill, the two operational regression matches, cutoff and target exclusion,
  duplicate authority, deterministic ties, overlap rejection, full rebuild,
  exact replay, tamper rejection, unchanged reconstructed replay, fixed config
  identity, P0 input isolation, and the no-seed blocker;
- isolated PostgreSQL 0034 round-trip: paired P0/P1, exact artifacts/replay,
  STRATZ-failure P0-only, idempotency, new mapping version preservation,
  append-only rejection, and absence of an official ID column in prediction;
- frontend component tests: no automatic official-v2 call, explicit
  confirmation, saved P0/P1 display, unlocked blocker, and seed blocker;
- Alembic has one head: `20260807_0034`.

### Frontend count audit

The earlier drop from 224 to 222 was real: it was not a skip or test-discovery
failure. Removing the automatic official-v2 call removed five tests and added
three, for a net loss of two. The removed tests were:

- `reuses the prematch Rosh analysis for a complete locked live draft`;
- `reuses a matching RayBet record and shows every linked official ID`;
- `does not call external Rosh analysis from replay`;
- `does not call external Rosh analysis for an ended match`;
- `reports STRATZ rate limiting without falling back to an unknown direction`.

The automatic-call, replay, and ended-match expectations are intentionally
retired because `MatchWorkspace` no longer imports or invokes the official-v2
create API. They are replaced by explicit-confirmation, frozen paired P0/P1,
and stable seed-blocker tests. The still-valid linked official-ID behavior is
restored as a read-only evidence test. The old automatic 429 test is replaced by
a P0-only test that proves Team Rating remains visible with
`prospective_rosh_evidence_unavailable`. The full frontend suite is therefore
back to 224 tests without reinstating automatic official-v2 execution.

The remaining operational blocker is only the absent explicitly frozen
prospective Team Rating seed. No operational migration, seed freeze, real
collector, 5-map acceptance, 20-map gate, calibration, deployment, causal
promotion, or order action was performed.
