# Rotman RIT Trading Bot

## ALGO3: Smart Order Routing

A Python execution engine for the Rotman Interactive Trader ALGO3 simulation. It evaluates institutional tenders against liquidity across two venues, accepts eligible offers, and unwinds inventory through refreshed child orders.

**Current implementation:** v12, `12-pending-order-reservation`. Experimental simulation software; neither trading profitability nor successful close-out is guaranteed.

## Strategy

THOR_M and THOR_A represent the same underlying stock in the tested configuration. Their reported positions mirror one shared inventory. The router combines both books, chooses the best displayed execution price, and breaks price ties using venue fees.

Tender evaluation estimates the cost of unwinding the full quantity. Displayed-depth discounts respond to observed quote persistence and actual fill ratios. A limited allowance for replenishment uses recent additions at existing price levels. Fees, unseen-depth stress, execution uncertainty, exposure limits, expiry and remaining execution time all enter the acceptance decision.

The current strategy opens exposure only through accepted tenders. Independent directional trading and cross-venue arbitrage were removed after practice evidence linked additional volume to case fines. Routing a tender hedge across venues remains supported.

## Execution and reconciliation

- Refresh order books for each child order; normally cap children at 5,000 shares.
- Check fresh shared inventory before submitting an order. Block orders that increase exposure or cross zero.
- Track unresolved order submissions across recovery. Do not submit a replacement while a prior fill remains uncertain.
- Reconcile missing order details using bounded inventory changes and order-state checks.
- Exclude small cleanups from institutional hedge-speed calibration.
- Stop new tenders at tick 260; target flat inventory by tick 285 of the 300-tick round.

The bot waits for an active case when launched early, processes one round, then exits.

## Windows setup

Run RIT and Python in the same Windows environment. In PowerShell, from this folder:

```powershell
py -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
$env:RIT_API_KEY = "YOUR_RIT_API_KEY"
$env:RIT_BASE_URL = "http://localhost:9999/v1"
```

Evaluate without submitting orders:

```powershell
& ".\.venv\Scripts\python.exe" -u ".\algo3_router.py"
```

Enable simulation trading:

```powershell
& ".\.venv\Scripts\python.exe" -u ".\algo3_router.py" --live --confirmed
```

Use one bot process. Concurrent manual activity can invalidate fill attribution and trigger recovery. Do not run the notebook alongside the script. The notebook embeds the same engine for exploration.

## Tests

```powershell
& ".\.venv\Scripts\python.exe" -m unittest discover -s . -p "test_router.py" -v
```

The 66 offline tests cover tender direction, shared inventory, liquidity estimates, partial fills, deadlines, ambiguous writes, order reconciliation and a delayed-fill overshoot regression. They verify behavior with controlled fixtures, not market profitability or live API reliability.

## Case configuration

| Parameter | Tested value |
|---|---:|
| Net limit | 100,000 shares |
| Gross limit | 200,000 shares |
| Maximum order | 25,000 shares |
| Main venue taker fee | $0.01/share |
| Alternative venue taker fee | $0.005/share |

Verify constants against the running case. Some practice settings differed from the written brief.

## Known limitations

Liquidity parameters are heuristics, not fitted forecasts. Book snapshots do not establish future fill availability. The API exposes sampled book depth and sequential rather than atomic reads.

An unresolved submission can block further trading. If status never becomes conclusive, inventory may require intervention. Pending reservations are process-local: restarting is not a safe way to clear uncertainty. Recovery currently prevents new tender acceptance for the remainder of the round.

Earlier practice results mixed different code versions and manual intervention; they are not a validated backtest of this release. No performance claim is made for v12.

See [OPERATIONS.md](OPERATIONS.md) for estimator details, recovery behavior and development findings, and [CHANGELOG.md](CHANGELOG.md) for the main revisions.

The original case brief, raw trading logs and performance PDFs are not distributed here. Obtain case materials through the appropriate course or simulator provider. This project is not affiliated with the simulator provider.
