# ALGO3 v12: unresolved order reservation

Replace algo3_router.py in the existing Windows folder. Keep your .venv and
RIT_API_KEY. Run only one bot, with no concurrent manual trades.

    & ".\.venv\Scripts\python.exe" -u ".\algo3_router.py" --live --confirmed

Without --live the script only evaluates. Launch before the round; it waits for
ACTIVE, runs one round and exits. Startup: 12-pending-order-reservation.

## Why estimates changed

The previous estimator discarded 40% of every displayed level, always required
usable depth for 60% of the tender, and stressed unseen shares by five cents.
It could force a walk into worse prices even when displayed liquidity persisted.
A prior log also showed estimated profit $395.27 rejected against a $1,460 hurdle.
Version 10 removes the per-share profit hurdle. Require at least $1 estimated
profit AFTER fees, unseen-tail stress and an execution uncertainty allowance.
There is no $0.15 tender-gap cutoff or minimum percentage of profit retained.

## Adaptive liquidity model

Observe books when evaluating tenders and while unwinding. Aggregate unfilled
quantity at each price and venue. Keep eight seconds of history; reset after
three-second gaps. Mature history requires four snapshots spanning three seconds,
with the latest no more than two seconds old. No trader-count assumption is used.

Initial usable depth is 85%. Mature usable fraction is 50% + 45% times observed
quantity persistence, capped at 95%. Persistence counts the overlap of quantity
at the same price and venue in consecutive snapshots. Observed aggressive-order
fill ratios also cap the usable fraction; passive fills are excluded. Fill-ratio
updates use 70% previous / 30% new. This can tighten or relax over a round.

Count additions only at price/venue keys common to consecutive snapshots. Use
25% of their observed rate, projected for at most eight seconds, as replenishment
credit. Credit is capped at 30% of tender size. Initial unseen-tail allowance is
35% of the tender; refill credit may extend this to at most 65%. With no visible
hedge liquidity, always reject. Additions are displayed observations, not proof
of executable future liquidity: snapshots cannot distinguish cancellations from
executions or identify all order replacement. New price levels receive no refill
credit. Stale or insufficient history gets no replenishment credit.

Price every unseen share at the worst sampled execution price with an adverse
allowance: initially two cents, later at least one cent or half the observed
best-price range, plus a penalty for disappointing fill ratios. Fees are included.
Charge an additional uncertainty allowance on the entire tender: at least 0.25
cents/share or 10% of the tail allowance, whichever is greater. These are explicit,
uncalibrated heuristics, not statistically validated confidence bounds. Acceptance
can still lose money. More aggressive estimation increases that possibility.

Execution timing now uses the worst rate among the last three completed hedges,
rather than the worst-ever round observation, together with current child/API
latency. A single slow early hedge can age out. Existing deadline margins remain.
Each decision logs liquidity_estimate: depth fraction, persistence, fill ratio,
visible/unseen shares, refill credit, uncertainty charge and estimated net profit.
There are no full historical books in old logs, so counterfactual profit or actual
improvement cannot be established from those logs alone.

## Fees, fines and execution

Actual reports show main taker fee $0.01/share and alternative $0.005/share, with
alternative maker rebate $0.0075/share. This overrides the brief's inconsistent
no-commissions statement. Acceptance estimates assume taker fees and no rebates.
Price impact is a separate execution cost. Constants must match the live case.

The case-11 report's $3,017.60 fines on 10,044 shares exactly match
5,000*$0.20 + 5,044*$0.40. This strongly indicates speculative-volume penalties.
Versions 7/8 independent entries remain REMOVED. Every live POST /orders checks
fresh mirrored inventory: no entry from flat, no increased exposure, no crossing
zero, no invalid size and no second order while a THOR order is open. Accepting
SELL tenders may create shorts; the following BUY orders reduce them.

THOR_M and THOR_A mirror one inventory. Both raw fields must agree. Net exposure
is counted once; gross conservatively counts both fields. Net cap 100,000, gross
200,000, max order 25,000. Normal children 5,000, refreshed quotes each child.
Missing order records require bounded inventory reconciliation; never blindly
retry a write. Unexpected inventory/orders or unresolved errors enter permanent
recovery for the round. Manual intervention can therefore stop later tenders.

No new tenders at tick 260. Close-out begins at 260, urgent market children at
275, flat target 285. Confirm both fields zero and no orders. API failures or
absent liquidity can prevent completion; no zero-fines guarantee. Short passive
tail execution remains available early in a normal hedge, but never in recovery.

## Validation

54 offline tests pass. New tests cover stable versus disappearing depth,
replenishment versus moved price levels, stale history, actual partial-fill
feedback, accepting small positive estimates, and rejecting empty books despite
refill credit. The existing tender, mirror-accounting, recovery, deadline and
speculative-order guard tests remain. Synthetic tests establish behavior, not
profitability. Run a practice round and provide its event log and performance
report to assess estimates against outcomes. No independent trading is added.


## Version 11: findings from algo3_events(1).jsonl

The upload contains 22 process runs and ends with three version-8 runs; it does
not validate versions 9/10. No directional_entry events appear. The run matching
report 11 has 12 cross-venue completions totaling 10,044 entry shares, matching
the fined volume. It accepted tender 215 at tick 95, then entered recovery after
an order visibility/reconciliation error; later automatic acceptance stopped.
Independent entries remain removed. Manual inventory also activates recovery.

A 300-share cleanup took 1.327 seconds. Version 8 extrapolated this small-order
seconds/share rate to a 71,000-share tender, giving a 505.73-second budget and
rejecting it at ticks 97 and 103. Another 600-share cleanup inflated later tender
budgets to roughly 135-152 seconds. Version 11 excludes hedges below 10,000 shares
from institutional timing calibration. Their summaries remain logged with
used_for_tender_timing=false. Recent large hedges and current child/API latency
still determine time admission; no fixed assumption that every hedge takes 10s.

Missing-detail reconciliation now tolerates a known child ID reappearing in the
open-order list: up to three cancellation/stable-position checks, without another
POST. An unrelated ID, invalid fill delta or unresolved known order still blocks
and triggers recovery. The old log does not include IDs for its generic visibility
errors, so this cannot be claimed to fix every historical recovery event.

59 offline tests pass. Added regressions replay the 300-share timing failure,
exclude tiny first hedges, age out old slow samples, resolve a known reappearing
child and continue blocking a child that never clears. Existing concurrent-order
and exposure guards remain. A new practice log is needed for live verification.


## Version 12: late-fill overshoot correction

The new log algo3_events(2).jsonl contains six v11 runs. In the run matching
report 15, inventory was -39,700 before order 825, a BUY of 25,000. Its detail
record was unavailable and inferred fill was zero. Recovery submitted order 831,
another BUY of 25,000. Expected inventory was -14,700 but observed inventory was
+10,300: exactly one extra 25,000-share fill. The bot then sold 10,300. This
strongly indicates the unresolved order filled late; the log cannot exclude
concurrent external trades. The old recovery loop lost the outstanding quantity.

Every order POST now reserves its entire requested quantity and pre-order
inventory before submission. A lost response retains the reservation. Before
another POST or recovery hedge, resolve the pending order. A terminal order
record must agree with stable inventory and no open THOR orders. If the record
is missing, only observing the full requested fill, with stable inventory and
no open orders, releases it. Partial or zero inferred fills do not release it.
A known OPEN order is cancelled and its terminal state awaited. A response
without an order ID blocks further submissions for inspection. No blind replay.

This is deliberately stricter than merely reading current positions: a delayed
fill can arrive after those reads. If RIT never supplies conclusive status, the
bot may remain blocked with inventory and require intervention; zero fines or
end-of-case closure cannot be guaranteed. Do not restart to bypass the block:
the reservation is process-local, not a persistent exchange transaction ledger.
Use one process without concurrent manual orders; fill inference depends on it.

66 offline tests pass, including the delayed 25,000-share fill, blocked oversized
replacement, retained reservation for unchanged/partial inventory, terminal
cancellation, mismatching terminal inventory, blocked recovery hedges, and lost
submission IDs. Live behavior still needs a practice run. The adaptive valuation
model and deadlines remain unchanged; no independent trading is introduced.

The log also shows a 78,000-share manual tender closed in 10.014 seconds after
a cold-start depth rejection (27,370 usable shares). This supports concerns about
liquidity estimation but does not establish realized profit from child limit
prices. Manual inventory triggered round-long recovery, preventing subsequent
tender evaluation. These separate issues should not be conflated.
