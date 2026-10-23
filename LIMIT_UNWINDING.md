# ALGO3 limit-order unwinding (v14)

The router already used marketable limit orders: a sell at the displayed bid or a buy at the displayed ask. Those orders cap execution price, but generally cross the spread. Version 14 adds a bounded passive phase to seek better execution prices before crossing.

## Execution policy

- Early in a hedge, quote on THOR_A at the consolidated same-side best price, improving by one cent when the spread is at least three cents. The snapshot quote does not cross the consolidated opposite price. There is no atomic post-only guarantee if prices move before arrival.
- Passive children are capped at 10,000 shares and the remaining position. At most 40% of the initial hedge is targeted for passive execution.
- Each passive attempt rests for one second. The passive phase ends after four attempts, two zero-fill attempts, six seconds since the hedge schedule began, an adverse midpoint move of two cents, or insufficient time for closeout. These are initial policy settings, not calibrated optimal values.
- Reconcile fills and confirm cancellation before replacing an order. Recovery retains the passive attempt and elapsed-time budget for the same inventory episode.
- Then use displayed liquidity across both venues with marketable limit children of at most 5,000 shares. The existing tender-price protection can still select a bounded waiting price until its soft deadline.
- At tick 260, stop the new passive phase. At tick 275, use market children of at most 25,000 shares for urgent closeout. Tick 285 remains the flat target before the 300-tick round ends. Completion depends on API availability and executable liquidity.

The existing profit-protection function now preserves a selected passive order's rest time; previously it could reset that time to 0.1 seconds. Profit accounting conservatively uses configured taker fees rather than assuming passive rebates. No speculative entry strategy is added.

## Diagnostics and verification

`unwind_order` records the selected execution style, price, quantity, wait, reason, passive attempts, and passive filled shares. `child` records actual confirmed fills and the requested rest time. These fields distinguish unfilled passive attempts from executions.

The 92 offline tests include non-crossing quotes, quantity caps, adverse moves, time limits, preserved resting time, recovery budgets, and unfilled passive orders followed by full long/short closeout. They test execution behavior, not profitability. Passive orders may not fill; the policy cannot guarantee better P&L.

Replace `algo3_router.py` and use the existing environment and command:

```powershell
& ".\.venv\Scripts\python.exe" -u ".\algo3_router.py" --live --confirmed
```

The startup event reports `14-passive-limit-unwind`. Keep `RIT_API_KEY` in the environment.
