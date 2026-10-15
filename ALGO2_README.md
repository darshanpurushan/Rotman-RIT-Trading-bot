# ALGO2 market maker v1.1

Standalone Python bot for the supplied ALGO2 Algorithmic Market Making case. This is a separate program from ALGO3. Requires Python 3.10+ and `requests`.

## Run on Windows

Save `algo2_market_maker.py` in your existing ALGO3_Practice folder (or another folder with a Python environment). Keep the ALGO3 bot stopped.

```powershell
$env:RIT_API_KEY = "YOUR_RIT_API_KEY"
& ".\.venv\Scripts\python.exe" -m pip install requests
& ".\.venv\Scripts\python.exe" -u ".\algo2_market_maker.py" --live --confirmed
```

Start before the simulation: it waits for ACTIVE and begins without a five-tick delay. One process handles one round, then exits. For observation without orders, omit `--live --confirmed`. Dry run reports intended quotes; it does not simulate fills or P&L.

## Case interpretation

The supplied ALGO2 brief specifies one security, ALGO, a 300-second round, 5,000 shares per order, a 25,000-share gross/net position limit, a $0.01/share market-order commission, a $0.005/share passive-fill rebate, and $0.10/share position-limit fines. The Python tutorial's commission examples reference variants and differ from this brief. The brief is the baseline; verify that your competition's configuration matches. The brief does not specify an additional end-inventory fine; this bot nevertheless seeks to end flat.

The parsing appendix is an Excel/VBA route to order data. This Python implementation uses the REST order fields directly. Speed-bump pacing follows the tutorial's principle of spacing request starts by elapsed transaction time, with slower pacing after a 429 response. Five writes/second is a configurable starting assumption, not an asserted ALGO2 server rate.

## Strategy

- Provide buy and sell limit quotes, normally 4,000 shares per side. Improve the best price by one cent when there is enough spread; never intentionally submit a crossing limit quote.
- Keep competitive quotes for queue priority; refresh for meaningful price changes, inventory changes, missing sides, or after three seconds. No fixed five-cent spread gate.
- Move quotes and reduce the inventory-increasing size as inventory builds. At 12,000 shares, quote only the reducing side. A separate 20,000-share operating cap leaves a 5,000-share buffer below the brief's limit.
- Short-window midpoint volatility widens quotes; rapid price movement makes the vulnerable side less aggressive. These are heuristics, not a calibrated prediction model. The bot does not take independent directional market-order entries.
- Count outstanding BUY and SELL quantities separately. Do not assume they will offset each other. Partial fills reserve only the unfilled quantity.
- At tick 260, only reduce inventory. At tick 275, cancel passive orders and close inventory through sequential market orders of at most the configured size. Continue until flat or the market stops.

A pair filled passively earns its price difference plus a combined $0.01/share rebate under the brief. A passive entry followed by a market exit has a net $0.005/share fee before price movement. Market movement and emergency exits can outweigh spread and rebate income. Fee/rebate constants are descriptive case settings; quote selection uses price spread and inventory, not a purported profit guarantee.

## Execution and recovery

Every submitted order is recorded in `algo2_state.json`; events go to `algo2_events.jsonl`. Use one bot process, one state file, and no concurrent manual orders on this account. It requires no open ALGO orders at startup. Existing inventory becomes the starting reconciliation baseline.

Cancellation must be followed by terminal order status and matching position before replacement. Cumulative fills update expected inventory exactly once. Individual-order 404 responses fall back to OPEN, TRANSACTED and CANCELLED order lists. Temporary missing records, position publication lag and cancellation confirmation delays block submissions while snapshots are retried; known orders are cancelled to reduce exposure. Normal quoting can continue once reconciled. Missing records for more than eight seconds or an external order enters closeout recovery. Absence alone never proves a fill or cancellation.

An ambiguous POST response is deliberately different: further submissions remain blocked, including after restart. Known orders can still be cancelled, but the unknown order must be checked in RIT. This may require manual closeout. Do not delete the state file merely to bypass the block. Once the case has stopped, all orders are confirmed terminal, and the account position is independently confirmed flat, archive the old state file before starting a fresh session. A clean normal run leaves a reusable empty state.

Ctrl+C requests cancel-and-flatten while the case is active. It is not an instant exit. If the server is unreachable, stopped, or gives inconclusive execution status, the program cannot guarantee liquidation. Check RIT directly in that situation. No orders are sent once the observed case tick reaches 300.

REST has no atomic snapshot or guaranteed post-only behavior here. A price may move between the last book check and arrival, so an intended passive quote can still take liquidity. Live fees and rate limits should be checked in practice. State persistence reduces duplicate-order risk but is not a substitute for broker-side idempotency.

## Files and validation

- `algo2_market_maker.py`: directly runnable bot; requests is the only external dependency.
- `test_algo2.py`: offline fake-API tests of order and inventory behavior.
- `requirements_algo2.txt`: dependency.

```powershell
& ".\.venv\Scripts\python.exe" -m unittest discover -p "test_algo2.py"
```

Offline tests cover position bounds, partial fills, stale position data, lost POST responses, restart blocking, passive prices, end-stage reduction, and dry-run behavior. They do not establish live profitability, queue behavior, throughput, or zero fines. This version has not been connected to your local RIT server. Start with a practice round and retain the JSONL log and performance report for tuning.
