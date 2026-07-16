# Dota 2 Strict Event Intelligence and Shadow Validation Design

## Status

Approved in six sections on 2026-07-13. This document is the consolidated
design for implementation planning. It does not authorize real-money wagering.

On 2026-07-15 the operator approved a local-access exception for the Edge
companion: the one-time pairing/HMAC/nonce protocol is intentionally omitted in
favor of fixed loopback binding, exact extension Origin/version checks, strict
CORS, size limits, and rate limits. That newer decision overrides references
to pairing/HMAC in this document and accepts the resulting local-process threat.

## Objective

Build a causally correct Dota 2 research system that:

1. Archives every map from manually approved Tier 1 main events with at least
   USD 1 million in prize money.
2. Produces role-aware player scores, team style profiles, and explainable draft
   power curves from completed match data.
3. Passively records RayBet Dota 2 live markets through the existing Edge
   extension and local companion.
4. Combines pre-map intelligence with the live market surface to create and
   settle fixed-stake shadow orders only.
5. Evaluates all predictions and orders out of sample, with no future or
   post-match data available to a historical decision.
6. Sends unattended email notifications for filled and settled shadow orders
   to `599084618@qq.com` through a transactional outbox.

The immediate goal is evidence collection and forward validation. Accuracy,
calibration, and simulated profitability must be measured before any strategy
is considered stable.

## Relationship to Existing Designs

This design extends, rather than replaces, the following approved boundaries:

- `2026-07-11-odds-implied-comeback-strategy-design.md`
- `2026-07-13-raybet-edge-monitor-extension-design.md`

The Edge extension remains passive. Browser events are sanitized, authenticated
to the localhost companion, stored idempotently, and then normalized by the
backend. The existing `DisabledExecutionAdapter` remains the only execution
adapter and always returns `execution_disabled`.

This design supersedes any earlier assumption that PandaScore or another
commercial live event feed is required. PandaScore remains disabled by default
and is not part of this design. STRATZ is not a live source. The production
Liquipedia API is excluded because its policy does not permit this betting-
related use case.

## Non-Goals

- Real orders, account automation, bet-slip interaction, or order endpoints.
- CAPTCHA handling, session extraction, or risk-control bypasses.
- Automatic acceptance of newly discovered events.
- Treating odds movement as a factual kill, gold, or objective event.
- Using `manualControlData.data[currentIndex].time` as a trusted game clock.
- Using final GPM or other end-of-map outcomes to infer player position.
- Random train/test splits, sequence models, or future-data repair in the live
  path.
- Supporting a shadow target other than the current map winner in version 1.

## Source Policy

### Event Evidence

Official tournament and organizer pages are manually audited for event tier,
prize pool, dates, and stage boundaries. Their evidence URLs and an audit
timestamp are stored in the event registry. Discovery is automatic, but event
approval is always manual.

### OpenDota

OpenDota is the primary source for league match IDs and completed-match detail,
including players, picks and bans, gold and XP curves, objectives, teamfights,
buybacks, and results. Each response is archived before normalization.

### STRATZ

STRATZ is an optional completed-match fallback and cross-check only. It may fill
a field that OpenDota has not published, but every value retains source
provenance. A conflicting value does not silently overwrite OpenDota; it marks
the field or map for reconciliation. No strategy depends on STRATZ live
coverage.

### RayBet

RayBet is the source of market truth. Complete Dota 2 odds responses may arrive
from the direct read-only collector or the Edge browser monitor. Browser event
time, companion receipt time, and normalized transport-observation time remain
separate fields.

### Video and Vision

Confirmed video/OCR observations provide map number, game clock, pause state,
team-side mapping, and the ten heroes. Vision is the trusted live clock for
version 1. A low-confidence or stale observation may be archived but cannot
produce a shadow decision.

## Strict Event Scope

A map belongs to the formal dataset only when all of the following are true:

- The event itself is Tier 1.
- The event itself has a prize pool of at least USD 1 million.
- The map is part of the event's formal main-event stage.
- The map was played and has a valid result.
- The map is not an exhibition, forfeit, or voided remake.

Tier and prize money do not flow down from a main event to feeder qualifiers,
regional qualifiers, or Division 2 competitions. BLAST's internal main-event
LCQ is included because it is an official stage of the registered main event.

The initial registry snapshot is:

| Event | OpenDota league ID | OpenDota maps at 2026-07-13 | Missing locally |
|---|---:|---:|---:|
| PGL Wallachia S8 | `19543` | 119 | 119 |
| DreamLeague S29 | `19696` | 185 | 0 |
| BLAST SLAM VII | `19101` | 102 | 36 |
| EWC 2026 | `19785` | 120 | 120 |

The initial target is 526 maps. The local database contains 251 target maps and
is missing 275. Thirty locally stored qualifier, Division 2, or other excluded
maps must not enter formal statistics. A public EWC count of 121 versus
OpenDota's 120 remains `reconciliation_pending`; verified maps may be archived,
but the event cannot be marked fully reconciled until the missing or duplicate
map ID is identified.

These counts are an audited starting snapshot, not hard-coded completion
criteria. Later maps enter only through the same registry policy.

## Architecture and Data Flow

```text
official evidence + OpenDota discovery
  -> event candidates
  -> manual event registry approval
  -> versioned raw completed-match archive
  -> normalized match facts and ingest status
  -> role assignments and player map scores
  -> team map-state facts and style profiles
  -> pure-draft and context-adjusted models
  -> chronological out-of-sample predictions

RayBet browser/direct observations + confirmed vision
  -> immutable odds and transport observations
  -> causal clock/map alignment
  -> de-vigged market surface
  -> draft/team/player context as of transport event time
  -> explainable live comeback decision
  -> pending shadow order
  -> next-observation simulated fill or rejection
  -> OpenDota post-match settlement and attribution
  -> reports + transactional email outbox
```

Completed-match ingestion, scoring, model training, live collection, strategy,
settlement, reporting, and notification are separate components. They exchange
versioned records through SQLite and content-addressed raw artifacts; they do
not reach into one another's internal payload formats.

## Event Registry and Incremental Archive

### Registry Tables

`event_registry` stores:

- Internal event ID and canonical name.
- Tier, prize pool in USD, and formal stage/date boundaries.
- OpenDota league ID and optional secondary provider IDs.
- Official evidence URLs.
- Scope-policy version, approval status, approver, and approval time.
- Reconciliation status and expected/observed map counts.

`event_candidates` stores automatically discovered leagues and their evidence.
A candidate cannot enter formal scoring, training, backtesting, or live shadow
eligibility until promoted to `event_registry` by an explicit audit action.

`match_ingest_status` records each map's discovery, basic-result, detailed-
parse, cross-check, and reconciliation states. It also records missing fields,
raw artifact versions, retry count, next retry time, the first time each
artifact became usable by this system, and component-specific eligibility such
as player-scoreable, state-scoreable, and draft-scoreable.

### Versioned Raw Responses

Every OpenDota or STRATZ response is compressed and stored by content hash with
source, endpoint, sanitized request identity, source/receipt timestamps,
`first_usable_at`, schema fingerprint, and event/match references. Request URLs
and identities remove tokens and credentials before persistence. A delayed
parse creates a new raw version and never overwrites an older response.
Normalized records update in a transaction only when the candidate version is
newer and demonstrably more complete, or when a versioned correction is
explicitly approved.

Missing facts remain `NULL` with a reason. The pipeline does not linearly
estimate absent early deaths, assists, control time, vision, damage taken, or
other facts. Secondary-source conflicts remain visible.

### Incremental Schedule

The flow is:

```text
discover candidate
  -> approve registry event
  -> obtain league match IDs
  -> fetch new or changed maps
  -> persist compressed raw response and hash
  -> validate result, teams, ten players, draft, and required timelines
  -> atomically normalize available facts
  -> mark each downstream component ready, retryable, or review-required
```

Active events are checked every 15 minutes. Recently completed details retry at
15 minutes, 1 hour, 6 hours, 24 hours, and 72 hours. The most recent seven days
are rescanned daily so OpenDota's delayed parser can upgrade incomplete maps.
Rate limits and transient failures use bounded backoff and do not erase the
last valid version.

## Position Assignment and Player Map Scores

### Position Assignment

The system stores two different role concepts and never substitutes one for the
other:

- `observed_position` is an ex-post label used to compare a player's execution
  with the correct positional benchmark.
- `expected_position` is an as-of prediction feature built only from facts
  available before the target map.

`observed_position` is assigned in this order:

1. The audited event roster's declared position.
2. The player's lane and resource-priority pattern over the 20 maps completed
   before the target map.
3. A single-map maximum-weight assignment: mid is position 2, safe-lane core is
   position 1, off-lane core is position 3, and positions 4/5 are separated by
   recorded 10-minute economy, last hits, roaming, warding, and stacking.

`expected_position` may use only the audited roster position and the 20-map
pattern completed and usable before the prediction cutoff. It cannot use the
target map's lane, economy, last hits, roaming, wards, stacks, final GPM, or any
other target-map timeline. If that pre-map evidence is insufficient, the role
is unknown and role-dependent draft features are omitted.

Final GPM is never a position feature in either context. Every assignment
stores its purpose, position, source, confidence, input cutoff, and assignment
version. An `observed_position` score with confidence below `0.7` is explicitly
low-confidence and is excluded from position rankings. An `expected_position`
below `0.7` is excluded from position-dependent model training and prediction.

### Role-Specific Weight Sets

| Position | Version 1 score composition |
|---|---|
| 1 | Farm efficiency 25%, output per unit economy 20%, participation 15%, tower/Roshan conversion 20%, survival 10%, late fights 10% |
| 2 | Lane differential 20%, rotation/participation 25%, kill and damage output 20%, tempo objectives 15%, resource efficiency 10%, survival 10% |
| 3 | Suppression of opposing position 1 20%, damage taken/initiation/control 25%, teamfights 20%, tower/high-ground conversion 15%, low-resource efficiency 10%, survival 10% |
| 4 | Early rotation/participation 25%, vision/dewarding 20%, control/initiation 25%, teamfights 15%, low-resource efficiency 10%, objective conversion 5% |
| 5 | Vision/dewarding 30%, control/save/healing 25%, teamfight participation 20%, low-resource efficiency 10%, lane support/pulls/stacks 10%, objectives 5% |

Only source-recorded facts contribute. When a component is unavailable, its
coverage drops; hero damage cannot stand in for damage taken, and prorated
end-of-map totals cannot stand in for true 10-minute events.

### Normalization and Outputs

Raw metrics first become per-10-minute rates, team shares, opportunity rates,
or output per unit economy as appropriate. They are compared with historical
distributions for the same patch, position, similar duration, and similar event
strength using median/MAD robust z-scores. Opponent strength, hero matchup, and
draft expectation are handled as residual adjustments rather than hidden win
bonuses.

Each player-map result contains:

- `execution_score`: individual execution without a win/loss reward.
- `result_adjusted_score`: execution plus at most +/-5 points for result and
  advantage conversion.
- Raw component facts, component scores, coverage, role confidence, benchmark
  cutoff, and score version.

The neutral score is 50. Missing coverage and uncertain position shrink the
score toward 50. All benchmarks contain only maps completed before the target
map. In prospective operation, their required artifacts must also have
`first_usable_at` no later than the score/profile cutoff. Pre-2026-04
professional maps may supply cold-start priors but remain separate from formal
target-event statistics, rankings, and evaluation.

## Team Map-State Labels and Style Profiles

### Team-Perspective State Curve

Each map produces one row per team. Radiant gold advantage is converted to a
signed team-perspective curve `A(t)` and smoothed with a three-minute median.
Classification examines minute 10 through two minutes before map end.

For game minute `t`:

```text
significant lead L(t) = max(3000, 250 * t)
stomp lead       S(t) = max(6000, 400 * t)
```

A state must persist for at least three consecutive minutes. One isolated
teamfight swing does not change the state.

### Label Precedence

Labels are evaluated in this order:

1. `comeback`: winner previously sustained a deficit beyond `L(t)`.
2. `throw`: loser previously sustained a lead beyond `L(t)`.
3. `stomp`: winner reached `S(t)` before minute 20, spent at least 60% of the
   analyzed period in stomp advantage, and was never significantly behind.
4. `stomp_loss`: the paired losing-team label for a stomp.
5. `advantage`: winner spent at least 25% in significant advantage and was
   never significantly behind.
6. `disadvantage`: loser spent at least 25% in significant deficit and was
   never significantly ahead.
7. `even`: every other scoreable map.

### Reproducible Facts

The system stores the facts behind the label:

- Duration, maximum lead, and maximum deficit.
- Fractions of time ahead, behind, and even.
- Signed and absolute gold-curve area.
- Lead/deficit crossings and first significant lead/deficit times.
- Time from obtaining a significant lead to map end.
- Roshan, tower, and high-ground conversion facts.
- Curve coverage, source versions, and label version.

A map without a complete enough gold timeline is `state_unscorable` and remains
eligible for retry. Final score or winner is not used to guess its state label.

### Long-Term Team Style

Profiles are conditional on opportunities, not raw win counts. Examples include
comeback rates after 3k/5k/10k deficits, throw rates after leads, time to close
from 5k, rates of reaching 40/50 minutes, and Roshan-to-tower, high-ground, and
win conversion.

Rates use Beta-Binomial shrinkage toward the applicable event/patch prior.
Observation weights decay with a 45-day half-life and are further adjusted for
roster overlap, patch distance, and opponent strength. Duration distributions
store P25/P50/P75 separately for wins, losses, advantage, disadvantage, and
even maps instead of relying on a single mean.

## Draft Rating and Historical Backtesting

### Prediction Targets

The model produces Radiant probabilities at 10, 20, 30, 40, and 50 minutes:

- `pure_draft`: heroes, pre-map `expected_position` assignments, and lineup
  structure only.
- `context_adjusted`: pure draft plus pre-map team style, player form, roster
  stability, patch adaptation, and opponent strength.

For horizon `t`, the target is explicitly:

```text
P(Radiant eventually wins | map is still active at t, information available
before the map)
```

Training for a horizon contains only maps that reached that horizon. The curve
does not claim to know live gold, kills, items, or objectives. Those live facts
are absent from both draft models.

### Features

Draft features cover:

- Role fit, hero familiarity, and resource-demand conflicts.
- Within-lineup synergy and cross-lineup counters.
- Scaling and timing windows.
- Reliable control, initiation, save, sustain, and disengage.
- Wave clear, push, high-ground attack and defense, and Roshan capability.
- Mobility, global presence, and split push.
- Physical/magical/pure damage profile and durability answers.
- Long-fight, cooldown, buyback, and base-defense capability.

Maps with pre-map `expected_position` confidence below `0.7` do not train
role-dependent features. The ex-post `observed_position` from the target map is
never a draft-model input. A map may contribute to explicitly unordered hero
features when all other eligibility checks pass.

### Model Family

Version 1 uses regularized, explainable walk-forward models with smoothed
hero/synergy/counter priors. Team and player strength controls prevent the
model from assigning every historical team advantage to its heroes. Every
feature statistic and calibrator is fit only on rows earlier than the target
prediction. Sparse estimates shrink toward neutral and expose their support.

The rule/explainable baseline remains permanent even if a more flexible model
is added later. Sequence models are out of scope until the live archive can
support them without treating repeated three-second polls as independent rows.

### Walk-Forward Evaluation

The intended target-event reporting progression is:

```text
PGL Wallachia S8
  -> DreamLeague S29
  -> BLAST SLAM VII
  -> EWC 2026
```

Professional maps before 2026-04-01 are cold-start priors only. For each map,
`prediction_cutoff` is the recorded completed-draft time when available,
otherwise map start time; the cutoff source is stored. Within each event, maps
are predicted in actual start order. The training engine does not
trust the event list itself as chronology: it globally sorts every map by its
stored draft/prediction cutoff, so overlapping events or rescheduled maps
cannot cross the time boundary. A prior map in the same series may be used only
when it completed before the target cutoff.

For facts collected after this system starts, prospective replay additionally
requires `first_usable_at <= prediction_cutoff`. A historical artifact without
a trustworthy availability timestamp may be used only in a
`reconstructed_walk_forward` run. Reconstructed and genuinely prospective
results are stored and reported separately. Reconstructed performance may
enable an explicitly experimental shadow-only landmark after the stated sample
and calibration gates, but it cannot establish prospective accuracy/stability
or promote a model beyond evidence collection. There is no random split and no
future patch, roster, result, profile, or odds observation in a feature vector.

Every out-of-sample prediction stores model version, training cutoff, feature
schema hash, input snapshot hash, horizon, point probability, uncertainty,
support, and result when later known.

Primary metrics are Brier score, log loss, calibration curves, and expected
calibration error. AUC and accuracy are secondary. Ablations compare pure
draft, team/player controls, style, and the complete context model.

Each horizon needs at least 100 strict out-of-sample target maps before it can
enter the live shadow feature set. The initial calibration gate is frozen as:
Brier score below `0.25`, log loss below `ln(2)`, five equal-count-bin ECE no
greater than `0.10`, and a series-cluster-bootstrap 90% upper ECE bound no
greater than `0.15`. Metrics are computed for one frozen model version and are
not pooled across versions. Event-level sensitivity is reported separately.
Unsupported 40/50 minute horizons remain `insufficient_evidence` rather than
borrowing confidence from earlier horizons.

RayBet odds are never draft-model features. They enter only the later live
strategy and evaluation layer.

## Real-Time Signal Design

### Strict Live Eligibility

A live RayBet match can produce a shadow decision only when it maps uniquely to
an approved registry event, exact teams, and current map number. Fuzzy names or
ambiguous event/map links create an audit/review row only. An accepted mapping
cannot silently rebind.

### Causal Time Rules

- Browser `captured_at` orders browser observations; companion receipt time is
  audit metadata, not event time.
- A browser timestamp more than five seconds in the future is audit-only.
- A late observation is audit-only and cannot change current odds, trigger a
  decision, or fill an order.
- The trusted visual observation must be confirmed, show ten unique heroes,
  have clock and draft confidence at least `0.9`, and be no older than 30
  seconds.
- The latest processed odds transport observation must be no older than 15
  seconds.
- Clock interpolation may move forward at most 15 seconds, only while the map
  is confirmed active and not paused.
- Map mismatch, clock regression, pause/unknown pause, stale vision, or missing
  prior vision freezes decisions.
- Profile and draft `as_of` cutoffs use transport event time, never local
  processing time.

`manualControlData.data[currentIndex].time` remains a separately stored
`diagnostic_untrusted` observation. It may be compared with confirmed video
time in research reports, but version 1 never promotes it into
`game_clock_seconds` or uses it to satisfy a strategy clock gate.

### Market Surface

The map-winner group must contain both open outcomes and is de-vigged by stable
market group ID. The minimum supporting surface also requires complete, open
kill-handicap, total-kills, and duration groups. Team-total and race-to-kill
markets enrich features when complete but are not mandatory in version 1.

Supporting markets describe what the market appears to believe. They do not
become claims about actual gold, kills, or objectives until joined to
post-match detail.

### Explainable Live Probability

The live strategy starts from de-vigged underdog map-winner probability
`p_market` and applies versioned, calibrated contributions from:

- Underdog conditional comeback ability.
- Favorite lead-conversion and throw weakness.
- Relative recent player form.
- Current/future draft timing power.
- Cross-market inconsistency and stable price movement.

The result is `p_live`. Price movement alone cannot create an order. At least
one positive independent contribution from draft, team style, or player form
is required. Until sufficient forward odds history exists, the explainable
rule model remains the live model; no supervised market-state model replaces it
before at least 500 filled, settled shadow orders and a clean forward test.

At game minute `m`, the probability contribution may use only the greatest
validated landmark `t <= m`, and that landmark may be no more than 10 game
minutes old. No draft-probability contribution is available before minute 10;
if the required landmark is `insufficient_evidence`, the strategy waits. A
future landmark probability is conditional on the map reaching that future
time and therefore cannot be inserted directly into current `p_live`. Static
draft scaling features may describe future timing in the explanation, but they
do not substitute the future conditional probability.

### Initial Entry Gates

All gates must pass:

- The selected team is the current map-winner underdog.
- Decimal odds are between `2.5` and `12.0` inclusive.
- Point edge `p_live - p_market` is at least `0.08`.
- A conservative probability remains above `p_market`. Before prospective live
  calibration exists, it is computed by shrinking every positive non-market
  log-odds contribution by its stored support/quality while counting negative
  contributions in full. Once prospective market-state validation exists, a
  versioned series-block-bootstrap uncertainty margin is added; a change to the
  confidence rule creates a new strategy version.
- Aggregate data quality is at least `0.2`.
- The active draft landmark passed its support and calibration gates.
- The complete market has been observed in two distinct, increasing, processed
  transport observations.
- The same team remains the underdog and absolute de-vigged `p_market` movement
  is no more than `0.02` (two percentage points) between those observations.
- No attempt already exists for the map.

Threshold changes create a new immutable strategy version. They are not tuned
retroactively against the held-out event being reported.

## Shadow Order and Settlement Semantics

Version 1 uses a fixed one-unit stake. It permits at most one shadow attempt per
map, with no averaging down, hedge, or reverse entry.

A qualifying decision atomically reserves the map and creates a `pending`
order with its signal transport identity and
`expires_at = signaled_at + 15 seconds`. It does not fill at the signal quote.
The first strictly later `on_time + processed` complete odds transport for the
same RayBet match and map determines execution. Replay uses the same captured
event-time boundary, not the time at which a delayed worker happens to run:

- The normalized membership of that exact response must explicitly contain the
  same odds ID. A carried-forward old snapshot cannot satisfy a fill.
- Same odds ID, market open, observation at or before `expires_at`, and no more
  than 3% adverse decimal-odds movement: `filled` at the later price.
- Missing outcome, closed/suspended market, observation after expiry, or
  excessive slippage: `rejected` with a specific reason.
- If no complete successor arrives by `expires_at`, the live clock/replay
  watermark atomically records `rejected=fill_timeout`.

A large price jump therefore never fills immediately. A response that omits the
outcome rejects it as `outcome_missing` rather than waiting for a more favorable
response. A pending order may be filled after restart and does not require a
new vision frame, because vision was a signal-creation gate; it still requires
the captured successor to satisfy the persisted event-time expiry, membership,
market, and slippage checks. Order status and map-attempt status update in one
transaction.

Settlement uses the completed map winner and preserves the exact RayBet match,
map, market, odds ID, signal/fill timestamps, signal/fill prices, model and
market probabilities, contribution breakdown, input references, strategy
version, and settlement source. Conflicting outcomes enter manual review.

No record is ever converted into a page action or real order. There is no
feature flag, environment setting, hidden adapter, or browser message that can
enable execution.

## Post-Match Attribution and Forward Learning

After a map ends, exact draft/team mapping links the odds timeline to OpenDota
gold, kills, buildings, Roshan, teamfights, buybacks, duration, and result.
These facts answer questions such as:

- Did winner odds overreact to kills without durable economy/objective control?
- Did kill handicap improve without winner-price recovery?
- Did winner, handicap, total, and duration markets disagree?
- Did the underdog retain a valid draft timing and high-ground defense window?
- Did the favorite repeat a measured failure to convert advantage?

Post-match fields are labels and analysis facts only. They are unavailable to
the decision from the same map and can affect only later model versions with a
training cutoff after the map completed.

Three-second polls are not independent training examples. Offline datasets
downsample by market-state episode: at most one regular sample per game minute,
plus explicit before/after jump, key-line crossing, and stable-state samples.
All samples from one map remain in the same temporal fold.

## Email Notification Outbox

### Notification Events

Each successfully simulated `filled` order creates an immediate entry email.
Each later settlement creates one result email. Rejected candidates, including
`fill_timeout`, remain in local daily reports and do not produce real-time order
mail.

The recipient is `599084618@qq.com`. Entry mail includes event, teams, map,
trusted game time, selected side, signal/fill prices, de-vigged market
probability, live-model probability, edge, principal contributions, quality,
model/strategy versions, and order ID. Settlement mail adds result, one-unit
profit/loss, and cumulative shadow statistics. Every message prominently says
that it is a simulation and no real wager was made.

### Transaction and Retry Rules

There are two explicit transaction boundaries:

1. `filled order + map-attempt status + immutable entry-mail outbox row`.
2. `settlement insert/resolution + immutable result-mail outbox row`.

A settlement in conflict/manual-review state does not schedule result mail. A
unique logical key `(order_key, event_type, channel)` prevents duplicate
scheduling. Each row stores an immutable payload, statistics cutoff, template
version, recipient, and stable Message-ID so a retry cannot silently render
different cumulative statistics. Notification failure never rolls back,
rejects, fills, settles, or recreates an order.

Outbox states are `pending`, `leased`, `sent`, and `dead_letter`. The delivery
worker claims a row in a short transaction by setting a unique `lease_token`,
`lease_until`, `attempt_count`, and `next_attempt_at`; SMTP I/O happens outside
the transaction. Completion uses a compare-and-set on the same lease token, so
an expired worker cannot update a row claimed by another worker. Retryable
network and SMTP 4xx failures retry after 1 minute, 5 minutes, 30 minutes,
2 hours, and 12 hours. Authentication, invalid-recipient, and permanent SMTP
5xx failures enter `dead_letter` immediately and mark mail health degraded.
Restart resumes pending and expired leases. A local audited command may requeue
a dead letter after configuration is corrected.

At the live-schema version 4 safety boundary (formal-email template version 2),
restart recovery is restricted to `pending` or expired `leased` rows that use
the current template and pass the complete immutable decision, vision, and fill
lineage gates. A pre-template-version-2 formal row, or any formal row missing
that lineage, enters an audited `dead_letter` during migration or its first
delivery claim. It is never re-rendered with a newer template or silently
upgraded. The migration audit found no such rows in the current production
database.

Every logical event uses a stable RFC Message-ID. The application sends a
logical event once after a successful SMTP acknowledgement. SMTP cannot
guarantee mathematical exactly-once delivery when the server accepts a message
but the connection fails before acknowledgement; the stable Message-ID and
outbox make such rare duplicates identifiable, while avoiding the false claim
that this network ambiguity can be eliminated.

### Delivery and Secrets

Unattended delivery uses `smtp.qq.com:465` with implicit TLS, certificate and
hostname validation, and no plaintext fallback. Mail is constructed with a
structured MIME API; CR/LF and other header-control characters are removed from
external event/team text before it reaches a subject or header. The sender
address and SMTP authorization code are deployment prerequisites stored only in
Windows Credential Manager or process environment. They never enter Git,
SQLite, raw artifacts, reports, browser storage, or logs. The recipient is
non-secret configuration.

The interactive `agently-mail` CLI is not the unattended adapter because each
send requires a separate user confirmation. It may be used only for an
explicitly confirmed manual test, not by the background service.

## Storage Additions

Additive migrations introduce or extend these logical tables:

- `event_registry`
- `event_candidates`
- `raw_source_artifacts`
- `match_ingest_status`
- `player_role_assignments`
- `player_map_scores`
- `team_map_states`
- `team_style_profiles`
- `draft_model_runs`
- `draft_predictions`
- Existing `browser_events`, `odds_transport_observations`, `odds_snapshots`,
  `odds_alignments`, `strategy_decisions`, `shadow_map_attempts`,
  `shadow_orders`, and `settlements`
- `notification_outbox`
- `service_health`

Derived rows store source/version references, feature or input hashes, event-
time cutoffs, quality/coverage, and status reasons. Large raw payloads remain
compressed outside SQLite and are referenced by content hash.

Migrations do not rewrite unrelated historical tables or discard existing user
data. Before workers start, the supervisor takes a Windows process-level lock,
checks `schema_version`, makes a SQLite online-backup snapshot, and runs any
additive migration in one exclusive transaction. Every connection enables
foreign keys and a 5-second busy timeout; `SQLITE_BUSY` receives bounded retry.
WAL mode is used for the long-running local service. Short transactions and
idempotent keys make restart behavior deterministic. The same online-backup
API, rather than copying live database/WAL files independently, is used for
operator backups and restore tests.

## Long-Running Service and Security

One local supervisor command owns component lifecycles for:

- Browser companion health.
- Completed-match event polling and retry scheduling.
- Odds normalization and shadow evaluation.
- Pending fill processing and post-match settlement.
- Report generation and notification delivery.

Components persist independent health and progress. Failure in one match or
component does not stop the others. A single-instance lock prevents two local
supervisors from racing on the same database.

The companion listens only on fixed loopback and enforces the approved exact
extension Origin/version, protocol-version, CORS, rate, content-type, and body
limits. Extension domain allowlists, pause state, payload size limits, and
sanitization remain enforced. This is local access control rather than
cryptographic authentication. Logs never include raw credentials, query tokens,
authorization codes, or unsanitized browser payloads.

Deployment requires:

1. Manually loading `C:\Users\59908\dota2-predictor\edge-extension` from
   `edge://extensions` in Developer mode.
2. Configuring the exact loaded extension Origin for the localhost companion and
   verifying protocol version 1.
3. Optionally configuring the sender address and SMTP authorization code locally
   without posting them in chat; missing SMTP degrades notifications only.
4. Starting the supervisor and verifying a Dota 2 live page, a companion
   acknowledgement, and a non-betting dry-run status.

## Failure Handling

- Unknown or unapproved event: discovery/audit only.
- Incomplete recent OpenDota parse: preserve the version and schedule retry.
- Source conflict: preserve both facts and require reconciliation.
- Missing player metric: reduce coverage and shrink toward neutral.
- Low role confidence: exclude from positional ranking/training.
- Missing gold timeline: `state_unscorable`, never infer from final score.
- Sparse team/hero history: shrink toward applicable priors.
- Browser duplicate: acknowledge idempotently without duplicate normalization.
- Future or late browser event: audit only.
- Odds/vision stale, paused, incomplete, or causally unaligned: no signal.
- Ambiguous match/map link: freeze strategy for that match.
- Market closes or slips: reject the shadow order with a reason.
- Crash during order creation or status update: transaction leaves either the
  complete state or no state.
- SMTP failure: retry only the outbox event; order state remains unchanged.
- Conflicting settlement: manual review, no guessed outcome.

## Testing Strategy

### Unit Tests

- Registry eligibility, stage inclusion, and exclusion rules.
- Retry schedule, raw content hashing, version selection, and reconciliation.
- Separate ex-post `observed_position` and as-of `expected_position`, position
  priority, maximum assignment, confidence, and no-final-GPM rule.
- Each role's weights, robust normalization, coverage shrinkage, and +/-5 result
  adjustment cap.
- Team-perspective curve conversion, smoothing, sustained thresholds, label
  precedence, and `state_unscorable` behavior.
- Beta-Binomial shrinkage and 45-day/roster/patch/opponent weighting.
- Draft feature generation, landmark activation, horizon eligibility, support
  and calibration gates, and immutable prediction references.
- De-vigging, market completeness, freshness, probability stability, edge,
  conservative uncertainty, exact-response membership, expiry, slippage, and
  one-attempt rules.
- Outbox transaction coupling, immutable payloads, lease-token fencing, retry
  classification, dead letters, MIME header sanitization, and secret redaction.

### Leakage and Replay Tests

- Recompute every profile and feature using only rows before its event-time
  cutoff.
- For prospective replay, reject facts whose `first_usable_at` is after the
  cutoff; label unavailable historical timestamps as reconstructed instead of
  prospective.
- Prove that a future browser timestamp, late response, future vision frame,
  later map, post-match fact, or future event cannot alter an earlier decision.
- Replay identical immutable inputs before and after restart and require the
  same assignments, scores, labels, predictions, decisions, orders, and
  settlements.
- Keep all samples from one map in one temporal fold.

### Contract and Integration Tests

- Sanitized OpenDota, optional STRATZ, RayBet, browser, and vision fixtures.
- Upstream schema-change detection without tokens or personal data.
- Edge extension -> companion -> SQLite -> strategy -> next-observation fill ->
  settlement -> fake SMTP end-to-end test.
- Transaction fault injection around normalization, map reservation, order
  transition, settlement, and outbox creation.
- Fake SMTP success, transient failure, permanent failure, restart, and
  acknowledgement-loss behavior.

### Live Soak

Run through at least one complete eligible event. Verify collection continuity,
map mapping, restart recovery, report totals, and mail delivery. The soak never
enables real execution.

## Evaluation and Promotion Gates

Historical draft evaluation reports Brier score, log loss, calibration/ECE,
AUC, accuracy, sample support, and ablations by event and horizon.

Live shadow reports include:

- Brier score, log loss, and calibration.
- Shadow ROI and maximum drawdown at one-unit stake.
- Fill, suspension, rejection, slippage, and stale-data rates.
- Results by event, team, odds band, trusted game-minute band, draft type,
  signal reason, quality, model, and strategy version.
- Market and vision latency, alignment success, and data-coverage rates.

Counts refer to unique maps/orders, never transport observations or
downsampled states. Fewer than 100 settled filled orders is descriptive only.
Results from 100 to 499 remain experimental. At least 500 filled and settled
forward orders from one frozen model/strategy version are required before
judging that version's stability or allowing a supervised live market-state
model to challenge the rule baseline. Incompatible versions are never pooled.

Uncertainty uses a series-cluster bootstrap with event-level sensitivity
reporting. A one-event cohort cannot claim cross-event stability regardless of
size. Hit rate alone is never a promotion metric; calibration, Brier/log-loss
improvement over the de-vigged market baseline, simulated return after observed
slippage, drawdown, and stability across events must agree. Point estimates and
90% confidence intervals are both reported.

## Acceptance Criteria

- Only manually approved strict-scope maps enter formal statistics, training,
  evaluation, or live shadow eligibility.
- Every raw response is content-addressed and versioned; missing facts remain
  `NULL` rather than guessed.
- Position assignments expose source/confidence and never use final GPM.
- Player scores reproduce from versioned raw facts and earlier-only benchmarks.
- Team labels reproduce from stored curves, thresholds, and label version.
- Draft and context predictions are chronologically out of sample and retain
  model, feature, input, and cutoff references.
- Historical reconstructed walk-forward and genuinely prospective results are
  labeled and reported separately; reconstructed results can enable only
  experimental shadow collection, not a prospective stability claim.
- Every live decision uses only event-time-available odds, vision, profiles, and
  draft predictions.
- `manualControlData...time`, stale/future/late inputs, incomplete markets, and
  paused or ambiguous maps cannot produce a new order. A previously pending
  order does not need new vision; only its strictly later on-time odds response,
  explicit outcome membership, persisted expiry, market status, and slippage
  determine fill or rejection.
- Two distinct transport observations are required for stability, including
  when semantic odds did not change.
- At most one shadow attempt exists per map and every fill uses a later eligible
  quote from the exact successor payload with the configured expiry and
  slippage rules.
- Restarted and uninterrupted replay produce the same durable results.
- Each fill and conflict-free settlement atomically schedules one immutable
  logical email event without coupling mail success to order state.
- Reports, logs, fixtures, browser storage, and database rows contain no API or
  SMTP secrets.
- PandaScore and STRATZ live data are unnecessary for normal operation.
- Every execution call returns `execution_disabled`; no code path places a real
  wager.
