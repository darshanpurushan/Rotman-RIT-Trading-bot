# ALGO2 practice review and v2.0 changes

Reviewed inputs: `algomm1.pdf`, `algomm2.pdf`, `algo2_events.jsonl`, and `algo2_state.json`. The raw files remain private and are not committed.

## Reported results

| Metric | algomm1 | algomm2 |
|---|---:|---:|
| Net P&L | $429.28 | $383.00 |
| Submitted orders | 323 | 96 |
| Trades | 29 | 19 |
| Executed volume | 107,066 | 72,000 |
| Commissions | $1,070.66 | $720.00 |
| Rebates | $1,425.99 | $903.00 |
| Net rebates less commissions | $355.33 | $183.00 |
| Price P&L before commissions/rebates | $73.95 | $200.00 |
| Final inventory | 0 | 0 |
| Position-limit fines | $0 | $0 |
| Participants | 1 | 1 |

These are single-participant practice results, not competitive rankings. Price P&L is derived as reported net P&L + commissions - rebates. There are no reported limit fines to adjust.

Both reports specify a $0.01/share fee, $0.015/share passive rebate, five API orders per second, a 5,000-share order maximum and a 25,000-share position limit. Commission totals equal total executed volume times $0.01, not merely active volume times $0.01. Rebates equal passive volume times $0.015. A passive fill therefore receives a net $0.005/share credit; two passive legs receive $0.01/share combined before price P&L.

## Log evidence

The log contains four starts: an initial v1.0 failure and three v1.1 runs. Runs are numbered from zero.

- Run 1 submitted 323 orders, matching algomm1. It recorded 414 visibility waits, 222 failed cancellation attempts, and 328 reconciliation waits. For 316 orders, the very next log event was a visibility wait; the median gap was approximately 0.011 seconds.
- Run 2 submitted 96 orders, matching algomm2. It recorded 118 visibility waits, 76 failed cancellation attempts, and 90 reconciliation waits. For 88 orders, the next event was a visibility wait; the median gap was approximately 0.009 seconds. A read timeout was followed by cancellation recovery, a 4,000-share market BUY and `shutdown_flat`, about 115 seconds after process start. This is consistent with the report's P&L flattening midway through the case.
- Run 3 also shows the same churn: 294 submissions, 356 visibility waits, 188 failed cancellations and 270 reconciliation waits. No corresponding performance PDF was supplied, so its P&L is not inferred.
- The supplied state file is clean: expected inventory 0, no records and no unknown submission.

The order counts support the two report/log matches. The logs do not include a complete order book or fill history, so they cannot establish how much profit a different quote policy would have earned.

## Root causes and fixes

1. **Publication delay caused cancellation.** v1.1 called cancellation recovery on ordinary VisibilityPending events, sometimes milliseconds after an acknowledged submission. v2.0 reserves the known order's unfilled quantity through a 1.5-second publication grace period. During that grace, it can add the opposite side only if position reconciliation agrees. Unknown POST outcomes remain blocked.
2. **Missing one side caused both sides to be replaced.** v2.0 replaces only the side that needs a price/size/risk change, refills a missing side without cancelling the other, and removes the automatic three-second reset of otherwise competitive quotes. Ordinary repricing waits at least 0.75 seconds. Cancellation retries are spaced at least one second apart.
3. **One read timeout ended the run.** v2.0 logs the failure and obtains fresh snapshots on the next iteration without automatically entering permanent closeout. Lost POST responses still retain persistent uncertainty and prevent new submissions.
4. **Fees were documented using the brief instead of actual reports.** v2.0 defaults to the observed $0.01 fee / $0.015 passive rebate, adopts live security metadata when available, logs them at initialization, and includes net passive fees in the minimum quote half-spread.
5. **Fill diagnostics were missing.** v2.0 logs cumulative fill deltas exactly once and logs terminal order states. `limit_price` is the submitted limit, not a claim about the actual fill VWAP. Status records include working order count and net passive pair rebate.
6. **Read latency can cross deadlines.** Immediately before POST, v2.0 rechecks ACTIVE status and the 260/275/300 boundaries. It still stops new exposure at 260 and switches to market closeout at 275.

## Validation and boundaries

44 offline tests pass. New regressions exercise 300 ms publication delays, two simultaneous side reservations, queue retention, side-specific replacement, read-timeout recovery, ambiguous POST blocking, cancellation pacing, fee metadata, exact residual closeout and late-case submission checks.

These are controlled execution tests, not a historical P&L backtest. v2.0 has not been run against the user's local RIT server. No increase in profit, fill probability or competitive rank is claimed. Missing status beyond the grace period still blocks writes; unresolved records beyond eight seconds trigger closeout recovery, which can itself remain blocked until the order is conclusively reconciled.
