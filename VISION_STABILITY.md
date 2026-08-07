# Stable realtime vision runtime

This branch adds a shadow-safe stabilization layer around the existing RayBet
vision watcher.  It does not change the P0/P1 prediction feature boundary and
it does not add a database migration.

## What is included

- sticky layout selection with acquisition, grace and challenger hysteresis;
- content-derived frame identity so repeated evidence is no longer keyed by the
  HLS URL hash;
- a DraftTracker fix that treats a progressing game clock as independent
  evidence even when the stream identity is unchanged;
- ten-slot global hero assignment using the existing hero feature scores and
  SciPy Hungarian assignment;
- HUD perception that continues to read clock, score, net-worth and heroes on
  replay/untrusted frames while publication remains fail-closed;
- freeze semantics for replay/untrusted frames: accumulated trackers are not
  reset by the stable launcher;
- frame-quality/freeze diagnostics;
- optional rate-limited failure-frame and hero-crop capture;
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

## Real-frame corpus

Create a JSONL manifest next to captured frames.  Each row may contain:

```json
{"file":"game_001.jpg","layout":"epl_s39_live_1080p","scene":"game","radiant_heroes":[1,2,3,4,5],"dire_heroes":[6,7,8,9,10]}
```

Evaluate with:

```bash
python scripts/evaluate_vision_stability.py path/to/manifest.jsonl
```

## Deliberate boundary

This implementation is the infrastructure/stability phase.  A learned ONNX
hero classifier is intentionally not shipped before a real broadcast crop
corpus exists.  The debug sink and evaluator are the data loop needed to make
that next step evidence-driven rather than another threshold guess.
