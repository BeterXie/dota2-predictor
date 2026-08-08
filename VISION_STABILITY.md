# Stable realtime vision runtime

This branch adds a shadow-safe stabilization layer around the existing RayBet
vision watcher.  It does not change the P0/P1 prediction feature boundary and
it does not add a database migration.

## What is included

- sticky layout selection with acquisition, grace and challenger hysteresis;
- separate stream-source and content-derived frame identities so repeated
  evidence is no longer keyed by the HLS URL hash;
- a DraftTracker fix that accepts time-separated, content-distinct frames even
  when the hero crop and game clock remain visually stable;
- ten-slot global hero assignment using the existing hero feature scores and
  SciPy Hungarian assignment;
- HUD perception that continues to read clock, score, net-worth and heroes on
  replay/untrusted frames while publication remains fail-closed;
- freeze semantics for replay/untrusted frames: accumulated trackers and locked
  lineups are not reset by the stable launcher, and OCR conflicts cannot mutate
  a locked lineup within one map;
- target-team gating: hero evidence remains frozen until the RayBet teams are
  confirmed on the broadcast, so a waiting-room or preceding-match HUD cannot
  poison the immutable lineup;
- frame-quality/freeze diagnostics;
- optional rate-limited failure-frame and hero-crop capture with an event cap
  that survives watcher restarts;
- a JSONL corpus evaluator and regression tests for the stability state
  machines.

## Run

Use the stabilized entry point instead of the legacy watcher:

```bash
python scripts/watch_raybet_stream_stable.py --match-id <raybet-match-id>
```

For the normal multi-match service, use the stable supervisor wrapper:

```bash
python scripts/supervise_raybet_streams_stable.py
```

A known tournament overlay can bypass per-frame layout guessing:

```bash
VISION_LAYOUT_PROFILE=epl_s39_live_1080p \
  python scripts/supervise_raybet_streams_stable.py
```

To collect real failure evidence:

```bash
VISION_DEBUG_DIR=data/live_betting/vision_debug \
  python scripts/watch_raybet_stream_stable.py --match-id <raybet-match-id>
```

The stable entry point installs adapters in-process and then delegates argument
parsing, persistence, RayBet identity checks, map progression, settlement-facing
observations and heartbeat output to the existing watcher.

If team-logo loading or matching is unavailable, the stable watcher deliberately
keeps draft tracking frozen.  An operator may use `--radiant-side team_one` or
`--radiant-side team_two` only when the target broadcast identity and side are
known independently.

## Real-frame corpus

Create a JSONL manifest next to captured frames.  Each row may contain:

```json
{"file":"game_001.jpg","layout":"epl_s39_live_1080p","scene":"game","radiant_heroes":[1,2,3,4,5],"dire_heroes":[6,7,8,9,10]}
```

Evaluate with:

```bash
python scripts/evaluate_vision_stability.py path/to/manifest.jsonl
```

For a truth-anchored retained observation sequence, run the stable hero
evaluator with exactly ten HUD-order hero IDs.  `--perception-only` skips OCR
gates and is intended for template-bank experiments; omit it for a
runtime-faithful gate replay.

```bash
python scripts/evaluate_hero_recognition.py --stable --perception-only \
  --layout-profile standard_dota_hud_1080p \
  --observation-jsonl path/to/observations.jsonl \
  --truth-hero-ids 1 2 3 4 5 6 7 8 9 10
```

## Deliberate boundary

This implementation is the infrastructure/stability phase.  A learned ONNX
hero classifier is intentionally not shipped before a real broadcast crop
corpus exists.  The debug sink and evaluator are the data loop needed to make
that next step evidence-driven rather than another threshold guess.
