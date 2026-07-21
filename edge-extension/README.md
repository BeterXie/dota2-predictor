# Dota 2 Live Market Monitor

This unpacked Microsoft Edge Manifest V3 extension passively observes
sanitized RayBet Dota 2 match and odds responses and sends them directly to
the local predictor companion at `127.0.0.1:8765`.

It does not read credentials, account data, balance, bet slips, stake inputs,
or order submissions. It cannot place a real wager.

## Test

```powershell
npm test
```

## Load In Edge

1. Start the predictor companion from `C:\Users\59908\dota2-predictor`:

   ```powershell
   python -m live_betting.browser_companion
   ```

2. Open `edge://extensions` in a normal Edge window.
3. Enable Developer mode and choose **Load unpacked**.
4. Select this `edge-extension` directory.
5. Use the extension card's reload button after local source changes.

The popup reports `Connected` as soon as the companion is reachable. Page
hook, bridge, transport, and classification rows separately confirm that the
RayBet page is actually being observed. Diagnostics retain only counters and
allowlisted host/path metadata; URL queries and response bodies are excluded.
No pairing code or local secret is required.

The capture state distinguishes the current failure boundary:

- `waiting for traffic`: the current RayBet page hook is ready but has not yet
  accepted a Dota event.
- `capturing`: current-page Dota traffic is reaching the companion normally.
- `page hook missing`: the current RayBet page needs a refresh after its
  extension context was replaced or failed to initialize.
- `companion offline`: the loopback companion did not answer the bounded
  status probe.
- `reconnecting`: the companion is reachable while a queued retry drains.
- `backpressure`: the browser-session queue reached 80 percent of an event or
  byte limit.
- `degraded`: an event was lost or a bridge, protocol, validation, or database
  readiness check failed.

`paused` remains an explicit user state, and `unsupported page` means the
active tab is outside the fixed RayBet host allowlist. The popup reason tooltip
and existing counters identify the specific condition without exposing event
payloads.

The companion remains bound to `127.0.0.1` and accepts only the configured
extension Origin. This workspace defaults to the currently loaded extension:

```text
chrome-extension://gfccbmpmpgicjfleahjbokeifhjnemam
```

If Edge assigns a different unpacked extension ID, start the companion with
the exact Origin shown on `edge://extensions`:

```powershell
python -m live_betting.browser_companion `
  --extension-origin chrome-extension://<extension-id>
```

Removing the extension deletes its browser-session queue. The companion still
enforces the Dota-only event contract, payload limits, and forbidden-field
checks. Real wager execution remains permanently disabled.
