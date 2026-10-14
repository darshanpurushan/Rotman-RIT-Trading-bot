# Development history

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
