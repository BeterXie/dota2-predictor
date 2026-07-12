# RayBet Dota 2 Edge Monitor Extension Implementation Plan

## Goal

Implement the approved passive Edge extension and localhost companion across
`dota2-ad-assistant` and `dota2-predictor`. The completed path captures only
sanitized Dota 2 market traffic, preserves event-time causality, feeds the
existing shadow strategy, and has no real-wager capability.

This is the first deliverable in the larger live-analysis program. Historical
T1 data, position-aware player ratings, draft backtesting, and email
notifications receive separate design and implementation cycles after this
browser data path is working.

## Fixed Boundaries

- Extension source: `C:\Users\59908\dota2-ad-assistant\edge-extension`
- Companion source: `C:\Users\59908\dota2-predictor\live_betting`
- Companion address: `127.0.0.1:8765`
- Predictor database: `C:\Users\59908\dota2-predictor\data\dota2.db`
- Dota 2 identity: `game_id=151`
- Extension code: zero-build classic JavaScript; no runtime dependencies
- Predictor tests: existing `unittest` suite plus focused companion tests
- Extension tests: Node 20 built-in test runner
- Manifest permissions: `storage`, `alarms`, the two fixed RayBet hosts, and
  loopback host access only
- No Git repository is initialized in `dota2-ad-assistant` without separate
  approval.
- No login, bet-slip, stake-entry, account, balance, or order submission code.

## Task 1: Extension Contract Utilities

### Files

- Add `dota2-ad-assistant/edge-extension/package.json`
- Add `dota2-ad-assistant/edge-extension/.gitignore`
- Add `dota2-ad-assistant/edge-extension/src/canonical-json.js`
- Add `dota2-ad-assistant/edge-extension/src/constants.js`
- Add `dota2-ad-assistant/edge-extension/src/redact.js`
- Add `dota2-ad-assistant/edge-extension/src/classify.js`
- Add `dota2-ad-assistant/edge-extension/tests/fixtures/raybet-dota-odds.json`
- Add `dota2-ad-assistant/edge-extension/tests/fixtures/raybet-match-list.json`
- Add `dota2-ad-assistant/edge-extension/tests/canonical-json.test.js`
- Add `dota2-ad-assistant/edge-extension/tests/redact.test.js`
- Add `dota2-ad-assistant/edge-extension/tests/classify.test.js`

### Work

1. Set `package.json` to ESM for Node tests and service-worker modules. Keep the
   page/content helpers as classic IIFEs under one shared namespace, freeze it
   in the final bridge script, and test them through `node:vm`; content scripts
   do not use imports.
2. Implement stable canonical JSON and Web Crypto/Node SHA-256 helpers.
3. Implement the recursive forbidden-key policy plus explicit RayBet field
   allowlists.
4. Enforce depth, node, array, object-key, string, and sanitized-byte limits.
5. Classify match list, complete odds, market update, video,
   `manual_control`, and metadata-only unknown events.
6. Establish a Dota match-ID allowlist only from `game_id=151` data.
7. Emit one match-list event per Dota match so every envelope has one explicit
   `raybet_match_id`.
8. Strip URL query strings/fragments and retain only the separately parsed
   RayBet match ID.
9. Keep fixtures synthetic and credential-free.

### Verify

- `node --test tests/canonical-json.test.js tests/redact.test.js tests/classify.test.js`
- Forbidden-key fixture scan returns no sensitive values.
- Non-Dota and unproven odds payloads produce no retained payload.

## Task 2: Transparent Main-World Capture

### Files

- Add `dota2-ad-assistant/edge-extension/src/main-hook.js`
- Add `dota2-ad-assistant/edge-extension/tests/hook.test.js`
- Add `dota2-ad-assistant/edge-extension/tests/manual-control.test.js`

### Work

1. Wrap Fetch while preserving arguments, return promise, rejection behavior,
   and response body; observe only `response.clone()`.
2. Stream-read the clone with a 1 MiB cap and cancel only the clone when the
   cap is reached.
3. Wrap XHR `open`/`send`, retain URL metadata internally, and schedule capture
   after `loadend` dispatch without changing callbacks or `responseType`.
4. Wrap the WebSocket constructor while preserving prototype, constants,
   constructor behavior, `onmessage`, and page listeners.
5. Emit only bounded JSON strings through a namespaced `postMessage` channel.
6. Buffer at most 60 early candidates/1 MiB until the isolated bridge sends a
   ready handshake, then flush in capture order; the handshake is not a
   security boundary.
7. Add the five-second fixed-path `manualControlData` sampler with descriptor
   checks, visibility gating, and recent-Dota gating.
8. Never read request headers/bodies, cookies, storage, DOM text, form fields,
   or framework internals.

### Verify

- `node --test tests/hook.test.js tests/manual-control.test.js`
- The original Fetch/XHR/WebSocket results and error order are identical with
  capture enabled and disabled.
- Oversized and malformed candidates do not affect the page-facing operation.
- A page-head Fetch that completes before the isolated bridge is ready is
  flushed once, in capture order, after the ready handshake.

## Task 3: Isolated Bridge And Event Envelope

### Files

- Add `dota2-ad-assistant/edge-extension/src/content-bridge.js`
- Add `dota2-ad-assistant/edge-extension/tests/content-bridge.test.js`

### Work

1. Accept only top-frame, same-window, namespaced string messages.
2. Check token bucket and raw string size before JSON parsing.
3. Validate, classify, sanitize, and canonicalize before hashing or forwarding.
4. Generate schema-v1 envelopes with random session ID, monotonic per-tab
   sequence, UTC capture time, payload hash, byte count, and reason code.
5. Emit unknown/oversized structures as metadata-only according to the design.
6. Mark all page-state manual control samples `diagnostic_untrusted`.
7. Send accepted envelopes to the service worker and update local counters
   without exposing payloads to logs.

### Verify

- `node --test tests/content-bridge.test.js`
- Forged object, deep tree, cyclic sanitizer input, oversized string, and
  candidate flood tests terminate within fixed bounds.
- Event IDs remain stable across delivery retries but differ across captures.

## Task 4: Predictor Event-Time Storage

### Files

- Modify `dota2-predictor/live_betting/storage.py`
- Modify `dota2-predictor/live_betting/markets.py`
- Modify `dota2-predictor/live_betting/monitor.py`
- Modify `dota2-predictor/live_betting/shadow_monitor.py`
- Add `dota2-predictor/tests/test_browser_storage.py`

### Work

1. Enable SQLite foreign-key checks and add a re-entrant
   `LiveBettingStore.transaction()` context; suppress helper
   auto-commit only while it is active and preserve legacy behavior otherwise.
2. Add a named savepoint context for normalization failures.
3. Add `browser_events` and `odds_transport_observations` tables and indexes,
   plus a trigger that rejects payload mutation while allowing processing
   status/reason updates.
4. Add strict insert/update helpers for immutable audit payloads and mutable
   processing status only.
5. Add an optional `received_at` to `snapshots_from_payload()` while preserving
   the direct collector default.
6. Record each complete direct `/odds` response and all semantic changes in one
   transaction.
7. Compare a market state with its nearest event-time predecessor, not the
   latest inserted row.
8. Change all current market queries to order by
   `(received_at DESC, id DESC)`.
9. Mark an older observation `late`; persist only its audit/transport row and
   never write semantic odds, notify strategy, or satisfy a fill.
10. Resolve next-observation hypothetical fills through a non-late transport
   observation, including unchanged later polls.
11. Add insert-only browser match metadata that never overwrites direct-owned
    metadata, raw detail, or `live_url`.

### Verify

- `python -m unittest tests.test_browser_storage -v`
- `python -m unittest tests.test_live_betting -v`
- Injected mid-response failure rolls back both direct semantic changes and
  its transport observation.
- `t1=A`, unchanged `t3=A`, then delayed `t2=B` leaves current state at `A`.
- Future/late browser events never affect an earlier or current live decision.

## Task 5: Companion Contract, Pairing, And Authentication

### Files

- Add `dota2-predictor/live_betting/browser_contract.py`
- Add `dota2-predictor/live_betting/browser_auth.py`
- Add `dota2-predictor/tests/test_browser_contract.py`
- Add `dota2-predictor/tests/test_browser_auth.py`

### Work

1. Define strict schema-v1 Pydantic models with forbidden extras, UTC
   timestamps, enum validation, exact hash shapes, Dota identity rules, and
   payload limits.
2. Recompute `payload_hash`; validate `event_id` as 64-hex and use it for
   deduplication, but do not claim it can be recomputed because the envelope
   intentionally omits the per-tab sequence.
3. Repeat the forbidden-key scan on payloads and unknown envelope keys before
   any database operation; do not misclassify the legitimate
   `capture_session_id` envelope key.
4. Persist pairing state under `%LOCALAPPDATA%\Dota2Predictor` with a random
   256-bit secret protected by Windows DPAPI and a paired extension origin;
   never use the repository.
5. Generate a ten-minute, one-use pairing code and add explicit local reset.
6. Implement HMAC-SHA256 over the exact UTF-8 string
   `timestamp\nnonce\nMETHOD\n/path\nbody_sha256`, with timestamp as Unix
   milliseconds.
7. Enforce a 30-second request window, bounded five-minute nonce cache, exact
   extension origin, body limits, and pairing/event rate limits.
8. Limit pairing to 5/minute, events to 120/minute, and status to 60/minute per
   origin.
9. Return stable error codes without logging bodies, codes, or secrets.

### Verify

- `python -m unittest tests.test_browser_contract tests.test_browser_auth -v`
- Test expired/reused pairing code, wrong origin, bad signature, stale request,
  nonce replay, forbidden field, and oversized body.
- Credential scan finds no secret in fixtures, SQLite, or logs.

## Task 6: FastAPI Companion And Browser Dispatch

### Files

- Add `dota2-predictor/live_betting/browser_companion.py`
- Add `dota2-predictor/scripts/run_browser_companion.py`
- Add `dota2-predictor/tests/test_browser_companion.py`
- Modify `dota2-predictor/live_betting/README.md`

### Work

1. Build `create_app()` for isolated tests and a CLI that binds only
   `127.0.0.1:8765`.
2. Implement `GET /health`, `POST /v1/pair`, `POST /v1/events`, and
   `GET /v1/status`.
3. Perform raw body/security validation before per-event model validation.
4. Process each valid event in its own transaction and each batch sequentially
   with one request-local store connection.
5. Insert the browser audit row before a normalization savepoint; on parser
   failure commit only the audit error result.
6. Dispatch only recognized complete Dota odds through the existing market
   parser. Keep match list, manual control, video, and unknown data audit-only
   unless their design explicitly permits metadata insertion.
7. Reject late events from semantic processing and return per-event accepted,
   duplicate, or rejected results.
8. Expose aggregate status only; never expose event payloads.
9. Add dynamic exact-origin CORS behavior for pairing and paired requests.
10. Handle unauthenticated `OPTIONS` preflight without data access: accept any
    syntactically valid extension origin only for `/v1/pair`, and only the exact
    paired origin for other routes. Allow the four protocol headers and
    `Content-Type`; authenticate the following real request normally.

### Verify

- `python -m unittest tests.test_browser_companion -v`
- Mixed valid/invalid event batch persists only allowed events.
- Retry of an accepted event is duplicate and performs no normalization.
- Parser failure leaves an error audit row; database unavailability does not
  claim persistence.

## Task 7: Disabled Execution Boundary

### Files

- Add `dota2-predictor/live_betting/execution.py`
- Add `dota2-predictor/tests/test_disabled_execution.py`

### Work

1. Add only `DisabledExecutionAdapter` and a result model.
2. Make `execute()` return `execution_disabled` without network, browser,
   filesystem, subprocess, or database writes.
3. Keep existing shadow order states `pending`, `filled`, and `rejected`.
4. Add no registry, dynamic import, feature flag, URL, credential field, or
   alternate adapter.

### Verify

- `python -m unittest tests.test_disabled_execution -v`
- Static search finds no real execution client, order endpoint, click action,
  stake entry, account field, or alternate adapter.

## Task 8: Companion Client, Session Queue, And Retry

### Files

- Add `dota2-ad-assistant/edge-extension/src/companion-client.js`
- Add `dota2-ad-assistant/edge-extension/src/queue.js`
- Add `dota2-ad-assistant/edge-extension/src/service-worker.js`
- Add `dota2-ad-assistant/edge-extension/tests/companion-client.test.js`
- Add `dota2-ad-assistant/edge-extension/tests/queue.test.js`
- Add `dota2-ad-assistant/edge-extension/tests/service-worker.test.js`

### Work

1. Implement one-time pairing and Web Crypto HMAC headers using the exact
   newline-delimited string and Unix-millisecond timestamp defined above.
2. Store only approved configuration in `chrome.storage.local` and events in
   `chrome.storage.session`.
3. Maintain the 1,000-event/5 MiB drop-oldest queue and reason counters.
4. Serialize every session-storage read/modify/write through one service-worker
   promise mutex; popup/options code never writes the queue directly.
5. Batch at most 50 events/1 MiB and remove only acknowledged IDs.
6. Deduplicate event IDs within the session queue.
7. Implement one-to-60-second bounded exponential retry with jitter; schedule
   a one-shot `chrome.alarms` fallback before service-worker suspension and
   clear it after successful acknowledgement.
8. Stop delivery on authentication failure until re-paired; never flood the
   companion.
9. Maintain `capturing`, `buffering`, `paused`, `unsupported_page`, and `error`
   state without affecting page behavior.
10. Implement the service worker as an ES module importing the pure queue and
    companion client; keep content scripts classic.
11. Restrict both `chrome.storage.local` and `chrome.storage.session` access to
    trusted extension contexts.

### Verify

- `node --test tests/companion-client.test.js tests/queue.test.js tests/service-worker.test.js`
- Service-worker restart restores the session queue and retry state.
- Browser-session replacement creates a new session ID and marks cross-session
  coverage unknown.

## Task 9: Manifest, Popup, Options, And Documentation

### Files

- Add `dota2-ad-assistant/edge-extension/manifest.json`
- Add `dota2-ad-assistant/edge-extension/src/popup.html`
- Add `dota2-ad-assistant/edge-extension/src/popup.js`
- Add `dota2-ad-assistant/edge-extension/src/popup.css`
- Add `dota2-ad-assistant/edge-extension/src/options.html`
- Add `dota2-ad-assistant/edge-extension/src/options.js`
- Add `dota2-ad-assistant/edge-extension/src/options.css`
- Add `dota2-ad-assistant/edge-extension/README.md`
- Add `dota2-ad-assistant/edge-extension/tests/manifest.test.js`

### Work

1. Register top-frame `document_start` main-world and isolated-world scripts;
   the main hook is self-contained, while classic isolated helpers load in
   manifest order and the bridge freezes their shared namespace. Register the
   background service worker with `type: "module"`.
2. Request only `storage`, `alarms`, the two fixed RayBet hosts, and loopback
   host access.
3. Add no cookie, tabs, history, webRequest, download, clipboard, or all-URL
   permission.
4. Build a compact popup for state, companion health, recognized match count,
   last event, queue/drop counts, pause, and optional loopback report link.
5. Build an options page only for pairing, enabled fixed domains, and debug
   level.
6. Use packaged scripts only and a strict extension CSP.
7. Document Edge unpacked loading, companion startup, pairing, pause/reset,
   logs/counters, and complete removal.

### Verify

- `node --test tests/manifest.test.js`
- `npm test`
- Manifest permission allowlist test rejects any expanded permission.
- Long labels fit at popup width without overlap.

## Task 10: Deterministic Local Integration

### Files

- Add `dota2-ad-assistant/edge-extension/tests/integration/server.mjs`
- Add `dota2-ad-assistant/edge-extension/tests/integration/test-page.html`
- Add `dota2-ad-assistant/edge-extension/tests/integration/test-socket.mjs`
- Add `dota2-ad-assistant/edge-extension/tests/integration/make-test-bundle.mjs`
- Add `dota2-predictor/tests/fixtures/browser_event_batch.json`

### Work

1. Serve deterministic HTTPS/WSS Fetch, XHR, WebSocket, non-Dota, oversized,
   forbidden, and manual-control cases from loopback.
2. Launch an isolated Edge profile with `--load-extension`, map
   `www.ray086.com` to loopback through `--host-resolver-rules`, and use a local
   test port/certificate. Keep the production manifest unchanged and do not add
   localhost page permission.
3. Start a temporary companion against a temporary SQLite database.
4. Pair, capture, retry, pause/resume, and reconnect without DevTools.
5. Compare page-visible results with capture enabled and disabled.
6. Verify audit rows, transport observations, semantic odds, duplicate results,
   and zero forbidden fields.

### Verify

- Full predictor unit suite.
- Full extension `npm test` suite.
- Local end-to-end smoke test reports identical page behavior and expected
  database counts.

## Task 11: Ordinary Edge And Real Dota Smoke Test

### Work

1. Start the real companion on `127.0.0.1:8765` and verify `/health`.
2. Load the production directory as an unpacked Edge extension in a normal
   browser profile.
3. Enter the one-time pairing code in options.
4. Observe a real RayBet Dota 2 page passively; do not open or manipulate the
   bet slip.
5. Confirm Fetch/XHR/WebSocket coverage available on that page and capture one
   full match when scheduling permits.
6. Compare extension counters, companion acknowledgements, audit rows,
   normalized odds, direct observations, queue loss, and duplicate rates.
7. Scan extension session storage, HTTP fixtures, SQLite, and logs for forbidden
   fields.
8. Leave the companion running only after health, authentication, and database
   paths are confirmed.

### Verify

- Page behavior is unchanged with the extension enabled.
- Only proven Dota 2 payloads are retained.
- `manualControlData...time` remains diagnostic and never populates game time.
- Delayed events cannot move current odds backward or trigger strategy.
- Every simulated execution result is `execution_disabled`.
- No real order, account, balance, bet-slip, or stake capability exists.

## Final Verification Commands

Run from `C:\Users\59908\dota2-predictor`:

```powershell
python -m unittest discover -s tests -v
python -m live_betting.browser_companion --check-config
rg -n -i "place.?bet|submit.?bet|account|balance|cookie|authorization|stake" live_betting tests
git diff --check
```

Run from `C:\Users\59908\dota2-ad-assistant\edge-extension`:

```powershell
npm test
rg -n -i "cookies|webRequest|<all_urls>|place.?bet|submit.?bet|balance|stake" .
```

Review every static-search hit in context. Test names and explicit forbidden
field lists are expected; executable real-wager or sensitive-capture paths are
not.
