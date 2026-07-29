# Frontend UX hardening — 2026-07-27

## Scope

This change refreshes the operator-facing web UI without modifying prediction,
order, settlement, storage, collector, or production-write behavior.

Parent commit: `159e754f9776d72e6b0dd8c3b8576799dcea1937`

## Changes

- Added a persistent `PAPER ONLY` safety boundary to the React monitor shell.
- Projected live capability and milestone-governance status into the monitor UI.
- Increased base typography, muted-text contrast, spacing, and mobile hero-label size.
- Removed the workspace-wide live region to prevent repetitive screen-reader output.
- Rebuilt the legacy matches page with shared navigation, responsive cards, and
  DOM-safe rendering of remote values.
- Refreshed the pre-match page with shared visual tokens, balanced Radiant/Dire
  layout, mobile stacking, keyboard-operable hero selection, and dialog semantics.
- Fixed the pre-match Matches link to target `/matches`.
- Added URL validation and image fallbacks for hero artwork.
- Added source-level regression checks for the legacy shells and monitor safety bar.

## Validation

Passed:

- `pytest -q tests/test_frontend_shells.py` — 6 passed.
- `npm test -- --reporter=dot` — 174 passed.
- `npm run build` — TypeScript and Vite production build passed; Vite reported
  only the existing large-chunk advisory.
- Inline JavaScript syntax checks for `web/static/index.html` and
  `web/static/prematch.html` using `node --check`.
- TypeScript/TSX syntax transpilation checks for the changed React sources.
- Headless Chromium smoke validation with mocked API responses:
  - desktop and mobile rendering;
  - no horizontal overflow;
  - external text is not executed as markup;
  - hero-picker Enter/Escape and focus return;
  - missing hero image fallback.
- `git diff --check`.

## Safety decision

This frontend change does not alter the existing P3/P4 authorization state. It
adds clearer operator-visible boundaries but does not grant live-canary,
production-deployment, real-betting, or database-mutation authority.
