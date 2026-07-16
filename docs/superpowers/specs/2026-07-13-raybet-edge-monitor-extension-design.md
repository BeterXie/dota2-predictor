# RayBet Dota 2 Edge Monitor Extension Design

## Approved Local-Access Override (2026-07-15)

The operator has explicitly chosen a zero-pairing local workflow because manual
pairing is too burdensome for this single-user deployment. This decision
supersedes this document's one-time pairing, shared-secret, HMAC, timestamp,
and nonce requirements, including the corresponding tests and acceptance
criterion.

The implemented boundary remains fixed to `127.0.0.1:8765`, an exact configured
`chrome-extension://` origin, the supported extension-version header, strict
CORS, request-size limits, and per-origin rate limits. This is an accepted
local threat-model tradeoff: it does not protect against a malicious local
process capable of forging the configured Origin and version headers, and the
companion must not be described as cryptographically authenticated.

## Objective

Build a Microsoft Edge Manifest V3 extension that observes RayBet Dota 2 page
traffic in an ordinary browser window, removes sensitive data before it leaves
the extension, and forwards sanitized events to the existing local shadow
betting system.

The first version is passive. It must not change page requests, page responses,
WebSocket behavior, betting controls, or account state. It may support complete
dry-run order construction, validation, audit, and simulated fills, but no code
path may submit a real wager.

## Scope

### In Scope

- Microsoft Edge Manifest V3, running without DevTools.
- RayBet Dota 2 only, identified by `game_id=151` and a known Dota match ID.
- Observation of relevant Fetch, XMLHttpRequest, WebSocket, and selected public
  page-state data.
- Sanitization in the browser before transmission.
- Origin/version-gated delivery to a companion bound to `127.0.0.1`.
- A separate immutable browser-event audit trail in the predictor database.
- Reuse of existing RayBet market normalization, causal alignment, comeback
  strategy, shadow order, settlement, and reporting code.
- A small popup for capture health and an options page for local capture settings.

### Out of Scope

- Login automation or credential storage.
- Reading or recording bet-slip selections, stake entry, balance, wallet,
  account, deposit, withdrawal, or submission activity.
- Clicking betting controls or constructing page DOM actions for a wager.
- CAPTCHA handling, anti-bot evasion, fingerprint spoofing, or risk-control
  bypass.
- Capturing football, other esports, or any `game_id` other than `151`.
- Treating `manualControlData.data[currentIndex].time` as the Dota game clock.
- Replacing the visual clock and draft-recognition pipeline.
- A commercial live-data integration or STRATZ integration.
- Persistent raw network archives inside the browser profile.

## Repository Boundary

The extension lives in:

```text
C:\Users\59908\dota2-predictor\edge-extension
```

It owns browser hooks, sanitization, buffering, local status UI, and delivery. It
does not import predictor Python code and does not read the predictor database.

The companion lives in:

```text
C:\Users\59908\dota2-predictor\live_betting\browser_companion.py
```

It owns loopback/origin/version access checks, schema validation, deduplication,
audit persistence,
and dispatch into the existing `live_betting` modules. The predictor remains
the sole owner of odds normalization, decisions, shadow orders, simulated
fills, settlement, and evaluation.

The repositories communicate only through the versioned localhost event
contract in this document.

## Selected Architecture

Use a Manifest V3 main-world hook with an isolated validation bridge:

```text
RayBet page
  -> main-world document_start hook
  -> isolated content-script validator and sanitizer
  -> service-worker session queue
  -> origin/version-gated localhost batch
  -> FastAPI companion on 127.0.0.1
  -> browser_events audit table
  -> recognized RayBet normalization
  -> existing shadow strategy and reports
```

This architecture sees page-owned Fetch, XHR, and WebSocket traffic in a normal
Edge window. The isolated content script, rather than page-world code, owns the
accepted event schema and payload hash. The companion treats every browser
event as untrusted even after extension-side validation.

Direct polling remains available as an independent source. Browser events do
not silently overwrite direct-collector snapshots; source and receipt time are
retained so duplicate observations can be explained.

This is a browser-page transport observer. It does not hook the Dota 2 or Steam
process, inject a DLL, inspect game memory, or read game files. It therefore
does not alter the existing assistant boundary that prohibits game-process API
hooks and memory inspection.

## Extension Components

The implementation is divided into focused modules:

```text
edge-extension/
  manifest.json
  package.json
  README.md
  src/
    main-hook.js
    content-bridge.js
    classify.js
    redact.js
    canonical-json.js
    service-worker.js
    companion-client.js
    popup.html
    popup.js
    popup.css
    options.html
    options.js
    options.css
  tests/
    fixtures/
    hook.test.js
    classify.test.js
    redact.test.js
    queue.test.js
    companion-client.test.js
```

The extension uses unpacked, zero-build JavaScript supported directly by Edge.
There is no bundler and no runtime package dependency. Unit tests use the Node
20 built-in test runner with small fake browser/page APIs, isolated from the
Python `pytest` suite.

### Main-World Hook

`main-hook.js` runs at `document_start` in the page's main JavaScript world. It
wraps only the minimum APIs needed for observation:

- `window.fetch`
- `XMLHttpRequest.prototype.open` and `send`
- the `WebSocket` constructor and message events

The wrapper must preserve function arguments, return values, promise behavior,
`this` binding, exceptions, response streaming, XHR ready-state events,
WebSocket constants, and page-installed handlers.

For Fetch, it calls the original function first and reads only a
`response.clone()`. A parse failure cannot reject or delay the page's original
promise. The clone reader stops after a 1 MiB raw-response cap and cancels only
the clone. For XHR, it schedules observation after `loadend` dispatch completes
and never changes `responseType`, callbacks, or response fields; a response
over the raw cap is not parsed. For WebSocket, it adds a message listener to the
created socket, checks message size before parsing, and does not replace
`onmessage` or transform message data.

The hook observes response data only. It never records request headers,
cookies, authorization material, or request bodies. It ignores non-RayBet
hosts and endpoints that cannot contain match, market, video, or public match
state.

The hook emits candidates through a namespaced `window.postMessage` channel.
Because page scripts can imitate this channel, it is a transport mechanism and
not a trust boundary.

The hook serializes its candidate as a JSON string before posting it. The
bridge rejects non-string messages and strings over 1 MiB before parsing. It
uses an iterative bounded sanitizer with maximum depth 32, 20,000 visited
nodes, 5,000 array items, 256 keys per object, 64 KiB per string, cycle
detection, and a 256 KiB sanitized-output budget. A token bucket accepts at
most 30 candidates per second with a burst of 60 per top-level frame. Limit
failures increment a reason-specific counter and discard the candidate without
hashing or recursively traversing it further.

After a Dota `/v2/odds` response establishes the active match, a diagnostic
page-state sampler may read only the fixed public path
`window.manualControlData.data[window.manualControlData.currentIndex].time`.
It runs every five seconds only while the top-level page is visible and a Dota
odds response was seen in the preceding 30 seconds. It copies only primitive
`currentIndex` and `time` values, never traverses the surrounding object, and
emits `manual_control` with transport `page_state` and
`capture_reason='diagnostic_untrusted'`. If the global or path is absent, it
does nothing. Accessor properties are skipped so sampling cannot invoke a page
getter; the sampler never searches framework internals or the DOM.

### Isolated Content Bridge

`content-bridge.js` is the first trust boundary. It verifies that the message
origin is the current window, rejects invalid types and structures, classifies
the payload, sanitizes it, applies size limits, canonicalizes it, and creates
the event envelope.

Known `/v2/match` payloads are filtered to rows where `game_id=151`. Their match
IDs form the page-session Dota allowlist. `/v2/odds` and WebSocket data are
accepted only when they contain `game_id=151` or reference a Dota match ID
already established by a match payload. Candidates that cannot be proven to
be Dota 2 are counted locally as ignored and their payload is discarded.

Recognized endpoints use a positive field allowlist plus recursive forbidden
key removal. Unknown endpoint or page-state structures may generate a
metadata-only `unknown` event containing endpoint, byte length, and payload
hash, but never the unknown payload itself. Unknown events cannot be dispatched
to odds normalization or strategy evaluation.

### Service Worker

The service worker owns deduplication, batching, retry, capture state, and
popup status. It keeps at most 1,000 sanitized events or 5 MiB, whichever is
reached first. The queue uses `chrome.storage.session`, so it survives service
worker suspension but is cleared when the browser session ends. It never uses
`chrome.storage.local` for event payloads.

When full, the queue drops the oldest event, increments a monotonic dropped
counter for the current browser session, and continues without affecting the
RayBet page. A batch contains at most 50 events and 1 MiB. Successful event IDs
are removed only after the companion acknowledges them.

Retry uses bounded exponential backoff with jitter, beginning at one second and
capped at 60 seconds. Short retries use a timer, while a one-shot
`chrome.alarms` wakeup preserves the retry after Manifest V3 service-worker
suspension. Successful delivery clears the alarm. A companion outage never
triggers unbounded browser work or a page-visible error.

### Popup

The popup is operational and compact. It shows:

- state: `capturing`, `buffering`, `paused`, `unsupported_page`, or `error`
- companion reachability and protocol compatibility
- number of recognized Dota matches in the current session
- last accepted event type and timestamp
- queued and dropped event counts
- a pause/resume toggle
- a link to the existing local shadow report when configured

Pausing stops new capture and delivery but does not mutate the page. Events
already acknowledged remain in the companion audit log. The popup contains no
bet controls, stake fields, balance, or order-submission controls.

### Options Page

The options page is limited to enabled-domain toggles within the manifest's
fixed RayBet host allowlist.

Diagnostic logs contain event IDs, types, sizes, status codes, and reason codes
only. They never contain sanitized payload bodies, secrets, account data, or
request data.

## Manifest Permissions

The manifest requests only:

- `storage` for capture configuration and the session queue
- `alarms` for one-shot delivery retry after service-worker suspension
- host access for `https://ray086.com/*`, `https://www.ray086.com/*`,
  `https://cfinfo.365raylinks.com/*`, and
  `https://iminfo.esportsworldlink.com/*`
- `http://127.0.0.1/*` for the companion; the client itself connects only to
  port `8765` because Chromium match patterns cannot narrow host access by port

It does not request `cookies`, `webRequest`, `webRequestBlocking`, `tabs`,
`history`, `downloads`, `clipboardRead`, `clipboardWrite`, or broad
`<all_urls>` access. The content scripts are statically registered for the
fixed RayBet host list and run at `document_start`; the observation hook uses
the manifest's main-world execution support.

The extension Content Security Policy permits only its packaged scripts and
localhost connection. There is no remote code, `eval`, or dynamically loaded
script.

## Event Contract

Every delivered event uses schema version 1:

```json
{
  "schema_version": 1,
  "event_id": "sha256-hex",
  "capture_session_id": "random-session-hex",
  "captured_at_utc": "2026-07-13T08:12:34.567Z",
  "page_origin": "https://www.ray086.com",
  "page_path": "/sports/esports",
  "source_path": "/v2/odds",
  "transport": "fetch",
  "event_type": "odds",
  "raybet_match_id": "38407985",
  "game_id": 151,
  "payload": {},
  "payload_hash": "sha256-hex",
  "payload_bytes": 12345,
  "capture_reason": null,
  "extension_version": "0.1.0"
}
```

Allowed `transport` values are `fetch`, `xhr`, `websocket`, and `page_state`.
Allowed `event_type` values are `match_list`, `odds`, `market_update`, `video`,
`manual_control`, and `unknown`.

`page_origin`, `page_path`, and `source_path` exclude query strings and
fragments. The separately parsed `raybet_match_id` is the only accepted query
value. No general query map is retained.

`capture_session_id` is a random 128-bit value stored only in
`chrome.storage.session`. It marks browser-session boundaries without creating
a persistent browser identifier. `payload_hash` is SHA-256 over canonical
sanitized JSON. `event_id` is SHA-256 over the session ID, capture timestamp,
monotonic per-tab sequence, transport, source path, match ID, and payload hash.
Retries preserve the same event ID. The hash input contains no machine,
account, profile, or long-lived browser identifier.

The maximum sanitized payload is 256 KiB per event. An oversized recognized
event becomes metadata-only with an empty payload, original sanitized byte
length, hash of the full sanitized value, and `capture_reason` set to
`payload_too_large`. A candidate that reaches the 1 MiB raw cap is not parsed;
it uses the empty-object hash, a lower-bound byte count, and
`capture_reason='raw_payload_too_large'`. Unknown or unparsed structures also
use the empty-object hash rather than hashing potentially sensitive raw data.
Payload text is decoded only as UTF-8 JSON. Binary WebSocket frames are
metadata-only unless a later schema version explicitly defines a safe decoder.

### Classification Rules

- `match_list`: Dota rows from RayBet match-list responses.
- `odds`: a complete RayBet odds response for one established Dota match.
- `market_update`: a partial market update tied to an established Dota match.
- `video`: public stream or playback state required by the visual pipeline;
  signed query parameters and tokens are removed.
- `manual_control`: public series/map control state retained for diagnostics.
- `unknown`: metadata only and never strategy-eligible.

`manualControlData.data[currentIndex].time` is retained only when it passes the
sanitizer and is labeled `diagnostic_untrusted`. It cannot populate
`game_clock_seconds`, align odds to a game minute, or satisfy a strategy clock
requirement. The existing video-derived clock remains authoritative.

## Data Minimization And Redaction

Redaction happens before hashing, queuing, logging, or transmission. The bridge
recursively removes keys whose normalized names indicate:

- cookie, authorization, bearer, token, secret, session, or CSRF material
- user, member, account, profile, username, phone, email, or identity data
- balance, wallet, currency, deposit, withdrawal, rebate, or transaction data
- device, fingerprint, advertising, analytics, or persistent client IDs
- bet slip, selection slip, stake, potential return, order, submit, or ticket
- request headers, response headers, request body, form data, or POST body

Matching is case-insensitive and ignores punctuation and separators. The known
RayBet extractors also use explicit allowed keys so a newly introduced field is
not retained merely because its name escaped the denylist.

Video URLs retain only scheme, host, and path when the host is allowlisted.
All query parameters and fragments are removed. Page URLs are reduced to
origin and path. Free-form HTML, DOM text, local storage, session storage,
IndexedDB, browser cookies, and form values are never read.

The companion repeats the same forbidden-key validation. A forbidden field is
a batch-level security failure: no event in that body is stored, and the body
is not logged. Ordinary per-event schema or classification failures are handled
individually after this whole-body security check.

## Local Access Boundary

The companion binds explicitly to `127.0.0.1:8765`. Neither host nor port is
runtime-configurable in version 1. The companion refuses startup with a
wildcard or non-loopback bind address.

The zero-pairing decision in the approved override is implemented directly.
There is no pairing endpoint, shared secret, HMAC signature, timestamp window,
or nonce cache. Non-health requests must include:

```text
X-Dota-Extension-Version
```

The companion accepts only the configured exact `chrome-extension://` Origin,
the supported extension version, JSON content, requests within the fixed body
limit, and calls within the per-origin rate limit. CORS echoes only that exact
Origin. This boundary is intentionally local-access control, not
cryptographic authentication, and does not defend against a malicious local
process that can forge the configured headers.

The localhost API is data-only. It never returns page scripts, navigation
instructions, clicks, bet parameters, or commands for the extension to execute.

## Companion API

The FastAPI companion exposes:

### `GET /health`

Unauthenticated liveness response containing only protocol version and service
state. It reveals no database paths, event counts, secrets, or match data.

### `POST /v1/events`

Accepts an origin/version-validated array of 1 to 50 schema-versioned events. The response
returns the exact numeric protocol version plus accepted, duplicate, and rejected
event IDs with stable reason codes. The extension does not acknowledge or remove
queued events when the response protocol version is missing or unsupported.
The entire body must be valid JSON and within 1 MiB. Individual invalid events
are rejected without preventing valid events in the same validated batch
from being persisted.

### `POST /v1/status`

Accepts an empty JSON object and returns origin/version-validated operational
state: protocol version, latest accepted timestamp, event type
counts, duplicate count, rejection count, known Dota match count, database
health, whether shadow strategy monitoring is active, and an optional existing
predictor report URL. A report URL must use `http` with a loopback host or it is
omitted. The endpoint does not return event payloads or account data.

All non-health endpoints enforce the exact configured Origin, the supported
extension-version header, strict JSON/body limits, and per-origin rate limits.
The extension accepts status as connected only when the response contains the
exact supported numeric `protocol_version`.

## Browser Event Persistence

Add a separate table through `LiveBettingStore.init_schema()`:

```sql
CREATE TABLE IF NOT EXISTS browser_events (
    event_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    capture_session_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    transport TEXT NOT NULL,
    event_type TEXT NOT NULL,
    raybet_match_id TEXT,
    game_id INTEGER,
    page_origin TEXT NOT NULL,
    page_path TEXT NOT NULL,
    source_path TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_bytes INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    capture_reason TEXT,
    extension_version TEXT NOT NULL,
    recognized INTEGER NOT NULL,
    processing_status TEXT NOT NULL,
    processing_reason TEXT
);
```

Create indexes on `(raybet_match_id, captured_at)` and
`(event_type, captured_at)`. `event_id` provides retry deduplication. The
sanitized `payload_json` is immutable after insert; only processing status and
reason may be updated.

`browser_events` is deliberately separate from `odds_snapshots`. A recognized
odds event is inserted into the audit table first, then passed to the existing
`live_betting.markets.snapshots_from_payload()` parser before the transaction
commits. That parser gains an optional `received_at` argument; the default
preserves direct-collector behavior, while the browser path passes
`captured_at_utc`. Companion arrival or retry time must never replace the
original browser capture time.

Add one narrow transport observation per complete odds response from either
source:

```sql
CREATE TABLE IF NOT EXISTS odds_transport_observations (
    observation_key TEXT PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN ('direct', 'browser')),
    source_event_id TEXT,
    raybet_match_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    normalized_state_hash TEXT NOT NULL,
    timing_status TEXT NOT NULL,
    processing_status TEXT NOT NULL,
    normalized_change_count INTEGER NOT NULL,
    FOREIGN KEY (source_event_id) REFERENCES browser_events(event_id)
);
```

`source` is `direct` or `browser`. A browser observation key derives from its
event ID; a direct observation key derives from source, match ID, observation
time, and payload fingerprint. `normalized_state_hash` is computed from sorted
market IDs, prices, statuses, and update fields after parsing, so independently
observed direct and browser states can be compared without hashing unrelated
metadata.

Create indexes on `(raybet_match_id, observed_at)` and
`(normalized_state_hash, observed_at)` for causal state lookup and cross-source
comparison.

Semantic `odds_snapshots` continue to store market state changes rather than
every unchanged poll. Content-change suppression compares an incoming state to
the nearest predecessor for the same match and odds ID ordered by
`(received_at DESC, id DESC)` where `received_at <= incoming.received_at`; it
must not compare against the row that happened to be inserted last. Every
current/latest market query likewise orders by `(received_at DESC, id DESC)`,
never `MAX(id)` alone.

An observation older than the newest already known observation for that match
is marked `timing_status='late'`. In version 1 it is audit-only: it writes the
browser event and transport observation but writes no `odds_snapshots`, cannot
notify or trigger the live strategy, and cannot satisfy a hypothetical fill.
This deliberately favors causal current state over online repair of historical
gaps. Offline replay may reconstruct a browser late event from its immutable
audit payload in a temporary database ordered by `captured_at`.

For non-late observations, the next-observation shadow-fill rule uses
`odds_transport_observations.observed_at`; it resolves the market state at or
before that time, so an unchanged but later poll can still provide a valid
hypothetical fill.

Match-list events are filtered to `game_id=151`, establish the Dota match-ID
allowlist, and remain audit-only. They are not passed to
`upsert_raybet_match()`, whose shape is the detailed `/odds.result`. A browser
odds event may insert sanitized team/tournament metadata only when the match is
absent. It never updates an existing match and never writes `live_url`; direct
collection remains authoritative for match updates, raw detail, and stream
URLs. Unknown, metadata-only, malformed, untrusted-match, and
unsupported-version events remain auditable but never enter normalized odds or
strategy code.

Before adding the companion, `LiveBettingStore` gains an explicit transaction
context. Store helpers do not auto-commit while that context is active; their
existing auto-commit behavior remains the default for legacy callers. Each
structurally valid browser event then runs in its own transaction: insert the
audit row, open a normalization savepoint, write the transport observation and
semantic changes, release the savepoint, update processing status, and commit.
If normalization fails, roll back the savepoint, mark the still-uncommitted
audit row `processing_status='error'`, and commit only that audit result. A
repeated event ID returns `duplicate` without normalizing again. Connections
enable SQLite foreign-key enforcement before processing.

FastAPI handles synchronous endpoints in worker threads. The companion opens
one `LiveBettingStore` per request, processes that request's batch sequentially,
and closes it before returning. It must not share one default `sqlite3`
connection across requests.

Direct collection also processes each complete `/odds` response in one
transaction context: all semantic state changes and its direct transport
observation commit together, or none do. It does not retain the old
per-snapshot auto-commit behavior inside that response transaction.

## Shadow Strategy And Execution Boundary

The browser path reuses the current predictor flow:

```text
recognized odds event
  -> normalized immutable odds snapshots
  -> causal visual-clock alignment
  -> team/player/draft profiles
  -> ComebackShadowStrategy.evaluate()
  -> strategy_decisions
  -> shadow_orders
  -> next-snapshot simulated fill
  -> settlement and report
```

Browser events do not relax any existing eligibility condition. In particular,
a fresh confirmed visual game clock and complete draft remain required. Odds
traffic alone may enrich the archive but cannot fabricate a game time or force
a decision.

Dry-run orders may contain market ID, outcome, observed price, nominal one-unit
stake, validation results, and a hypothetical fill. They remain records in the
shadow ledger and are never converted to page actions.

Existing shadow order statuses remain `pending`, `filled`, or `rejected` for
simulation and reporting. `execution_disabled` belongs only to the dry-run
execution facade and must not replace or reinterpret those shadow statuses.

No real execution adapter, RayBet order client, order endpoint, DOM click
module, or account session interface is included. Any generic execution
boundary exposed for testing has exactly one implementation,
`DisabledExecutionAdapter`, whose `execute()` method performs no I/O and returns
the stable result `execution_disabled`. There is no feature flag, environment
variable, hidden setting, or alternate adapter that can enable a real order.

## Operational States And Failure Handling

### Extension States

- `capturing`: companion is reachable and events are being acknowledged.
- `buffering`: capture continues while delivery is unavailable or backing off.
- `paused`: the user explicitly disabled capture from the popup.
- `unsupported_page`: the active page is outside the allowed RayBet Dota scope.
- `error`: manifest, storage, companion access, protocol, or validation failure
  requires attention.

State changes are local and never alter the RayBet page.

### Failure Rules

- Hook parse errors are isolated from the original page API call.
- Non-JSON, binary, unknown, or oversized payloads become metadata-only or are
  discarded according to the classification rules.
- Missing Dota identity discards the payload.
- Queue overflow drops the oldest event and records the loss counter.
- Companion outage buffers within the fixed bounds and retries with backoff.
- Origin, extension-version, or companion-protocol mismatch stops delivery and
  remains fail-closed; retry backoff prevents flooding the companion.
- A parser failure commits the event's audit row with a stable error reason. A
  database lock before the audit insert returns `database_unavailable`; no
  audit row is claimed when SQLite could not accept one.
- Service-worker suspension loses no queued event within the active browser
  session because the queue uses `chrome.storage.session`.
- Browser restart may discard undelivered events by design. A new random
  `capture_session_id` marks the boundary; coverage across that boundary is
  reported as unknown rather than reconstructed, counted exactly, or guessed.
- Unknown schemas and event types cannot influence strategy.
- A companion crash cannot affect browser page behavior.

## Testing Strategy

### Extension Unit Tests

- Fetch resolves and rejects exactly as the unwrapped function while the clone
  is observed asynchronously.
- XHR ready-state, event order, response type, and callbacks are preserved.
- WebSocket constructor behavior, constants, listeners, and `onmessage` remain
  unchanged.
- Non-string forged messages, over-cap strings, excessive depth/node/string/
  array/key counts, and token-bucket overflow are rejected before expensive
  traversal; cyclic sanitizer input terminates safely.
- Known Dota payloads are classified; non-Dota and unproven match payloads are
  discarded.
- Recursive redaction removes every forbidden key at arbitrary depth and with
  case/punctuation variants.
- Positive allowlists exclude newly added fixture fields by default.
- URL sanitization strips query strings, fragments, and signed video tokens.
- Canonical hashes are stable and retries retain event IDs.
- Payload, batch, queue, and drop-oldest limits are exact at boundaries.
- Pause, resume, backoff, and service-worker suspension preserve state rules.
- Alarm wakeup resumes a pending retry and is cleared after acknowledgement.
- The fixed `manualControlData` sampler runs only under its visibility and
  recent-Dota conditions, copies only two primitives, and remains diagnostic.

### Companion Unit And Contract Tests

- Exact Origin binding, extension-version checks, protocol-version checks,
  rate limiting, content-type validation, and body limits.
- Schema versions, enum values, timestamps, hashes, and Dota identity checks.
- Defense-in-depth forbidden-field rejection.
- Per-event accept, duplicate, and reject responses in mixed batches.
- SQLite schema creation, indexes, immutable payloads, and idempotent retry.
- The transaction context prevents helper auto-commit; an injected failure on
  a later market insert rolls back all normalized changes while committing the
  audit row as `error`.
- Recognized odds reuse `snapshots_from_payload()` and unsupported data does not.
- A newer direct observation followed by an older delayed browser event leaves
  current state unchanged, marks the browser observation late, and does not
  trigger live strategy evaluation.
- The sequence `t1=A`, unchanged `t3=A`, then delayed `t2=B` leaves current
  semantic state at `A`; `t2` exists only in audit/transport records.
- Latest-state queries use event time rather than insertion ID; an unchanged
  later observation remains eligible as the next hypothetical fill check.
- An injected failure midway through a direct complete-odds response rolls back
  both its semantic changes and transport observation.
- Browser match metadata never overwrites direct metadata, raw detail, or
  `live_url`.
- Parser failure leaves an auditable error state; database unavailability
  returns a stable error without claiming persistence succeeded.
- `DisabledExecutionAdapter` always returns `execution_disabled` and makes zero
  network, browser, filesystem, or subprocess calls.

Sanitized fixtures contain no real credentials, tokens, account values, or
personally identifying data.

### Ordinary Edge Integration Test

Load the unpacked extension in a normal Edge profile without opening DevTools.
Use a deterministic local RayBet-like test page to exercise Fetch, XHR, and
WebSocket traffic, then observe one real Dota 2 match passively when available.

Verify:

- the page produces identical visible data and interaction behavior with the
  extension enabled and disabled
- companion batches pass local access checks and deduplicate across retries
- Dota match and odds events reach `browser_events` and normalized snapshots
- non-Dota traffic and forbidden fields do not reach the companion
- pausing and companion outages do not affect the page
- popup counters agree with companion acknowledgements and known losses

No integration test logs in automatically, manipulates a bet slip, enters a
stake, or submits a wager.

### Shadow Replay Test

Replay stored sanitized browser events in capture order against a temporary
database. Restarted and uninterrupted processing must produce the same
normalized odds, strategy decisions, shadow orders, and settlements. Future
events and post-match labels must remain unavailable to earlier decisions.

## Observability

The extension records session-only counters for candidates, accepted events,
ignored non-Dota events, metadata-only events, queued events, acknowledged
events, retries, and dropped events. The companion records aggregate counts,
latency, schema rejection reasons, duplicate rates, processing errors, and last
success time.

Missing-rate calculations within an uninterrupted browser session distinguish:

- hook or parse rejection
- non-Dota filtering
- size filtering
- queue overflow
- delivery/access-or-protocol failure
- companion validation rejection
- normalized-parser rejection

Across a browser-session boundary, the report marks the interval as unknown
coverage because an orderly final flush cannot be guaranteed.

Duplicate-rate calculations distinguish extension retry duplicates from the
same market state independently observed by direct polling. These metrics are
diagnostic; they never change a strategy decision retroactively.

## Delivery Sequence

1. Add deterministic extension hook, classification, redaction, and queue unit
   tests with sanitized fixtures.
2. Implement the Manifest V3 extension and local status UI.
3. Add transaction-aware storage, event-time latest-state queries, transport
   observations for direct collection, and their regression tests.
4. Add the companion schema, loopback/origin/version guards, API tests, and
   `browser_events` persistence.
5. Dispatch recognized browser events through the existing market parser and
   verify late-delivery handling and idempotent replay.
6. Add the disabled execution boundary and its no-I/O tests.
7. Run the local integration page in ordinary Edge.
8. Passively observe one complete real Dota 2 match and produce a data-quality
   report before relying on browser events in shadow evaluation.

## Acceptance Criteria

- The extension captures relevant Dota 2 Fetch, XHR, and WebSocket responses in
  an ordinary Edge window without DevTools.
- A full observed match shows no page regression with the extension enabled.
- Only proven `game_id=151` payloads are retained; unknown payloads are metadata
  only and cannot affect strategy.
- Automated fixture scans and the real-match audit find zero forbidden or
  sensitive fields in extension storage, HTTP batches, SQLite, and logs.
- The companion listens only on `127.0.0.1`, accepts only the configured exact
  extension Origin and version within fixed rate/body limits, and never exposes
  payloads through its status API. The extension rejects missing or unsupported
  companion protocol versions.
- Duplicate delivery is idempotent from browser event through normalized odds
  and shadow decisions.
- Delayed events are ordered by capture time, cannot move current odds backward,
  and cannot trigger a live decision after a newer observation exists.
- During an uninterrupted session, queue loss, filtering, rejection, and
  duplicate rates are measured and explainable; a browser-session boundary is
  explicitly reported as unknown coverage.
- `manualControlData.data[currentIndex].time` remains diagnostic and cannot be
  used as the game clock.
- Existing visual confidence, freshness, market completeness, edge, slippage,
  and one-attempt-per-map rules remain enforced.
- Replay results are identical across process restart and uninterrupted runs.
- All order-like activity is hypothetical; the only execution result is
  `execution_disabled`.
- No login, account, balance, bet-slip, stake-entry, or real order capability
  exists in the extension or companion.
