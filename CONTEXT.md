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

**Milestone revocation**:
An append-only governance fact that invalidates conclusions dependent on later-conflicted evidence while preserving the original records.
_Avoid_: Deletion, result rewrite
