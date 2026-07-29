# Dota 2 Paper Betting Decision Context

This context describes the shared language for the direct-only paper-betting decision lifecycle. Detailed acceptance thresholds and governance contracts live in the accepted ADRs and the betting-decision domain glossary.

## Language

**Strategy decision**:
A persisted result of evaluating one strategy opportunity. It is not an order.
_Avoid_: Signal, bet

**Qualifying strategy rejection**:
A fully evaluated, authority-complete policy rejection that can prove the decision chain reached its canonical strategy boundary.
_Avoid_: No-signal, waiting result

**Paper order**:
A simulated order created from an eligible strategy decision and never sent to a real-money betting endpoint.
_Avoid_: Real bet, settlement

**Order resolution**:
The mutually exclusive transition of a pending paper order to filled or rejected. Rejection is terminal.
_Avoid_: Settlement

**Settlement**:
An authoritative outcome and return fact associated only with a filled paper order. It is separate from order resolution.
_Avoid_: Outcome label, order status

**Outcome label**:
An authoritative post-map result attached to an eligible decision for calibration. It can exist without an order settlement.
_Avoid_: Settlement

**Calibration cohort**:
The M3-C population of eligible forward decisions with authoritative outcome labels, regardless of fill status.
_Avoid_: Filled-order cohort

**Economic cohort**:
The M3-E population of filled and formally settled forward orders used for execution and economic evaluation.
_Avoid_: Calibration cohort

**M3 readiness**:
Evidence that a cohort has sufficient mature, covered, diverse, and reproducible data for formal review. It is not strategy approval.
_Avoid_: Promotion, passed strategy

**M4 promotion decision**:
A preregistered, reproducible passed-or-failed evaluation of a ready cohort. A passed result authorizes only a new strategy proposal, not deployment.
_Avoid_: M3 readiness, automatic deployment

**Executable strategy contract**:
The versioned canonical evaluator and policy identity that normatively determine eligibility. Human-readable gate lists are summaries.
_Avoid_: Checklist policy

**Rosh score**:
A signed lineup-advantage score under one immutable Rosh parity profile. Positive means Radiant direction and negative means Dire direction. It is not a probability, edge, or stake input.
_Avoid_: Win probability, confidence

**Rosh minute score**:
The signed Rosh score for one reached minute bucket. A live decision uses only the latest available bucket at or before the authoritative game minute and never a future bucket.
_Avoid_: Forecast minute, interpolated future score

**Rosh parity profile**:
The immutable query, formula, window, fallback, rounding, serialization, and upstream-bundle identity under which Rosh parity is claimed. Any semantic or hash change creates a new profile.
_Avoid_: Latest Rosh formula, mutable configuration

**Rosh analysis attempt**:
A request attempt before a canonical draft has been fully validated. For historical_match, transport, HTTP, GraphQL, JSON, and invalid or incomplete GetMatchPicksBans draft failures remain attempts: they return only sanitized structured errors and emit operational metrics/logs, never create a RoshAnalysisRun or invent a null, empty, or partial draft or zero hash.
_Avoid_: Failed Rosh analysis run, partial run

**RoshAnalysisRun**:
An immutable succeeded or failed terminal analysis record created only after binding a valid 10-slot canonical draft and its draft_hash. historical_match crosses this boundary only after complete side, position 1..5, and globally unique hero validation; explicit_draft completes the same validation before request planning. A later classifiable failure may form a failed run only with the real draft, request/profile identities, existing sanitized manifest, and a stable error_code, and it has no result or children.
_Avoid_: Pre-draft attempt, mutable analysis

**Calibrated probability**:
A probability produced by a versioned calibration artifact evaluated on its declared prospective cohort and feature schema. A raw Rosh score or a linear transformation of it is not a calibrated probability.
_Avoid_: Rosh score, pseudo-probability

**Milestone revocation**:
An append-only governance fact that invalidates conclusions dependent on later-conflicted evidence while preserving the original records.
_Avoid_: Deletion, result rewrite
