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
a fake formal target or a reconstructed prediction. It restores the latest
legal state for the frozen prospective seed, then applies only formal results
whose authoritative `first_usable_at` is no later than the lock time. It uses an
empty current roster so heroes, players, OCR, and live state cannot affect P0.

The accepted fixed configuration was taken from the final legal reconstructed
Team Rating run, not from the most frequent walk-forward grid choice:

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

The operational database contains zero prospective Team Rating seeds. The
read-only seed dry-run loaded 3,932 authoritative results and wrote zero rows,
but failed closed at input 97:

```text
rating cutoff cannot precede last_observed_at
previous match 8784700248 completed 2026-04-24T20:21:37Z
previous result usable 2026-07-13T12:26:09.977647Z
current match 8784597508 started 2026-04-24T18:23:21Z
current result usable 2026-07-13T12:26:10.250873Z
```

The authoritative availability order is causal, but these two backfilled
results arrive in the reverse of map time, and the current seed replay refuses
to move a team rating clock backward. This task does not change Team Rating or
select a different cutoff. Until a separately reviewed seed construction fix is
approved and a seed is explicitly frozen, the page returns the stable blocker
`prospective_team_rating_seed_unavailable`. It never falls back to a
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
- non-PostgreSQL CI suite: 819 passed, 17 skipped, 1,201 deselected;
- PostgreSQL integration suite: 130 passed;
- frontend suite: 222 passed;
- production frontend build passes;
- focused Python unit tests: locked/unlocked mapping, duplicate hero and
  position rejection, no-player mapping, P0 lineup/live-state independence,
  seed blocker, frozen candidate, and causal classification;
- isolated PostgreSQL 0034 round-trip: paired P0/P1, exact artifacts/replay,
  STRATZ-failure P0-only, idempotency, new mapping version preservation,
  append-only rejection, and absence of an official ID column in prediction;
- frontend component tests: no automatic official-v2 call, explicit
  confirmation, saved P0/P1 display, unlocked blocker, and seed blocker;
- Alembic has one head: `20260807_0034`.

The remaining operational blocker is the absent prospective Team Rating seed,
with the seed dry-run ordering failure above. No operational migration, seed
freeze, real collector, 5-map acceptance, 20-map gate, calibration, deployment,
causal promotion, or order action was performed.
