# Dota 2 Match Intelligence

This context joins live market observations, broadcast evidence, and verified postmatch facts without allowing later evidence to rewrite an earlier decision.

## Language

**RayBet Series**:
A head-to-head event listed by RayBet. One series may contain several maps.
_Avoid_: Match, official match

**Map**:
One independently tracked Dota 2 game within a RayBet Series. Its identity is the RayBet Series ID together with its positive map number.
_Avoid_: Series, round

**Map Identity**:
The explicit pair of RayBet Series ID and map number that identifies one Map without a derived hash or sequence guess.
_Avoid_: Hash ID, latest map, inferred next map

**Official Match ID**:
The Valve/Dota Match ID for one Map, shared by OpenDota and STRATZ.
_Avoid_: OpenDota ID, STRATZ ID, RayBet ID

**Exact Map Link**:
A verified relationship from one RayBet Series and map number to one Official Match ID.
_Avoid_: Probable match, fuzzy match

**Canonical Postmatch Facts**:
The final result and match record accepted as the authority for one Exact Map Link.
_Avoid_: Enrichment, live observation

**Postmatch Enrichment**:
Optional analytics that add detail without replacing Canonical Postmatch Facts.
_Avoid_: Result authority, fallback result

**Live Observation**:
A timestamped Vision or odds observation captured while a RayBet Series is active.
_Avoid_: Postmatch fact

**Immutable Decision**:
A P0/P1 prediction or betting decision whose inputs and outcome are fixed at creation time.
_Avoid_: Recomputed decision, corrected decision
