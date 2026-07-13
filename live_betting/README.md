# Dota 2 Live Shadow Betting

This package records RayBet Dota 2 odds and, when provisioned, PandaScore live
data. It creates hypothetical orders only. It has no real betting endpoint.

## Current Capability

- RayBet Dota 2 discovery (`game_id=151`) and immutable odds snapshots
- PandaScore Dota 2 fixtures, `/lives` endpoint discovery, frames/events sockets
- Conservative cross-provider fixture matching
- Winner, total kills, team total kills, kill handicap, race-to-kills, and
  duration market normalization and settlement
- Complete-outcome-group de-vigging
- Next-snapshot fills, slippage rejection, idempotent shadow orders
- Brier, log-loss, fill-rate, and shadow ROI helpers
- Explicit odds-source backoff and commercial-feed-disabled state
- Causal visual-clock alignment with no future-frame interpolation
- Team style, roster-change, player form, and draft timing profiles
- Explainable comeback decisions with one shadow attempt per map
- Exact-draft OpenDota post-match labeling and JSON evaluation reports

PandaScore does not currently document Dota 2 support in its sandbox or event
recovery feature. Record live Dota payloads locally before changing the frame
and event mappings. Any WebSocket gap freezes signal generation.

## Configuration

PandaScore is disabled by default, even when a token remains in `.env`. To use
the optional commercial fixture linker, set the secret in `.env` and opt in on
the command line:

```dotenv
PANDASCORE_TOKEN=
```

```powershell
python -m live_betting.monitor --enable-pandascore
```

The normal RayBet collector commands below never call PandaScore.

## Commands

One read-only collection pass:

```powershell
python -m live_betting.monitor --once
```

Continuous collection (15-second match refresh, 3-second odds refresh):

```powershell
python -m live_betting.monitor
```

After PandaScore Live access is provisioned, collect frames and events:

```powershell
python -m live_betting.pandascore_monitor
```

Run tests:

```powershell
python -m unittest discover -s tests -v
```

Raw snapshots are written under `data/live_betting/raw/`; normalized rows use
the existing `data/dota2.db` database.

Run the strategy monitor against the visual observation directory:

```powershell
python scripts/run_comeback_shadow.py `
  --database data/dota2.db `
  --vision-jsonl C:/Users/59908/dota2-ad-assistant/logs/live_observations
```

Run exact-draft post-match labeling for every completed observed series:

```powershell
python scripts/run_postmatch_labeler.py --database data/dota2.db --all
```

Backfill recent historical detail for a known OpenDota team ID:

```powershell
python scripts/backfill_team_profiles.py --team-id 5014799 --limit 30
```

Generate the current shadow report:

```powershell
python -m live_betting.report --database data/dota2.db `
  --output data/live_betting/shadow_report.json
```

`strategy_decisions` retains rejected decisions and their reasons. A result is
descriptive below 100 settled shadow orders and remains provisional below 500.
Post-match fields are written only after RayBet marks the series completed and
are never used by a decision from that series.
