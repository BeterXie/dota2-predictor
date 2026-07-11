# Dota 2 Live Shadow Betting Design

## Objective

Build a read-only shadow betting system for Dota 2. The system collects RayBet
live odds, joins them to PandaScore fixtures and live events, prices supported
markets, records hypothetical orders, and settles them without placing real
bets or storing RayBet account credentials.

The implementation lives in `C:\Users\59908\dota2-predictor` and reuses the
project's SQLite database, historical Dota 2 data, and prediction components.
Existing STRATZ live ingestion is outside this system because its live coverage
has been insufficient for the target matches.

## Data Sources

### RayBet

- Base API: `https://cfinfo.365raylinks.com/v2`
- Dota 2 `game_id`: `151`
- Match lists: `/match?match_type=<type>&page=<page>`
- Match odds: `/odds?match_id=<raybet_match_id>`
- Expected match types: `1/2` live, `3` pre-match, `4` completed
- Provides fixtures, match state, map scores, live stream URL, market status,
  prices, limits, market identifiers, and settlement-related fields.

RayBet is the source of market truth. Every response is timestamped on receipt
and retained as an immutable raw snapshot. Its `manualControlData` represents
map/series scoring and must not be interpreted as in-map kill counts.

### PandaScore

PandaScore is the first commercial live-data adapter. Abios is the fallback
provider if PandaScore does not cover enough target matches or its measured
latency is unsuitable.

- Free Fixtures API supports initial fixture discovery and mapping.
- PandaScore's public sandbox and event recovery currently cover LoL/CS, not
  Dota 2. Dota integration therefore requires a live-supported match and local
  recording for deterministic replay tests.
- Pro Live WebSocket frames/events are required for production live signals.
- Lower-latency feeds are evaluated only after coverage and licensing are
  confirmed.

Before purchasing access, confirm target Dota 2 league coverage, event fields,
latency, WebSocket recovery, historical replay rights, regional availability,
and permission for personal betting analysis.

### Supporting Sources

- OpenDota supplies post-match audit data and fills final historical details.
- RayBet live video is evidence and a manual verification fallback, not the
  primary event source.
- No live event is inferred solely from an odds movement.

## Architecture

Add an isolated `live_betting` package:

```text
live_betting/
  providers/
    base.py
    pandascore.py
    abios.py
  raybet_client.py
  match_linker.py
  market_normalizer.py
  event_detector.py
  pricing/
  strategy.py
  shadow_ledger.py
  settlement.py
  storage.py
  monitor.py
```

The provider boundary exposes:

```python
list_live_matches()
get_match_state(provider_match_id)
stream_events(provider_match_id, cursor)
get_final_result(provider_match_id)
```

Provider-specific payloads are converted into internal fixture, frame, event,
and result models. Pricing and settlement code never reads provider payloads
directly.

## Match Linking

RayBet does not expose a Valve match ID. Linking therefore uses normalized team
names, scheduled time, tournament, best-of format, and current map number.

A link is accepted only when there is one high-confidence candidate. Ambiguous
or low-confidence candidates enter a review state and cannot generate shadow
orders. An accepted link is persisted and is not silently rebound. A conflict
between providers freezes the match until reviewed.

## Collection And Time Alignment

- Refresh RayBet Dota 2 live match lists every 15 seconds.
- Poll odds for active matches every 3 seconds.
- Consume PandaScore live frames/events over WebSocket.
- Preserve source timestamp, provider sequence/cursor, and local receipt time.
- Store all system timestamps in UTC.
- Mark odds stale when their age exceeds a configured threshold.
- Detect gaps, event reordering, score regression, and local clock drift.

PandaScore reconnects from the last confirmed cursor only when the game and
plan support recovery. PandaScore currently does not document recovery for Dota
2. A Dota disconnect therefore creates an explicit gap and pauses signal
generation until a fresh consistent state is established. RayBet timeouts
retain the last snapshot for display and audit, but stale prices cannot be
treated as executable.

Signals are recalculated on confirmed event or price changes. A hypothetical
order uses the next still-open odds snapshot after the signal. This prevents an
event from being evaluated against a price that was visible only before the
event.

## Market Rollout

### Phase 1

- Map winner
- Map total kills over/under
- Series winner is collected but is not priced from a map probability

### Phase 2

- Kill handicap
- First team to 5, 10, and 15 kills
- Map duration over/under
- Team total kills

### Phase 3

Collect and classify every returned Dota 2 market:

1. Directly priceable from current state and an existing model.
2. Requires a dedicated historical model.
3. Collection-only until sufficient data and settlement rules exist.

Each supported market has a dedicated parser, probability function, and
settlement implementation. Uncertain string parsing is never allowed to create
a supported market implicitly.

## Pricing And Strategy

Pricing uses only data available at the quote timestamp. Market probabilities
are de-vigged within a complete outcome group. Model and market probabilities,
input versions, latency, and reason codes are stored with each quote.

A signal requires all of the following:

- Fresh and continuous live data
- A unique accepted match/map link
- An open, fresh market
- A supported and sufficiently trained model
- Edge above the configured threshold after de-vigging
- End-to-end latency below the configured limit

The initial shadow strategy uses a fixed one-unit stake. Kelly sizing is not
used until calibration is established. Correlated selections within one map
share an exposure cap even though no real money is placed.

## Shadow Order Semantics

A signal is not automatically counted as filled. The next RayBet snapshot
determines the result:

- Still open and within allowed slippage: fill at that snapshot's price.
- Closed or suspended: reject as unavailable.
- Price moved beyond allowed slippage: reject as slipped.
- Duplicate signal/order key: ignore idempotently.

Filled orders are immutable. They retain the RayBet match, map, market and odds
IDs; model probability; de-vigged market probability; event/frame reference;
signal and fill timestamps; observed latency; strategy version; price; and
stake.

There is no real order endpoint, login automation, account storage, CAPTCHA
handling, or risk-control bypass in scope.

## Settlement

Settlement supports full win, half win, push, half loss, and full loss where
the market requires them. Provider final results are checked against RayBet's
final state and later audited with OpenDota when an identifiable match exists.
Conflicts are flagged for manual review rather than automatically settled.

## Storage

Add normalized tables:

- `provider_matches`
- `raybet_matches`
- `match_links`
- `odds_snapshots`
- `live_frames`
- `live_events`
- `market_definitions`
- `model_quotes`
- `shadow_orders`
- `settlements`
- `collector_runs`

Raw provider JSON is compressed and stored separately; SQLite contains indexed
normalized data and references to raw artifacts. API keys are read only from
environment variables and are never persisted in the database, raw fixtures,
logs, or Git.

When no PandaScore Live credential is configured, RayBet collection and final
result capture continue, but the system clearly reports that live-event
signals are disabled. It does not fabricate missing events.

## Failure Handling

- A failure in one match does not stop collection for other matches.
- Network and rate-limit failures use bounded exponential backoff.
- Missing event continuity, time regression, or state regression freezes the
  affected map.
- Parser failures preserve the raw market and mark it unsupported.
- Restart restores cursors, accepted links, active matches, and unsettled
  orders from SQLite.
- Collector health records last success, failure, gap, rate-limit, and latency
  measurements.

## Evaluation

Report results overall and by market, tournament, provider coverage, game-time
bucket, and model version:

- Brier score and log loss
- Calibration curves
- Closing-line value
- Shadow ROI and maximum drawdown
- Fill, suspension, rejection, and slippage rates
- Provider and odds latency distributions
- Fixture/map linking success and ambiguity rates
- Live data coverage and gap rates

Fewer than 100 filled shadow orders is considered descriptive only. At 500 or
more, a market can be evaluated for stability, but profitability is not
assumed from hit rate alone.

## Testing

### Unit Tests

- Team and tournament name normalization
- Market parsing and outcome grouping
- De-vigging
- Freshness and slippage rules
- Asian and binary settlement
- Duplicate suppression and exposure caps

### Contract Tests

Sanitized RayBet and PandaScore fixtures detect upstream schema changes without
including credentials or personal data.

### Replay Tests

Recorded events and odds snapshots replay in timestamp order. Tests prove that
future frames, events, odds, and results are unavailable to earlier decisions.

### Online Shadow Tests

Run collection without real ordering and verify reconnect, cursor recovery,
duplicate delivery, stale odds, restart recovery, and final settlement.

## Acceptance Criteria

- Duplicate inputs never create duplicate shadow orders.
- Restarted and uninterrupted replay produce the same orders and settlements.
- Every order is traceable to exact event/frame, odds, model, and strategy
  versions.
- Suspended or stale odds are never counted as filled.
- Unsupported markets never enter strategy evaluation.
- Logs, fixtures, database rows, and reports contain no API token.
- RayBet collection remains operational without a commercial live credential,
  while live-event signals remain explicitly disabled.
- No code path can place a real wager.
