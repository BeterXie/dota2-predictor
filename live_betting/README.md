# Dota 2 Live Shadow Betting

This package records RayBet Dota 2 odds and creates hypothetical orders only.
It has no real betting endpoint.

## Current Capability

- RayBet Dota 2 discovery (`game_id=151`) and immutable odds snapshots
- Winner, total kills, team total kills, kill handicap, race-to-kills, and
  duration market normalization and settlement
- Complete-outcome-group de-vigging
- Next-snapshot fills, slippage rejection, idempotent shadow orders
- Brier, log-loss, fill-rate, and shadow ROI helpers
- Explicit odds-source backoff
- Causal visual-clock alignment with no future-frame interpolation
- Team style, roster-change, player form, and draft timing profiles
- Explainable comeback decisions with one shadow attempt per map
- Exact-draft OpenDota post-match labeling and JSON evaluation reports
- Append-only research-only live predictions with successor-price and result labels

## Commands

One read-only collection pass:

```powershell
python -m live_betting.monitor --once
```

Continuous collection with the production-safe polling intervals:

```powershell
python -m live_betting.monitor --database data/dota2.db `
  --raw-dir data/live_betting/raw --interval 6 --list-interval 30
```

Run tests:

```powershell
python -m pytest -q
```

Raw snapshots are written under `data/live_betting/raw/`; normalized rows use
the existing `data/dota2.db` database.

Start one visual watcher for every active RayBet match that exposes an HLS
stream. Observations, evidence frames, and watcher logs stay under the project
data directory:

```powershell
python scripts/supervise_raybet_streams.py --database data/dota2.db
```

Run the strategy monitor against that observation directory:

```powershell
python scripts/run_comeback_shadow.py `
  --database data/dota2.db `
  --vision-jsonl data/live_betting/live_observations
```

For every strictly mapped map with a complete market surface and confirmed
ten-hero observation, the same worker also appends a non-actionable research
prediction. Missing deployment/calibration artifacts remain `null` with an
explicit gate reason. These rows never enter `shadow_orders`; manual-control
page time remains `diagnostic_untrusted` even after continuity checks.

For passive browser capture, start `python -m live_betting.browser_companion`
and load `edge-extension/` as an unpacked Edge extension. It connects directly
to the localhost companion; setup and safety details are documented in
`edge-extension/README.md`.

Refresh the local hero-recognition asset after the Dota hero roster changes:

```powershell
python scripts/fetch_hero_portraits.py
python scripts/build_hero_features.py
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

The report includes research coverage, next-price movement, settled accuracy,
Brier score, and log-loss. Model metrics remain `null` until a deployable model
has produced raw probabilities and post-match labels exist.

Deliver simulation notifications from the transactional outbox (credentials
are read only from `DOTA2_SMTP_SENDER` and `DOTA2_SMTP_AUTH_CODE`, or an
installed OS keyring):

```powershell
python scripts/run_notification_worker.py --database data/dota2.db --once
```

The worker uses `smtp.qq.com:465` with certificate-verified implicit TLS. A
missing SMTP configuration reports `mail_unhealthy` and does not stop odds
collection or shadow evaluation. Every message states that no real wager was
placed.

Run the local health/report supervisor once (safe for verification) or keep it
running with only explicitly selected components:

```powershell
python scripts/run_dota_shadow_service.py --once --database data/dota2.db
```

The supervisor uses a single-instance lock, writes component health and strict
coverage reports, and leaves collection, shadow, and mail workers stopped until
their `--start-*` flags are supplied.

`strategy_decisions` retains rejected decisions and their reasons. A result is
descriptive below 100 settled shadow orders and remains provisional below 500.
Post-match fields are written only after RayBet marks the series completed and
are never used by a decision from that series.
