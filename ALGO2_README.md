# ALGO2 market maker v2.0

Standalone Python 3.10+ bot for RIT ALGO2. Install `requests`; run the program from your extracted ALGO2 folder with the existing virtual environment:

```powershell
$env:RIT_API_KEY = "YOUR_RIT_API_KEY"
& ".\.venv\Scripts\python.exe" -u ".\algo2_market_maker.py" --live --confirmed
```

The command and filename are unchanged. Keep your existing state file. The startup version is `2.0-publication-grace`. Launch before the simulation: it waits for ACTIVE, trades one round and exits when the case stops. Omit `--live --confirmed` for read-only observation, which updates quote history but does not simulate fills or P&L.

## Strategy

Quote both sides of ALGO, normally up to 4,000 shares each. Improve the displayed price by one cent when the spread permits; check the opposite touch before sending to avoid intentionally crossing it. Adapt quote prices and sizes to midpoint movement, volatility and inventory. Stop adding to a position at 12,000 shares and operate below a 20,000-share cap, leaving a buffer beneath the case's 25,000-share limit.

Count unfilled buys and sells separately. Quotes stay in place while competitive; changing one side does not automatically cancel the other. Ordinary repricing requires approximately two cents of price movement and a minimum quote lifetime of 0.75 seconds. Risk or size changes can initiate cancellation sooner. There is no fixed three-second reset of competitive quotes.

At tick 260, allow only inventory reduction. At tick 275, cancel passive orders and flatten through sequential market orders, each at most 4,000 shares or the configured size. Recheck the case immediately before submission. No orders are sent after an observed tick of 300. Ctrl+C requests cancellation and flattening while the market remains active.

## Actual case economics

The supplied practice reports specify:

| Setting | Observed value |
|---|---:|
| Order maximum | 5,000 shares |
| Position limit | 25,000 shares |
| API order rate | 5 orders/second |
| Fee | $0.01/share |
| Passive rebate | $0.015/share |
| Round | 300 ticks |

Reported commission totals apply the fee to all executed volume; passive fills additionally receive the rebate. A passive fill nets a $0.005/share credit. Two passive legs net $0.01/share in rebate less commission before the captured price difference. These actual reports take precedence over the older tutorial/brief examples. Live `fee` and `limit_order_rebate` security fields override the defaults when available. Quote half-spread includes a net passive-fee floor.

Actual practice net P&L was $429.28 and $383.00, both ending flat with zero position-limit fines and one participant. These are historical v1.1 results, not proof of v2.0 performance. See [the report/log review](ALGO2_PRACTICE_REVIEW.md).

## Publication delay and recovery

An acknowledged order with a known ID can temporarily be absent from both detail and status-list endpoints. For 1.5 seconds after acknowledgement, retain its full unfilled reservation and poll without reflexively cancelling it. If the observed position agrees with expected fills, the opposite quote can be submitted; another quote on the same side cannot. If the position disagrees, writes remain blocked. Beyond that grace, a missing record blocks writes until it reappears; after eight seconds of persistent missing visibility, enter closeout recovery.

Cancel only the order requiring replacement, pace cancellation retries at least one second apart, and require terminal status plus matching position before replacement. Absence alone never proves cancellation or execution.

A transient read timeout no longer permanently ends normal market making. The next iteration fetches fresh case, order and position snapshots. In contrast, an ambiguous POST outcome remains blocked, including after restart: do not retry it blindly or delete its state to bypass the block.

State is saved to `algo2_state.json` before POST, using flush/fsync and atomic replacement. Unchanged snapshots skip redundant writes. A previous unresolved session requires independent reconciliation in RIT. The supplied state file from the reviewed runs is clean; it need not be deleted. Run only one process and avoid concurrent manual trades on ALGO.

## Diagnostics and validation

`algo2_events.jsonl` now includes fill deltas, terminal statuses, working-order counts and API waits. The `limit_price` on a fill event is the submitted price, not an actual execution-price estimate. Logs alone are not a realized-P&L ledger.

```powershell
& ".\.venv\Scripts\python.exe" -m unittest test_algo2
```

44 offline tests cover partial fills, publication delays, reservations, side-specific replacement, transient read errors, lost POST responses, cancellation pacing, fees, state persistence and closeout. These tests do not establish live profitability or guarantee zero fines. The API does not offer atomic post-only checks here, and the price can move before order arrival. If execution status cannot be resolved or RIT becomes unavailable, inventory may require manual intervention.
