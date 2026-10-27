# ALGO3: Institutional tender evaluation and smart order routing

A Python execution engine developed for Columbia University's high-frequency trading course using Rotman Interactive Trader. The bot evaluates institutional block offers, estimates their hedge economics across two exchanges, and manages the resulting inventory with price-aware limit orders and deadline controls.

**Current release:** `16-late-tender-aware-unwind`. The authoritative implementation is [`algo3_router.py`](algo3_router.py); the older notebook is a historical companion. Exact execution settings are documented in [`LIMIT_UNWINDING.md`](LIMIT_UNWINDING.md).

## What the strategy does

An accepted BUY tender creates a long position that must be sold. An accepted SELL tender creates a short position that must be bought back. The opportunity is the difference between the tender cash flow and the hedge cash flow, after fees and execution uncertainty.

The router follows six steps:

1. **Observe both venues.** Combine remaining bids and asks from `THOR_M` and `THOR_A`, preserving price priority and using fees to break ties. Sample books between tenders so recent liquidity history is available when an offer arrives.
2. **Estimate the full hedge.** Compare a depth-discounted sweep with a patient alternative supported by observed replenishment near current prices. Apply fees, adverse-price stress, and an uncertainty allowance.
3. **Accept eligible opportunities.** Require modeled net profit of at least $1, flat starting inventory, valid fixed-price tender terms, sufficient time, and compliance with exposure limits. The $1 floor is an admission threshold, not guaranteed profit.
4. **Revalue and work the position.** Refresh executable prices and depth before each child, update the remaining hedge value, and choose between passive limits and faster execution.
5. **Confirm each execution.** Account for partial fills, cancel unfilled quantities, and reconcile orders and positions before submitting a replacement.
6. **Recover and close out.** Resume tender evaluation after recovery confirms flat inventory and no unresolved writes. Increase execution urgency near the round's end.

## Tender economics

For a BUY tender, the model estimates:

```text
net edge = hedge sale proceeds - tender purchase cost - hedge fees - uncertainty
```

For a SELL tender:

```text
net edge = tender sale proceeds - hedge purchase cost - hedge fees - uncertainty
```

The calculation works through price levels for the tender quantity. A favorable last price or best quote alone does not establish that an entire block can be hedged profitably.

### Adaptive liquidity estimates

The router tracks persistence at the same price/venue keys, aggressive-order fill ratios, displayed additions, and recent price movement. Usable depth starts at 85% of displayed volume and adapts within bounded limits. Observations expire rather than remaining valid indefinitely.

The patient alternative requires mature, recent evidence of additions near current prices. It discounts those additions and caps future shares at 65% of the tender; enough current modeled depth must cover the remainder. It does not infer cheap future liquidity from participant count or distant-book additions. The existing sweep model also has a bounded stressed-tail allowance when usable displayed depth is incomplete.

These are adaptive heuristics. Cancellations and executions cannot be distinguished reliably from book snapshots alone, and future replenishment is not guaranteed.

## Late tenders remain eligible

The five offers belong to one 300-second round. Tender order or ID does not affect eligibility: the fifth offer can be accepted around tick 250–260. Remaining time changes execution requirements, rather than automatically disqualifying that offer.

From tick 240, admission requires the entire hedge to fit within discounted displayed depth, positive fee-inclusive profit after price stress and uncertainty, and enough time to target flat inventory by tick 285. No future replenishment is credited. The time budget counts price/venue fragments and measured active execution latency. The case, books, positions, open orders, and expiry are checked again before acceptance.

New late hedges use marketable limits immediately, without passive rests or waiting for a profit-retention target. Prices and depth are still refreshed for each child. New entries stop at tick 275, when urgent closeout begins. A smaller tender can therefore qualify later than a larger one. These checks reduce deadline risk but cannot guarantee fills or profitability.

## Price-aware unwinding

The execution engine values the remaining inventory against the current combined book, including fees. It tracks a conservative bound from confirmed limit fills and the highest observed complete-position exit estimate. If visible depth cannot cover the residual, it does not claim a complete executable P&L estimate.

The early retention target is 60% of that peak estimate, declining toward 10% over the hedge window, subject to the initial minimum target. The target becomes a fee-adjusted buy ceiling or sell floor. It is released early enough to reserve active execution time. It is not a stop-loss guarantee or a promise to preserve a fixed percentage of realized P&L.

### Passive and active orders

- **Passive limits:** seek improvement over crossing the spread, usually on the alternative venue. The bot compares the potential saving with recent adverse movement and price noise. The passive phase has share, time, attempt, and zero-fill limits.
- **Marketable limits:** execute against available liquidity with a price cap. Normal children target 5,000 shares, increasing toward 10,000 when remaining time and measured latency require it. Nearby levels can be combined within one cent on the same venue, stopping before better competing quotes.
- **Urgent market orders:** from tick 275, prioritize closing known inventory with children capped at 25,000 shares.

A book snapshot is not an atomic post-only guarantee. Prices can move before an intended passive order reaches the exchange. Configured taker fees are used conservatively in the hedge budget rather than assuming a maker rebate.

## Inventory and recovery

| Control | Implementation |
|---|---|
| Shared underlying | Validate that the two THOR position fields match; count directional inventory once |
| Exposure | 100,000-share net cap; conservative 200,000-share gross cap |
| Order size | Maximum 25,000 shares; ordinary children are smaller |
| Order direction | Reduce actual inventory; block independent speculative entries and crossing zero |
| Pending writes | Reserve a submission before sending it; never blindly replay an uncertain POST |
| Replacement | Require conclusive order and inventory reconciliation before another child |
| Terminal evidence | Retain explicit completed/cancelled records returned by the exchange |
| Manual inventory | Use a unique recent tender match where available; otherwise do not invent an entry price |
| Recovery | Resume admission only after verified flat inventory with no open orders or unresolved writes |

ALGO3 reservations live in process memory. Restarting is not a substitute for resolving an outstanding order. Concurrent manual activity can invalidate attribution and prevent reconciliation.

## Round schedule

| Tick | Behavior |
|---|---|
| Before ACTIVE | Wait automatically for the case to start |
| Before 240 | Evaluate tenders with adaptive liquidity and execution-time checks |
| 240–274 | Keep late tenders eligible with full discounted depth, stressed profit, and sufficient measured execution time; use active limits without passive waiting |
| 260 | End the passive phase for existing earlier hedges |
| 275 | Stop new tenders and use urgent market children for remaining inventory |
| 285 | Target flat inventory, leaving time before the 300-tick end |
| Case inactive | Exit and log the last active flat check |

Liquidity, API availability, and unresolved executions can prevent completion despite these targets.

## Main functions

| Function or class | Responsibility |
|---|---|
| `API` | REST requests, inventory reads, book observation, and write-boundary checks |
| `aggregate()` / `walk()` | Build combined depth and estimate execution value across levels |
| `LiquidityEstimator` | Track persistence, refill evidence, fill ratios, and price movement |
| `estimate_unwind()` | Compare sweep and supported replenishment economics |
| `should_accept()` | Apply profit, position, expiry, and time checks |
| `update_exit_value()` / `protect_exit()` | Revalue the hedge and apply a bounded price objective |
| `choose_unwind_order()` | Select passive, marketable-limit, or urgent execution |
| `execute_child()` | Submit, confirm/cancel, reconcile, and account for one child |
| `cover()` | Repeat execution until the hedge target is reached |
| `recovery_step()` / `run()` | Manage the case lifecycle, recovery, and resumption |

## Run

Requires Python 3.10+, `requests`, and an API-enabled RIT client. In PowerShell, from the project directory:

```powershell
python -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
$env:RIT_API_KEY = "YOUR_RIT_API_KEY"
& ".\.venv\Scripts\python.exe" -u ".\algo3_router.py" --live --confirmed
```

Use the existing virtual environment if already installed. Omitting both live flags performs read-only observation; it is not a fill simulation. The default endpoint is `http://localhost:9999/v1`. Launch before the round starts and run one bot process for the case.

## Diagnostics and validation

```powershell
& ".\.venv\Scripts\python.exe" -m unittest test_router test_improvements test_audit_events test_late_tenders
& ".\.venv\Scripts\python.exe" .\audit_events.py .\algo3_events.jsonl
```

The 130 offline tests cover liquidity estimates, admission constraints, passive failures, price protection, active sizing, partial fills, cancellation races, uncertain submissions, recovery, and deadlines. They validate execution behavior, not profitability.

Logs distinguish submitted limit prices from confirmed filled quantities. A submitted price is not an actual fill VWAP, and these logs alone are not a realized-P&L ledger. Historical results from previous versions do not establish v16 performance. See the repository's main README for qualified historical practice results.

The project demonstrates limit order book analysis, execution-cost estimation, smart routing, inventory control, API integration, and failure recovery in an educational simulation. Key next steps are actual-fill-price collection, empirical parameter calibration, and a durable ALGO3 order ledger.
