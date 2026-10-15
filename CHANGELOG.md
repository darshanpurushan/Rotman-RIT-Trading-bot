# Development history

## ALGO2 v2.0 — Practice-report execution fixes
Reviewed algomm1/algomm2 and matching event-log runs. Adds a 1.5-second known-ID publication grace with full remaining-quantity reservations, side-specific repricing, paced cancellation, continued operation after transient read timeouts, actual report fee/rebate defaults with live metadata overrides, fill diagnostics and immediate pre-POST deadline checks. 44 offline tests. Historical v1.1 reports: $429.28 and $383.00, one participant each, flat with zero limit fines. No v2.0 P&L improvement is claimed.

## Code cleanup and execution overhead
- Format both bots and their tests consistently; remove unused imports and expand compressed control flow.
- Keep ALGO2 quote sizes within the configured order size, including sizes below 500 shares.
- Ignore fully filled book rows when sampling midpoint history, and update that history during read-only observation.
- Skip unchanged ALGO2 state-file replacements while retaining flush/fsync for changed state and retrying failed writes.
- Serialize ALGO3 log events once for both outputs, flush console output, and consolidate the order-submission guard.
- 30 ALGO2 tests and 67 ALGO3 tests pass offline. These changes have not been benchmarked for live P&L or latency.

## ALGO2 v1.1 — Market making and order visibility
Adds a separate inventory-aware ALGO market maker with passive quotes, outstanding-order exposure checks, a persistent submission reservation, automatic case-start monitoring and staged closeout. Individual-order 404 responses fall back to status-filtered order lists. Includes 22 offline tests. Positive practice P&L is user-reported; no ALGO2 report has been archived for numerical comparison.

## ALGO3 history

## v12 — Pending order reservations
Retains unresolved submissions across recovery to prevent additional orders against inventory that has not yet reflected a delayed fill. Adds regression coverage for a 25,000-share delayed fill and a potential 10,300-share overshoot. 66 offline tests.

## v11 — Timing and reconciliation
Excludes sub-10,000-share cleanups from institutional timing calibration. Adds bounded retries when a known child reappears in the open-order list.

## v10 — Adaptive liquidity
Replaces fixed depth discounts with quote-persistence and fill-ratio feedback, capped replenishment credit and explicit uncertainty charges. Accepts positive estimated net profit above a $1 numerical floor.

## v9 — Tender-only execution
Removes independent entries and adds an inventory-reducing order guard after evidence of speculative-volume case fines.

## v7–v8 — Retired experiments
Explored cross-venue entries and short-term directional signals. These strategies are not part of the current engine and should not be used as evidence of permitted activity in ALGO3.

## Earlier revisions
Added shared-inventory accounting, short-tender hedging, end-of-round liquidation, partial-fill reconciliation, missing-order handling, price-level aggregation and automatic case-start monitoring.



