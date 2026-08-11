## No Artificial Workflow or Excessive Task Decomposition

This is a solo-maintained project.

Do not create or simulate:

- Owners, Reviewers, Verifiers, Approvers, or Delivery Managers;
- role-based handoffs or staged ownership transfers;
- SHA freeze tasks or baseline-freezing phases;
- approval gates, sign-offs, or independent verification phases;
- separate process tasks that produce no code, tests, or required documentation.

Testing, linting, type checking, regression checks, and secret scans are technical verification steps, not organizational approval stages.

For normal implementation work, use one continuous workflow:

1. inspect the relevant code;
2. implement the requested change;
3. update directly affected tests or required documentation;
4. run focused verification;
5. report the result and continue.

Do not decompose a normal request into more than 3–5 concrete execution steps.

Each step must directly contribute code, tests, required documentation, or meaningful verification. Do not create separate steps for:

- assigning ownership;
- freezing baselines;
- transferring work between simulated roles;
- approving completed work;
- re-running checks already covered by final verification;
- producing process artifacts.

Merge implementation, directly related documentation, and focused verification whenever practical.

Do not wait for approval after presenting a plan. Continue immediately unless there is an actual technical blocker, high-risk ambiguity, or an operation that explicitly requires confirmation.

## Codex Participation in Match Analysis and Decisions

The user explicitly authorizes Codex to participate as a persistent analysis collaborator in this project's match workflow. In future sessions, Codex should treat live-match review and shadow decision support as part of its normal project responsibilities; the user should not need to repeat this authorization.

Codex may:

- inspect structured match state, odds evidence, R.O.S.H. outputs, Vision observations, and selected source frames;
- review low-confidence or conflicting observations and explain what is and is not supported by the evidence;
- combine verified pre-match and in-game inputs into a traceable shadow recommendation;
- assist with post-match review, calibration, and evaluation of whether its participation improved predictions.

Keep these authority boundaries:

- The deterministic local system remains authoritative for ingestion, match and Map identity, odds freshness, stored frames, OCR observations, draft validity, results, and settlement.
- Model-derived content is inference, not raw evidence. Record its source, confidence, input version, supporting observations, conflicts, missing fields, and timestamp. Never overwrite or silently upgrade source evidence with a model conclusion.
- Model unavailability, timeout, malformed output, stale inputs, unresolved conflicts, an untrusted feed, or an unverified ten-hero lineup must not stop collection and must produce `skip` rather than a fabricated decision.
- Codex must not place real bets or trigger irreversible external actions unless the user gives a separate explicit instruction. The initial role is second-opinion and shadow decision support, and it must not change the current fixed 8% policy on its own.

Treat every Map as an independent unit:

- isolate its evidence, model context, R.O.S.H. analysis, Vision state, recommendations, checkpoints, result, and settlement from every other Map;
- use plain identifiers such as `raybet_match_id`, `map_number`, and `decision_sequence`; do not derive match, Map, or decision identity from hashes;
- Series-level context may inform analysis but must never overwrite Map-level facts or leak one Map's observations into another Map's decision record.

For each decision checkpoint, produce or persist a structured result containing at least:

- `team_a`, `team_b`, or `skip`;
- estimated win probability, confidence, and the relevant market-implied probability;
- the R.O.S.H., Vision, odds, and intelligence inputs actually used;
- supporting and opposing reasons, evidence conflicts, missing inputs, and the exact skip reason;
- model and prompt/input versions, decision time, and the eventual result for offline evaluation.

Increase model influence only from measured offline evidence such as calibration, Brier score, log loss, and shadow return, and only after an explicit user instruction. Do not treat persuasive explanations as proof of predictive value.

The initial live shadow model is frozen for the three-Series acceptance run beginning with RayBet Series `38423648`: keep `vision-gold-lead-logit-v1`, its validated 5–60 minute coefficients, the 8 percentage-point edge threshold, and the assumed `1 unit` stake unchanged until three consecutive Series are accepted. Checkpoints outside the validated minute range must record `skip`; do not extrapolate or retune during the run.

## Required Live Match Decision Pipeline

The project must maintain a working end-to-end live pipeline for every eligible Series and each independent Map:

`real-time event discovery -> match metadata -> pre-match/live/closing odds -> live stream -> draft and Vision observations -> verified ten-hero lineup -> R.O.S.H. analysis -> decision checkpoints -> result and settlement -> offline review`

This pipeline is a continuing product requirement, not a one-off demonstration:

- Discover upcoming and live events without depending on a manually supplied match ID. Preserve source, source event ID, scheduled time, discovery time, status changes, and explicit gap reasons.
- Receive pre-match, in-play, and closing odds with market identity, source, source timestamp when available, local observation time, freshness, and continuity. Never synthesize an unavailable price or silently substitute another market.
- Detect BP and match start, acquire the live stream, and retain the planned per-Map frames and observations. Record stream and observation failures explicitly without allowing one Map's state to contaminate another.
- Parse teams, players, heroes, sides, and other required draft facts with source and confidence. A formal lineup is valid only after all ten heroes and both sides are credibly established; conflicting or incomplete lineups must not enter formal R.O.S.H. analysis.
- Run R.O.S.H. automatically after the valid ten-hero lineup is established. Persist the exact lineup, analysis inputs and version, interpretable component scores, overall result, and failure reason.
- Produce a traceable shadow betting decision at meaningful checkpoints: before the Map when valid pre-match inputs exist, immediately after a trustworthy draft and R.O.S.H. result, and during play when fresh odds plus trustworthy Vision evidence materially change the estimate. Repeated checkpoints with no material input change need not create duplicate decisions.
- A decision may recommend `team_a`, `team_b`, or `skip`. Missing, stale, contradictory, replay-derived, untrusted, or causally late evidence must be reflected in the decision or force the relevant checkpoint to `skip`.
- Continue collection even when analysis or model participation fails. Expose discovery gaps, odds delay or outage, stream state, draft validity, R.O.S.H. state, latest decision state, and exact skip/failure reasons through project observability.

Every capability above requires focused end-to-end verification and acceptance on real matches. A healthy service or isolated unit test is not sufficient evidence that the live decision pipeline works.
