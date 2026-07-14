# Dota 2 Live Market Monitor

This unpacked Microsoft Edge Manifest V3 extension passively observes
sanitized RayBet Dota 2 match and odds responses and sends them to the local
predictor companion at `127.0.0.1:8765`.

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
5. Open the extension settings and enter the one-time pairing code printed by
   the companion.

Use the popup to pause capture and view queue health. Removing the extension
deletes its browser-session queue and local pairing secret.

If the extension was removed, reinstalled with a different ID, or lost its
local secret, stop the companion and reset its persisted pairing:

```powershell
cd C:\Users\59908\dota2-predictor
python -m live_betting.browser_companion --reset-pairing
python -m live_betting.browser_companion
```

Then enter the newly printed one-time code in the extension settings.
