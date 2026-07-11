# Odds-Implied Dota 2 Comeback Strategy Design

## Objective

Build an explainable shadow strategy that identifies live Dota 2 underdogs
whose comeback probability is higher than the de-vigged RayBet map-winner
market probability.

The first version places no real wager. It records at most one hypothetical
map-winner order per map. Other RayBet markets describe the market-implied game
state but are not direct betting targets.

## Core Hypothesis

RayBet's complete in-play market surface contains a noisy observation of the
current match state. Map-winner prices, kill handicaps, total kills, team total
kills, race-to-kill markets, and duration prices jointly describe which team is
leading, the approximate size of the advantage, and whether the market expects
the game to continue.

That market state can be mispriced when it overreacts to short-term events or
underweights information outside the immediate scoreboard:

- The underdog historically defends high ground or comes back well.
- The favorite historically struggles to convert a lead.
- The underdog's players are in stronger recent form than the market assumes.
- The underdog draft scales better or has superior late-game execution tools.
- Kill advantage is not supported by durable economy or objective control.
- Winner prices move more sharply than related handicap, total, and duration
  markets.

RayBet prices may be model inputs, but a signal cannot be justified by price
movement alone. Independent value must come from pre-match profiles, draft
timing, and historically verified outcomes of similar market states.

## Scope

### First Version

- Bet target: current map winner only.
- Candidate: the team currently priced as the underdog.
- Maximum: one filled shadow order per map.
- No averaging down, repeated entry, hedge, or reverse order.
- Fixed one-unit stake.
- Live state: RayBet market surface plus video-derived map number, game clock,
  pause state, and ten hero picks.
- Outcome: final map winner.

### Supporting Markets

These markets are features rather than direct order targets:

- Kill handicap
- Total kills
- Team total kills
- First team to 5/10/15 kills
- Duration over/under
- Series/map score and market status

## System Boundary

### `dota2-ad-assistant`

Owns visual extraction from the RayBet `m3u8` stream:

- Stream frame capture
- Map number and Dota game-clock OCR
- Pause and map-transition detection
- Ten-hero recognition from draft or in-game HUD
- Confidence scoring
- Versioned JSONL output

### `dota2-predictor`

Owns data, modeling, strategy, and evaluation:

- RayBet odds collection
- Vision observation ingestion
- Team style and roster-version profiles
- Player form profiles
- Draft power curves
- Market-state archive
- Comeback probability model
- Shadow entry and fill rules
- Post-match alignment, settlement, and evaluation

The repositories communicate through a versioned JSON contract. They do not
import each other's Python modules or share internal classes.

## Vision Observation Contract

Each confirmed observation contains:

```json
{
  "schema_version": 1,
  "raybet_match_id": "38407673",
  "map_number": 3,
  "captured_at_utc": "2026-07-11T14:43:10.123Z",
  "game_clock_seconds": 1654,
  "is_paused": false,
  "radiant_hero_ids": [1, 2, 3, 4, 5],
  "dire_hero_ids": [6, 7, 8, 9, 10],
  "clock_confidence": 0.98,
  "draft_confidence": 0.96,
  "source_frame_ref": "..."
}
```

The exact game clock cannot be obtained reliably from RayBet's odds response.
`manualControlData.data[currentIndex].time` remained unchanged across 50 live
samples and hundreds of stored raw responses while odds changed. It is retained
for diagnostics only. `currentIndex` may help identify the current map, but it
does not replace video-derived time.

Interpolation between clock observations is allowed only while the stream is
confirmed active and not paused. Clock regression without a confirmed map
transition freezes the map.

## Team Style Profiles

Profiles are keyed by organization and roster version. They are computed only
from matches completed before the target match.

Features include:

- Comeback rate when behind 2k/5k/10k net worth
- Comeback rate when behind at 10/15/20/30 minutes
- Throw rate after leading 2k/5k/10k
- Time from 5k lead to map completion
- Rate of reaching 40 and 50 minutes
- Roshan-to-tower, Roshan-to-high-ground, and Roshan-to-win conversion
- Failed high-ground attempts and high-ground fight outcomes
- Buyback-fight and base-defense outcomes
- Lead growth or decay during successive five-minute windows
- Results against different opponent-strength buckets

Raw comeback counts are not used as strength measures. Rates are conditional on
entering the relevant deficit state and are shrunk toward tournament averages.

Changing two or more starters creates a new roster version. The organization
profile remains available with sharply reduced weight; player profiles carry
forward individually.

## Player Form Profiles

Player form is role-specific and time-decayed. It includes:

- Lane performance where available
- Net worth and experience relative to role expectation
- Kill participation and avoidable deaths
- Farm efficiency and item timing
- Hero-pool breadth and recent hero familiarity
- Late-game deaths, buyback use, and teamfight contribution
- Performance adjusted for opponent and tournament strength

Form windows are patch-aware and do not silently mix different positions.

## Draft Power Curves

The ten recognized heroes produce relative power estimates at 10, 20, 30, 40,
and 50 minutes. Draft features include:

- Hero and lineup win-rate curves by duration
- Damage type and damage scaling
- Reliable control and initiation
- Save, sustain, and disengage
- Wave clear and base defense
- Tower and high-ground pressure
- Roshan speed and contest tools
- Split push and global mobility
- Core scaling and farm distribution
- Buyback and long-fight capability
- Counter-matchups and lineup synergy

The model evaluates the underdog's remaining timing window. A nominally
late-game lineup does not receive credit after its key timing has already
passed or when its required cores have no plausible recovery path.

## Market-State Archive

Every meaningful odds state is aligned after the map with detailed match data:

```text
RayBet receipt timestamp
-> OCR map/game clock
-> complete market surface
-> post-match kills, net worth, buildings, Roshan, buybacks
-> final map result
```

This archive supports event studies and supervised learning of market errors:

- Winner-price overreaction to kills
- Kill and economy divergence
- Winner and handicap inconsistency
- Winner and duration inconsistency
- Price stabilization while the underdog retains a scaling advantage
- Handicap improvement without winner-price recovery
- Mean reversion after abrupt price jumps

Post-match detail is used only for labels, alignment, analysis, and future
training. It is never inserted into a historical live decision.

## Sample Construction

Three-second polls are not independent training examples. Candidate samples are
downsampled by market-state episode:

- At most one regular sample per game minute
- One sample before and after a major price jump
- One sample when a kill-handicap key level is crossed
- One sample after at least two stable odds snapshots

All snapshots from the same map stay in one dataset split.

## Comeback Model

The first version uses an explainable weighted score with conservative
thresholds. Its components are:

```text
team conditional comeback ability
+ opponent lead-conversion weakness
+ player recent form
+ draft power at current and future timings
+ market-implied disadvantage
+ historical outcome of similar market states
= independent comeback probability
```

Weights are initially estimated from historical priors and calibrated on
forward data. After at least roughly 500 filled shadow orders, a supervised
model may replace the weighted score. The rule model remains a permanent
baseline.

Sequence models are out of scope until there is enough data to support
time-series training without leakage.

## Entry Rules

A candidate requires all of the following:

- Map number and game clock are confirmed.
- Ten hero picks are complete and draft confidence is sufficient.
- Winner, kill-handicap, total-kills, and duration markets are present.
- Relevant prices are stable for at least two snapshots.
- The underdog's provisional decimal price is between 2.5 and 12.0.
- Independent comeback probability exceeds de-vigged market probability by at
  least 8 percentage points.
- The underdog has not clearly passed its remaining draft timing.
- Data is fresh, the stream is not paused, and the market is open.
- No order has already filled for the map.

A major odds jump never fills immediately. The next still-open snapshot must
confirm the price and satisfy existing slippage limits. Thresholds are
configuration values and are changed only through a versioned strategy.

## Failure Protection

- Missing or low-confidence clock/draft: collect only, no signal.
- Clock regression without map transition: freeze the map.
- Pause, obscured HUD, or unknown stream delay: pause signal generation.
- Incomplete outcome group or stale/closed market: no quote or fill.
- Implausible source values are preserved but never repaired by guessing.
- Sparse team history: shrink to tournament and opponent-strength priors.
- Two or more roster changes: sharply reduce organization-history weight.
- Sparse current-patch draft data: shrink to cross-patch hero priors.
- Conflicting final results: manual review rather than automatic settlement.

## Evaluation

Dataset splits are chronological:

- Earlier tournaments: training
- Later tournaments: calibration
- Latest complete tournaments: test

Maps, series, and tournaments are never randomly mixed across splits when that
would leak repeated opponents or later information. Profiles are recomputed as
of each historical decision timestamp.

Metrics include:

- Brier score and log loss
- Calibration by probability bucket
- Incremental performance over de-vigged winner prices
- Closing-line value
- Fill, suspension, rejection, and slippage rates
- Shadow ROI and maximum drawdown
- Results by odds range, game minute, patch, tournament, and confidence

Ablation tests compare:

- Market surface only
- Market plus draft
- Market plus team/player profiles
- Complete model

Fewer than 100 filled orders is descriptive only. A stability decision requires
at least 500 filled orders and forward-test performance. Hit rate alone is not
an acceptance metric.

## Acceptance Criteria

- Every decision can be reproduced from data available at its timestamp.
- No post-match or future snapshot appears in a live feature vector.
- At most one shadow order fills per map.
- Low-confidence vision observations cannot produce an order.
- Roster and patch boundaries affect profile weights as designed.
- Rule and supervised models can be evaluated on the same replay dataset.
- Each signal explains the contributions from market, team, player, and draft
  components.
- No code path places a real wager.
