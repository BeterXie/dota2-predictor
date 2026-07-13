# Dota 2 Strict Event Intelligence and Shadow Validation Implementation Plan

## Goal

Implement the approved strict-event archive, role-aware scoring, team-state
profiles, draft walk-forward evaluation, RayBet shadow strategy integration,
and transactional email notifications while preserving the passive Edge
extension and permanently disabled real execution boundary.

## Verified Starting State

- Edge extension tests: 26/26 pass.
- Predictor focused suite: 86/86 pass.
- Strict target data: 251/526 maps; 275 missing.
- Target local coverage: PGL S8 0/119, DreamLeague S29 185/185, BLAST SLAM
  VII 66/102, EWC 2026 0/120.
- Thirty existing maps are outside strict scope and must be excluded.
- The database has no strict event registry, raw artifact versions, role
  assignments, player map scores, team map-state labels, draft model runs,
  outbox, or mail delivery records.
- Existing draft curves and team/player profiles are prototypes, not the
  approved models.
- Existing live tables have odds and vision observations but zero browser
  events, shadow attempts/orders, and settlements in the primary database.
- The localhost companion is not currently listening on port 8765.
- The predictor worktree contains pre-existing modified and untracked work.
  Never reset, broadly format, or stage it.

## Fixed Boundaries

- Predictor repository: `C:\Users\59908\dota2-predictor`
- Extension: `C:\Users\59908\dota2-ad-assistant\edge-extension`
- Database: `C:\Users\59908\dota2-predictor\data\dota2.db`
- OpenDota is the primary completed-match source.
- STRATZ is optional completed-match fallback/cross-check only.
- PandaScore is disabled and unnecessary.
- Formal maps come only from the manually approved registry.
- Generated databases, raw responses, models, reports, evidence frames, and
  credentials are not committed.
- New historical scoring code never reads the legacy estimated 10-minute
  assist/death fields as source facts.
- Existing user changes are preserved. Implementation commits are not created
  unless separately requested.
- Real execution remains structurally unavailable and always returns
  `execution_disabled`.

## Task 1: Additive Intelligence Schema and Registry

### Files

- Add `event_intelligence/__init__.py`
- Add `event_intelligence/models.py`
- Add `event_intelligence/storage.py`
- Add `event_intelligence/registry.py`
- Add `tests/test_event_registry.py`
- Add `tests/test_intelligence_storage.py`

### Work

1. Define frozen records and enums for event scope, ingest state, component
   readiness, artifact provenance, role purpose, and reconciliation.
2. Add idempotent schema initialization for `event_registry`,
   `event_candidates`, `raw_source_artifacts`, and `match_ingest_status`.
3. Add later-component tables up front so migrations have one authoritative
   version: role assignments, player facts/scores, team map states/profiles,
   draft model runs/predictions, notification outbox, and service health.
4. Enable foreign keys, WAL, and a bounded busy timeout for every connection.
5. Seed exactly the four approved events with league IDs, prize/tier/stage
   facts, evidence references, and scope version.
6. Represent BLAST internal LCQ inclusion and explicit qualifier/Division 2,
   exhibition, forfeit, and void-remake exclusions.
7. Make candidate discovery audit-only; no unapproved candidate can enter a
   formal query.
8. Store the EWC 120/121 discrepancy as reconciliation pending.

### Verify

- `python -m pytest -q tests/test_event_registry.py tests/test_intelligence_storage.py`
- A temporary database initializes twice without changing seeded identities.
- Formal event queries return IDs `19543`, `19696`, `19101`, `19785` only.
- Candidate rows never appear in the formal-map eligibility view.

## Task 2: Content-Addressed Raw Archive and Exact Facts

### Files

- Add `event_intelligence/raw_archive.py`
- Add `event_intelligence/opendota.py`
- Add `event_intelligence/facts.py`
- Add `tests/test_raw_archive.py`
- Add `tests/test_completed_match_facts.py`
- Modify `fetch/db.py` only where needed to make completed-match normalization
  transaction-safe without changing existing callers.

### Work

1. Reuse the existing rate-limited OpenDota HTTP client through a narrow
   adapter that returns response bytes, parsed JSON, sanitized request identity,
   receipt time, and status metadata.
2. Store gzip artifacts under a source/date/match path keyed by SHA-256.
3. Deduplicate identical content while retaining each observation/provenance
   row and `first_usable_at`.
4. Validate match identity before any normalized write.
5. Extract source-exact facts with `NULL` for absence. Never default missing
   vision, control, damage-taken, ward, stack, rune, Roshan, or early-event
   fields to zero.
6. Store ten-player completeness, ten-pick completeness, timeline coverage,
   objective coverage, source schema fingerprint, and component readiness.
7. Make legacy match/team/player/timeline replacement one transaction. Remove
   helper commits only inside an explicit transaction while retaining current
   standalone behavior.
8. On failure, keep the immutable raw artifact and mark normalization retryable;
   never leave half-replaced match children.

### Verify

- `python -m pytest -q tests/test_raw_archive.py tests/test_completed_match_facts.py`
- Identical payloads create one artifact and multiple observations without
  overwriting bytes.
- A changed delayed parse creates a second version and upgrades readiness only
  when more complete.
- Injected child-table failure rolls back the entire normalized replacement.
- Credential/token scans of artifact metadata return no secrets.

## Task 3: Incremental Ingestion, Retry Scheduler, and Reconciliation

### Files

- Add `event_intelligence/ingest.py`
- Add `event_intelligence/scheduler.py`
- Add `scripts/run_strict_event_ingest.py`
- Add `tests/test_strict_ingest.py`
- Add `tests/test_ingest_scheduler.py`

### Work

1. Discover match IDs only from approved registry leagues and stage/date
   boundaries.
2. Record every discovered ID before fetching detail.
3. Treat existing legacy rows as candidates for raw re-fetch and completeness
   audit; an arbitrary player row is no longer enough to call a map complete.
4. Implement retry times at 15 minutes, 1 hour, 6 hours, 24 hours, and 72 hours.
5. Poll active events every 15 minutes and rescan the latest seven days daily.
6. Persist attempts, sanitized errors, next retry, first usable version, and
   reconciliation state so restart is deterministic.
7. Provide `--once`, `--event`, `--match`, `--active`, and `--reconcile`
   commands. Normal operation needs no STRATZ token.
8. Enforce bounded concurrency and OpenDota rate limits.

### Verify

- `python -m pytest -q tests/test_strict_ingest.py tests/test_ingest_scheduler.py`
- A fake clock reproduces the exact retry sequence across restart.
- Unapproved league payloads remain candidates and write no formal map.
- A recent unparsed map is retried; a complete unchanged map is not reinserted.

## Task 4: Backfill and Prove the Strict 526-Map Baseline

### Work

1. Back up the live SQLite database through the SQLite backup API.
2. Initialize the registry/schema against `data/dota2.db`.
3. Re-fetch and raw-archive all four approved league match lists.
4. Fetch the missing 275 details, then re-fetch incomplete local target maps to
   establish source versions and exact fact coverage.
5. Leave the thirty excluded local maps in legacy storage but exclude them from
   all formal views, scores, models, and reports.
6. Reconcile league counts and list exact missing/duplicate IDs. Keep EWC
   pending if public 121 cannot be mapped to a valid OpenDota match.
7. Generate a machine-readable coverage report by event and component.

### Verify

- Formal map count is 526 or every difference has an explicit reconciliation
  row and exact match ID/evidence reason.
- Target player completeness reports 10 players per scoreable map.
- Draft and gold timeline coverage are counted separately, never inferred.
- The excluded thirty maps contribute zero formal rows.
- `PRAGMA integrity_check` returns `ok`.

## Task 5: Separate Observed and Expected Positions

### Files

- Add `event_intelligence/roles.py`
- Add `tests/test_role_assignment.py`

### Work

1. Implement `observed_position` for post-match scoring with priority: audited
   roster, earlier 20-map pattern, then one-map maximum-weight assignment.
2. Use lane identity and exact 10-minute facts for the one-map assignment;
   separate positions 4/5 using exact economy, last hits, roaming, wards, and
   stacks only when present.
3. Implement `expected_position` using only audited roster and maps completed
   and usable before the prediction cutoff.
4. Store purpose, source, confidence, cutoff, input hash, and version.
5. Never read final GPM for either assignment.
6. Exclude confidence below 0.7 from positional rankings/features while
   retaining an auditable low-confidence row.

### Verify

- `python -m pytest -q tests/test_role_assignment.py`
- A target-map 10-minute mutation can change `observed_position` but never
  `expected_position` or an earlier prediction.
- Final-GPM permutations do not change either role assignment.
- Five players receive a one-to-one assignment or explicit unknown status.

## Task 6: Five-Role Player Scoring

### Files

- Add `event_intelligence/player_scoring.py`
- Add `event_intelligence/benchmarks.py`
- Add `scripts/score_strict_event_players.py`
- Add `tests/test_player_scoring.py`
- Add `tests/test_player_benchmarks.py`

### Work

1. Encode the five approved weight sets as immutable versioned configuration.
2. Convert metrics to per-10-minute, team-share, opportunity, or per-economy
   rates according to component meaning.
3. Build earlier-only median/MAD benchmarks by patch, position, duration, and
   event strength, with documented fallbacks when a cell is sparse.
4. Apply opponent, hero matchup, and draft expectation as explicit residual
   adjustments.
5. Produce `execution_score` and `result_adjusted_score`, cap result adjustment
   at +/-5, and shrink missing coverage/role uncertainty toward 50.
6. Persist every raw component, normalized component, weight, coverage,
   benchmark cutoff/hash, score version, and explanation.
7. Keep low-confidence roles out of position leaderboards.

### Verify

- `python -m pytest -q tests/test_player_scoring.py tests/test_player_benchmarks.py`
- Each role's weights total exactly 1.0.
- Missing control/vision/damage-taken lowers coverage rather than substituting
  another metric or zero.
- Changing a future match cannot alter an earlier score.
- Recomputing a version from stored inputs produces byte-equivalent output.

## Task 7: Team Map-State Labels and Persistent Style Profiles

### Files

- Add `event_intelligence/team_states.py`
- Add `event_intelligence/team_profiles.py`
- Add `scripts/build_strict_team_profiles.py`
- Add `tests/test_team_states.py`
- Add `tests/test_team_profiles.py`

### Work

1. Convert Radiant gold advantage into one signed curve per team.
2. Apply three-minute median smoothing from minute 10 to two minutes before
   end, with `L(t)=max(3000,250*t)` and `S(t)=max(6000,400*t)`.
3. Require three consecutive minutes and implement the approved label
   precedence for comeback, throw, stomp/stomp_loss, advantage,
   disadvantage, and even.
4. Persist duration, extrema, time shares, signed/absolute AUC, crossings,
   first thresholds, closeout time, and objective conversions.
5. Mark inadequate timelines `state_unscorable`; never use final score as a
   substitute.
6. Build opportunity-conditional Beta-Binomial style profiles with 45-day
   half-life, roster overlap, patch distance, opponent strength, and event
   strength weights.
7. Store P25/P50/P75 duration by result/state and version every profile cutoff.

### Verify

- `python -m pytest -q tests/test_team_states.py tests/test_team_profiles.py`
- Synthetic curves hit every label boundary and sustained-duration edge.
- Radiant/Dire facts are exact sign mirrors while paired labels are correct.
- One map creates one opportunity per threshold, not one per sampled minute.
- Future maps and unavailable artifacts cannot change an earlier profile.

## Task 8: Explainable Draft Models and Walk-Forward Backtest

### Files

- Add `event_intelligence/draft_features.py`
- Add `event_intelligence/draft_model.py`
- Add `event_intelligence/backtest.py`
- Add `scripts/run_strict_draft_backtest.py`
- Add `tests/test_draft_features.py`
- Add `tests/test_draft_backtest.py`

### Work

1. Build pure-draft features from heroes, expected positions, role fit,
   earlier-only synergy/counters, scaling, control/initiation, save/sustain,
   wave clear, push/high ground, Roshan, mobility/split push, damage profile,
   farm demand, and long-fight/buyback evidence.
2. Build context-adjusted features by adding only pre-map team style, player
   form, roster stability, patch adaptation, and opponent strength.
3. Train regularized explainable landmark models for 10/20/30/40/50 minutes,
   using only maps that reached each landmark.
4. Globally sort by prediction cutoff. Keep reconstructed and genuinely
   prospective availability modes separate.
5. Use pre-2026-04 professional maps as cold-start priors only and report the
   four approved events in the accepted sequence.
6. Store immutable model version, training cutoff, feature schema/input hash,
   support, uncertainty, probability, and eventual outcome for every OOS row.
7. Compute Brier, log loss, five-bin ECE, bootstrap bounds, AUC/accuracy, and
   pure/context ablations.
8. Mark unsupported horizons `insufficient_evidence` and enforce the approved
   calibration gates.

### Verify

- `python -m pytest -q tests/test_draft_features.py tests/test_draft_backtest.py`
- Shuffling future rows cannot alter earlier features or predictions.
- Target-map observed roles/timelines never enter a draft input.
- A 17-minute live lookup uses the validated 10-minute landmark, never 20.
- The report contains one OOS prediction per eligible map/horizon and both
  availability modes are visibly separated.

## Task 9: Bring the Live Shadow Path Up to the Approved Semantics

### Files

- Modify `live_betting/profiles/draft_curve.py`
- Modify `live_betting/shadow_strategy.py`
- Modify `live_betting/shadow_monitor.py`
- Modify `live_betting/storage.py`
- Modify `live_betting/models.py`
- Add/extend `tests/test_shadow_monitor_safety.py`
- Add `tests/test_strict_live_eligibility.py`

### Work

1. Require exact mapping to an approved registry event/team/map before strategy
   evaluation.
2. Replace target-map/post-event prototype role/profile inputs with versioned
   earlier-only intelligence snapshots at transport event time.
3. Activate only the greatest validated landmark not after the trusted clock
   and no more than ten minutes old; wait before minute 10.
4. Compute stability from two distinct transports with the same underdog and
   absolute de-vigged probability movement no greater than 0.02.
5. Add conservative contribution shrinkage and require an independent positive
   draft/team/player reason.
6. Persist complete decision inputs and no-signal reasons.
7. Add signal transport identity and a 15-second event-time expiry to pending
   orders.
8. Fill/reject from the exact first later on-time processed response; require
   the odds ID to be a member of that response and reject missing outcomes.
9. Preserve no-fresh-vision pending fills, atomic order/map transitions,
   restart determinism, and one attempt per map.
10. Keep `DisabledExecutionAdapter` as the only execution implementation.

### Verify

- `python -m pytest -q tests/test_shadow_monitor_safety.py tests/test_strict_live_eligibility.py tests/test_disabled_execution.py`
- Exact response omission yields `outcome_missing`; no successor by expiry
  yields `fill_timeout`.
- Future/late/stale odds cannot signal or fill.
- Paused/stale vision blocks new signals but does not block a valid pending
  successor fill.
- Static search finds no real order/click/account/stake adapter.

## Task 10: Transactional QQ SMTP Outbox

### Files

- Add `live_betting/notifications.py`
- Add `live_betting/smtp_delivery.py`
- Add `scripts/run_notification_worker.py`
- Add `tests/test_notification_outbox.py`
- Add `tests/test_smtp_delivery.py`
- Modify `live_betting/storage.py`
- Modify `live_betting/shadow_monitor.py`
- Modify `live_betting/postmatch_monitor.py`
- Modify `.env.template` without adding any real credential.

### Work

1. Atomically create immutable entry-mail payloads with filled orders and
   result-mail payloads with conflict-free settlements.
2. Use logical uniqueness `(order_key,event_type,channel)` and stable
   Message-ID.
3. Implement pending/leased/sent/dead-letter states, lease-token fencing,
   immutable template version/statistics cutoff, and the approved retry times.
4. Use `smtp.qq.com:465` with verified implicit TLS and no plaintext fallback.
5. Construct structured MIME messages and sanitize external header text.
6. Send filled and settled shadow notifications to `599084618@qq.com`, clearly
   marked as simulations. Rejections remain in reports.
7. Read sender and authorization code only from Windows credential storage or
   environment. Never log or persist them.
8. Classify transient versus permanent SMTP errors and provide an audited local
   dead-letter requeue command.

### Verify

- `python -m pytest -q tests/test_notification_outbox.py tests/test_smtp_delivery.py`
- Fault injection proves fill/settlement and outbox rows commit together.
- A stale lease holder cannot mark another worker's row sent.
- Fake SMTP proves TLS configuration, MIME sanitization, stable payloads,
  retries, and permanent failure handling.
- Repository/database/log secret scan remains clean.

## Task 11: Supervisor, Health, and Reports

### Files

- Add `scripts/run_dota_shadow_service.py`
- Add `event_intelligence/report.py`
- Add/extend `live_betting/report.py`
- Add `tests/test_service_health.py`
- Update `live_betting/README.md`
- Update root `README.md` only for the new supported commands.

### Work

1. Provide one local supervisor for strict ingestion scheduling, companion,
   odds strategy, pending fills, post-match settlement, reports, and mail.
2. Reuse the existing visual watcher supervisor instead of duplicating stream
   OCR internals.
3. Persist independent component health and a single-instance Windows lock.
4. Start migrations before workers and perform bounded SQLite busy retries.
5. Generate coverage, player rankings, team styles, draft metrics, signal
   reasons, fill/slippage, settlement, ROI/drawdown, and mail health reports.
6. Keep reconstructed/prospective and strategy/model versions separate.
7. Make missing SMTP configuration a visible mail-unhealthy state without
   stopping collection or shadow orders.

### Verify

- `python -m pytest -q tests/test_service_health.py`
- Restart resumes retries, pending orders, unsettled maps, and outbox rows.
- Two supervisors cannot run against the same database.
- A failed mail worker does not stop ingestion or shadow strategy.

## Task 12: Full Verification and Live Activation

### Work

1. Run the complete focused predictor and extension suites.
2. Start the companion and confirm `127.0.0.1:8765/health`.
3. Manually load/pair the unpacked Edge extension if it is not already loaded.
4. Open a real RayBet Dota 2 live page without touching a bet slip.
5. Prove a browser event reaches `browser_events`, a complete odds response
   reaches transport/semantic tables, and duplicate delivery is idempotent.
6. When an approved live event and confirmed video are available, prove the
   complete decision -> pending -> fill/reject -> settlement path.
7. Configure sender credentials locally, send one explicitly identified test
   mail, then prove one outbox-driven simulation mail and later settlement mail.
8. Leave real execution disabled and run the shadow system through at least one
   eligible event.

### Verify

From `dota2-predictor`:

```powershell
python -m pytest -q tests
python scripts/run_strict_event_ingest.py --once --reconcile
python scripts/run_strict_draft_backtest.py --database data/dota2.db
python -m live_betting.browser_companion --check-config
```

From `dota2-ad-assistant\edge-extension`:

```powershell
npm test
```

Final evidence must show:

- Strict coverage and explicit reconciliation for every target map.
- Persisted role scores, team labels/profiles, and OOS draft predictions.
- At least one real Edge browser event in the primary database.
- Shadow records only; every execution result is `execution_disabled`.
- Outbox and mail delivery evidence without exposing credentials.
- No claim of accuracy below the approved sample thresholds.
