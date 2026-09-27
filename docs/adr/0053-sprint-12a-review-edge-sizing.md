# ADR 0053 — Sprint 12 A review: sizing on the edge per unit of risk, and the approved open points

- **Status:** accepted
- **Date:** 2026-09-27
- **Decided by:** project owner, at the Sprint 12 A review (C-22); amends ADR 0052
- **Tasks:** RISK-002, RISK-005, SIGNAL-004 (sizing inputs); PAPER-001 (a requirement recorded
  for later)

## Context

ADR 0052 left open points for the owner (C-22). The main one: the risk profile scaled a position
by the raw calibrated win probability (zero size at p = 0.5, full at 0.6). That fits a 1:1 payoff
only. With 2:1 barriers break-even is p = 1/3, so a good 2:1 trade at p = 0.45 got no size, and a
poor one could get a large size if its p happened to be high.

## Decision

1. **Edge-per-unit-risk scaling replaces raw-probability scaling.** For an intent carrying a
   calibrated win probability p with standard error `p_se`:

       p_lcb = max(0, p - lcb_z x p_se)                   (the lower confidence bound, as in SIGNAL-002)
       ev_r  = p_lcb x TP/SL - (1 - p_lcb) - round_trip_cost/SL
       size multiplier = clip(ev_r / ev_r_full, 0, 1)

   TP and SL are the distances from the entry reference to the target and to the accepted
   (possibly widened) stop. `ev_r` is the expected profit per unit of the amount at risk, so a
   trade at break-even gets zero size, whatever its payoff ratio. `ev_r_full` and `lcb_z` live in
   the risk profile and are provisional: `ev_r_full = 0.25` (full size once the edge net of costs
   reaches a quarter of the risk) and `lcb_z = 1.645` (one-sided 95 %). The multiplier only ever
   shrinks the size; every limit still applies after it.
2. **Who computes what.** The signal engine passes the forecast's calibrated p and its standard
   error in the intent (`p_win`, `p_se`), not a bound of its own. The risk engine takes its own
   lower bound with the profile's `lcb_z` and prices the round trip with its own cost model — the
   same one the fills use: spread + 2 × slippage + 2 × commission (`CostModel.round_trip_cost_bps`,
   now shared with the signal engine's EV). A strategy therefore cannot enlarge its size by
   understating its uncertainty or its costs.
3. **An intent with a probability needs its standard error and a target.** Without `p_se` there is
   no lower bound, and without a target no payoff ratio. Such an intent is refused, like an
   uncalibrated one. An intent without a probability (the rule and exposure strategies) is not
   scaled.
4. **The profile version becomes `risk-2`.** Its meaning changed: the fields `probability_zero` and
   `probability_full` are replaced by `ev_r_full` and `lcb_z`.
5. **ADR 0052's other open points are approved as they stand.** The provisional profile values;
   a refused reversal still closes the opposite position; backtests may run without a kill
   switch.
6. **PAPER-001 requirement.** The paper and live runtimes refuse to start without a kill-switch
   source (a file, an environment variable or the database flag). Recorded in STATUS for when
   PAPER-001 is built; the backtester keeps running without one (ADR 0052, RISK-006).

## Consequences

- The forecast-to-fill test of SIGNAL-005 now runs with the default profile. It no longer needs a
  profile matched to its 2:1 barriers, because 2:1 trades at p ≈ 0.45 get a positive size.
- Decisions record `p_lcb`, `ev_r`, `edge_scale`, the round-trip cost and the target distance in
  their limits snapshot.
- The provisional `ev_r_full` is a risk-appetite setting, not a fitted parameter. It is reviewed
  with paper trading, like the rest of the profile.
