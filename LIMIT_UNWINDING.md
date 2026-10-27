# ALGO3 price-aware unwinding (v16)

The router evaluates fixed-price tenders and hedges the resulting shared THOR inventory across THOR_M and THOR_A. A marketable limit order caps the execution price but can still cross the spread. Passive limits seek better prices while accepting fill risk.

## Tender evaluation

The bot observes both books every 0.5 seconds while idle, and refreshes them for tender decisions and child orders. Quantity already filled is removed from displayed orders. Price priority across venues comes first; fees break ties.

Two estimates are compared:

1. An immediate book sweep with a depth haircut, fees, uncertainty, and the existing bounded unseen-tail allowance.
2. A patient alternative that uses current depth near the touch and a conservative fraction of recently observed replenishment at nearby price/venue levels. This alternative needs mature, recent observations, at least 25% immediate modeled coverage, and sufficient measured replenishment. Future shares are capped at 65% of the tender. In practice that cap requires at least 35% current modeled coverage. Distant-book additions and participant counts do not support this alternative. Future prices include adverse-movement stress.

The better supported estimate is used, with both estimates and the chosen model logged. Estimated profit must remain positive, positions must be flat before a new tender, and exposure and execution-time checks must pass. Future liquidity remains uncertain. More participants do not automatically imply better liquidity.

## Late tenders remain eligible

The five offers belong to one 300-second round. Tender order or ID does not affect eligibility: the fifth offer can be accepted around tick 250–260. Remaining time changes execution requirements, rather than automatically disqualifying that offer.

From tick 240, admission requires the entire hedge to fit within discounted displayed depth, positive fee-inclusive profit after price stress and uncertainty, and enough time to target flat inventory by tick 285. No future replenishment is credited. The time budget counts price/venue fragments and measured active execution latency. The case, books, positions, open orders, and expiry are checked again before acceptance.

New late hedges use marketable limits immediately, without passive rests or waiting for a profit-retention target. Prices and depth are still refreshed for each child. New entries stop at tick 275, when urgent closeout begins. A smaller tender can therefore qualify later than a larger one. These checks reduce deadline risk but cannot guarantee fills or profitability.

## Exit pricing and pacing

For an attributable tender, every child decision revalues the remaining inventory using executable depth on both venues and configured fees. If the displayed book cannot cover the residual, no complete-position executable P&L estimate is claimed. Previously confirmed limit fills contribute a conservative profit bound; submitted limit prices are not labeled as actual execution prices.

The early target retains 60% of the highest observed complete-position exit estimate. That retention fades toward 10% over the hedge window, subject to the existing minimum target. The router reserves time for active execution before releasing the target. This is a bounded price objective, not a guaranteed P&L floor: prices and liquidity can change, and completion takes priority as the deadline approaches.

Passive execution is limited to 40% of the initial hedge, with 10,000-share children, one-second rests, at most four attempts, at most two zero-fill attempts, and a six-second phase window. The router also stops passive waiting after adverse movement, when recent price movement makes the potential price improvement unattractive, or when a favorable move offers an opportunity to lock an exit. These are initial policy settings, not empirically optimal parameters. There is no atomic post-only guarantee.

Active children normally target 5,000 shares, rising toward 10,000 when the remaining time and measured active execution latency require it. Nearby levels on the same venue can be combined within one cent; the router stops before a better competing venue quote. It can still send a tiny order when honoring the best displayed price requires one.

The passive phase stops at tick 260; eligible late tenders remain available until tick 275. Urgent market children begin at tick 275, capped at 25,000 shares. The flat target is tick 285 before the 300-tick round ends. API outages or insufficient liquidity can still prevent completion.

## Reconciliation and recovery

Only one child may be outstanding. Explicit terminal order records from submission, cancellation, or status reads are retained. This avoids unnecessary detail reads after completion is already confirmed. A missing detail record alone is never proof that an order completed.

Unknown writes remain reserved until reconciled. Partial fills and cancellations must be confirmed before replacement. After recovery reaches verified flat inventory with no orders or uncertain writes, tender evaluation resumes before the entry cutoff. Manual inventory uses a unique recent matching tender price when available; otherwise the router does not invent an entry price or claim an attributable profit floor. Concurrent manual trades can invalidate attribution and block reconciliation.

Passive wait duration does not train the active-child latency estimate. Historical total hedge duration still contributes to tender time budgeting.

## Diagnostics

- `decision`: estimated edge, sweep/patient alternatives, liquidity assumptions, time budget, and top book levels.
- `tender_accepted`: bot source, ID, tick, side, quantity, and tender price.
- `exit_valuation`: remaining shares, displayed coverage, projected exit value, peak estimate, and active profit target.
- `unwind_order`: limit price, quantity, execution style, wait, and selection reason.
- `child`: requested and confirmed filled quantity; its price is the submitted limit, not a fill VWAP.
- `recovery_rearmed`: verified resumption of tender evaluation.

`audit_events.py` summarizes versions, bot acceptances, decisions, recoveries, and child execution statistics. It deliberately does not reconstruct realized P&L from submitted limit prices or label unlogged acceptances as definitely manual.

## Running and validation

Replace `algo3_router.py` and use the existing virtual environment and API-key environment variable:

```powershell
& ".\.venv\Scripts\python.exe" -u ".\algo3_router.py" --live --confirmed
```

Startup reports `15-adaptive-price-aware-unwind`. No new runtime dependency is required beyond `requests`.

```powershell
& ".\.venv\Scripts\python.exe" -m unittest discover -p "test_*.py"
& ".\.venv\Scripts\python.exe" .\audit_events.py .\algo3_events.jsonl
```

The ALGO3 suite contains 130 offline tests covering admission, exposure limits, partial fills, cancellation races, uncertain writes, recovery, liquidity history, price protection, passive failure, active sizing, and closeout. These tests validate behavior, not profitability. Historical event logs without full book and actual-fill records cannot establish how this version would have performed in earlier rounds.
