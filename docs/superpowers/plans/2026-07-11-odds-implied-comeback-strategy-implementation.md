# Odds-Implied Comeback Strategy Implementation Plan

## Goal

Implement the approved comeback strategy across `dota2-ad-assistant` and
`dota2-predictor` while preserving a replayable boundary between visual
observations and strategy decisions.

## Phase 1: Capture Real Vision Fixtures

### Files

- Add `dota2-ad-assistant/vision/stream_capture.py`
- Add `dota2-ad-assistant/scripts/capture_raybet_stream.py`
- Add `dota2-ad-assistant/tests/test_stream_capture.py`

### Work

1. Read a RayBet `m3u8` URL through `cv2.VideoCapture` with reconnect and bounded
   timeouts.
2. Save a small set of full-resolution frames from draft, active game, pause,
   and transition states.
3. Record source URL hash, capture timestamp, width, and height without storing
   tokens or account data.
4. Add fixture-based tests for frame shape, reconnect, and failed streams.

### Verify

- Capture at least one nonblank real frame.
- `pytest tests/test_stream_capture.py -v`

## Phase 2: Versioned Vision Contract

### Files

- Add `dota2-ad-assistant/contracts/live_observation.py`
- Add `dota2-ad-assistant/vision/observation_writer.py`
- Add `dota2-ad-assistant/tests/test_live_observation.py`
- Add matching parser in `dota2-predictor/live_betting/vision.py`

### Work

1. Define schema version 1 with match ID, map number, UTC receipt time, game
   clock, pause state, ten hero IDs, confidence values, and frame reference.
2. Write append-only JSONL atomically.
3. Reject unknown schema versions and invalid hero counts in the predictor.

### Verify

- Round-trip fixture between repositories.
- Invalid/partial observations are persisted but cannot become confirmed.

## Phase 3: Game Clock And Map Recognition

### Files

- Add `dota2-ad-assistant/vision/layouts.py`
- Add `dota2-ad-assistant/vision/clock_reader.py`
- Add `dota2-ad-assistant/vision/map_state.py`
- Add `dota2-ad-assistant/tests/test_clock_reader.py`

### Work

1. Define resolution-independent normalized regions from captured fixtures.
2. Segment Dota clock glyphs and classify digits/colon using local templates.
3. Support negative pre-horn time, active time, pause, and map reset.
4. Confirm readings across consecutive frames; expose confidence.
5. Freeze on unexplained clock regression.

### Verify

- Exact clock on labeled fixtures.
- No interpolation during pause or obscured-clock fixtures.

## Phase 4: Hero Draft Recognition

### Files

- Add `dota2-ad-assistant/data_pipeline/fetch_hero_icons.py`
- Add `dota2-ad-assistant/data_pipeline/build_hero_features.py`
- Add `dota2-ad-assistant/vision/hero_recognizer.py`
- Add `dota2-ad-assistant/tests/test_hero_recognizer.py`

### Work

1. Download hero portraits from a public Dota asset source into a separate hero
   template directory.
2. Compute pHash/ORB features without mixing ability icons.
3. Detect ten HUD/draft slots and identify heroes with margin-based confidence.
4. Require temporal agreement and unique hero IDs before confirmation.

### Verify

- Ten correct heroes on labeled fixtures.
- Unknown/ambiguous crops return no confirmed draft.

## Phase 5: Vision Monitor CLI

### Files

- Add `dota2-ad-assistant/scripts/watch_raybet_stream.py`
- Extend tests with stream replay fixtures.

### Work

1. Accept RayBet match ID, `m3u8` URL, output JSONL path, and sampling rate.
2. Reconnect safely and emit only meaningful observation changes.
3. Retain evidence frames around confirmed changes and errors.

### Verify

- Run against one real RayBet stream for ten minutes.
- JSONL remains valid across reconnect.

## Phase 6: Predictor Storage And Alignment

### Files

- Extend `dota2-predictor/live_betting/storage.py`
- Add `dota2-predictor/live_betting/vision.py`
- Add `dota2-predictor/live_betting/alignment.py`
- Extend `dota2-predictor/tests/test_live_betting.py`

### Work

1. Add vision observation and alignment-anchor tables.
2. Attach map/game time to each changed odds state using confirmed observations.
3. Interpolate only during confirmed unpaused intervals.
4. Mark gaps, regressions, and low-confidence intervals unusable.

### Verify

- Replay produces deterministic anchors.
- Future observations never align to earlier odds snapshots.

## Phase 7: Historical Profiles

### Files

- Add `dota2-predictor/live_betting/profiles/team_style.py`
- Add `dota2-predictor/live_betting/profiles/player_form.py`
- Add `dota2-predictor/live_betting/profiles/rosters.py`
- Add profile tables and tests.

### Work

1. Build roster versions from historical player/team membership.
2. Compute conditional comeback/throw rates from minute net-worth curves.
3. Compute lead-conversion time, duration tendency, objective conversion, and
   high-ground proxies where source data supports them.
4. Build role-specific, opponent-adjusted, time-decayed player form.
5. Apply Bayesian shrinkage and patch/tournament boundaries.

### Verify

- Historical-as-of tests prove no target/future match is included.
- Two-player roster change triggers profile downweighting.

## Phase 8: Draft Power Curves

### Files

- Add `dota2-predictor/live_betting/profiles/draft_curve.py`
- Add `dota2-predictor/live_betting/profiles/draft_features.py`
- Add tests for known synthetic lineups.

### Work

1. Derive 10/20/30/40/50-minute hero and lineup power.
2. Add matchup, synergy, scaling, control, save, wave-clear, push, Roshan,
   mobility, and buyback proxies from available historical/static data.
3. Shrink sparse patch observations to cross-patch priors.

### Verify

- Curves are reproducible and do not read post-match fields from the target map.

## Phase 9: Market-State Archive

### Files

- Add `dota2-predictor/live_betting/archive.py`
- Add `dota2-predictor/live_betting/episodes.py`
- Add replay tests.

### Work

1. Build one aligned feature row per minute and meaningful market transition.
2. Add post-match state and final outcome only as labels.
3. Keep every map within one chronological dataset split.
4. Record market jump, stabilization, and cross-market divergence features.

### Verify

- Leakage audit rejects any row whose feature timestamp follows its decision.

## Phase 10: Comeback Scorer And Shadow Strategy

### Files

- Add `dota2-predictor/live_betting/comeback.py`
- Extend `engine.py`, `strategy.py`, and storage tables.
- Add strategy replay tests.

### Work

1. Implement explainable rule score from team, player, draft, and market blocks.
2. Calibrate probability on chronological data when enough labels exist.
3. Enforce odds 2.5-12, 8-point minimum edge, stable prices, valid draft timing,
   and one filled order per map.
4. Store block contributions and strategy version with every decision.

### Verify

- Duplicate/replayed input creates the same single order.
- Removing a feature block produces the documented ablation output.

## Phase 11: Evaluation And Long-Running Shadow Test

### Files

- Extend `dota2-predictor/live_betting/evaluation.py`
- Add `dota2-predictor/live_betting/report.py`

### Work

1. Report Brier, log loss, calibration, CLV, fill rate, ROI, and drawdown.
2. Break down by odds bucket, minute, patch, tournament, and confidence.
3. Compare market-only, market+draft, market+profiles, and complete models.
4. Run read-only collection continuously; do not claim stability below 500
   filled shadow orders.

### Verify

- Full unit suite in both repositories.
- Ten-minute online smoke test.
- Credential scan and Git diff checks.
