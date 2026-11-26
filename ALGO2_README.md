# ALGO2 market maker v3.8

Standalone Python 3.10+ bot for RIT ALGO2. Install `requests`; run the program from your extracted ALGO2 folder with the existing virtual environment:

```powershell
$env:RIT_API_KEY = "YOUR_RIT_API_KEY"
& ".\.venv\Scripts\python.exe" -u ".\algo2_market_maker.py" --live --confirmed
```

The command and filename are unchanged. Keep your existing state file. The startup version is `3.8-primary-refill`. Launch before the simulation: it waits for ACTIVE, trades, then waits for the next round. A backwards tick reset starts a new round only when the previous state is resolved and fresh account checks confirm no position or open ALGO orders. A pause without a tick reset resumes the same round. Add `--once` to exit after one case. Omit `--live --confirmed` for read-only observation, which updates quote history but does not simulate fills or P&L.

Add `--preflight` to check the RIT connection and saved state without placing orders. See the [changelog](ALGO2_CHANGELOG.md) for version history.

## Execution walkthrough

The market maker seeks spread income and passive-fill rebates while controlling the inventory left by uneven fills. It continuously observes the external book, computes inventory-adjusted bid and ask prices, and reconciles each order before replacement.

| Stage | What the bot does |
|---|---|
| Observe | Exclude its own orders from the reference book and track midpoint changes |
| Price | Adjust quote width for short-term variation and shift prices with inventory and trend |
| Size | Reduce the side that increases exposure; stop increasing inventory at the soft limit |
| Maintain | Preserve competitive quotes and pace cancellations and replacements |
| Reconcile | Track partial fills and reserve uncertain submissions until resolved |
| Finish | Taper late inventory and quote size; reduce only by 290 and flatten by 294, earlier if execution needs more time |

Under the configured case economics, a passive fill pays a $0.01 fee and earns a $0.015 rebate, a net $0.005 per share. Two passive legs can add $0.01 per share to the captured spread before adverse movement and any liquidation costs. Both legs filling is not guaranteed.

### Code map

| Function | Responsibility |
|---|---|
| `quotes` | Inventory, trend, and volatility adjustments to prices and sizes |
| `capacity` | Worst-case exposure if all outstanding buys or sells fill |
| `Bot.step` | Quote maintenance and end-of-round execution |
| `Bot.submit` | Fresh state checks and reservation before submission |
| `Bot.reconcile` / `Bot.order_status` | Partial fills, publication delays, and uncertain execution |
| `Bot.save` | Atomic persistence for restart protection |
| `API.call` | Request handling and write-rate pacing |

## Strategy

Quote both sides of ALGO, normally up to 4,000 shares each. Improve the displayed price by one cent when the spread permits; check the opposite touch before sending to avoid intentionally crossing it. Adapt quote prices and sizes to midpoint movement, volatility and inventory. Stop adding to a position at 12,000 shares and operate below a 20,000-share cap, leaving a buffer beneath the case's 25,000-share limit.

Count unfilled buys and sells separately. Quotes stay in place while competitive; changing one side does not automatically cancel the other. Ordinary two-cent repricing waits at least 0.75 seconds. One-cent adverse price changes also qualify after that minimum lifetime. A quote one cent behind the external touch can be improved after 1.5 seconds if the target price supports it. Small desired-size changes do not automatically discard queue priority; material size excesses and risk changes still trigger cancellation. There is no fixed three-second reset of competitive quotes.

From tick 240, taper quote size and the soft inventory target. Continue two-sided quoting after 280 when exposure and time permit. At tick 290, allow only inventory reduction; at 294, cancel passive orders and flatten through sequential market orders, each at most 4,000 shares or the configured size. These are latest deadlines: a conservative child-execution budget based on inventory, unfilled buys/sells, and measured API latency can move both earlier. Candidate orders must also fit the remaining time budget. The final buffer is a risk policy, not a claim that the case imposes a flat-inventory fine. Override the latest deadlines using `--stop-entry` and `--flatten`, with 0 < stop-entry < flatten < 300. Recheck the case immediately before submission. No orders are sent after an observed tick of 300. Ctrl+C requests cancellation and flattening while the market remains active.

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

## Publication delay and recovery

An acknowledged order with a known ID can temporarily be absent from both detail and status-list endpoints. For 1.5 seconds after acknowledgement, retain its full unfilled reservation and poll without reflexively cancelling it. If the observed position agrees with expected fills, the opposite quote can be submitted; another quote on the same side cannot. If the position disagrees, writes remain blocked. Beyond that grace, a missing record blocks writes until it reappears; after eight seconds of persistent missing visibility, enter closeout recovery.

Cancel only the order requiring replacement, pace cancellation retries at least one second apart, and require terminal status plus matching position before replacement. Absence alone never proves cancellation or execution.

A transient read timeout does not permanently end normal market making. The next iteration fetches fresh case, order and position snapshots. In contrast, an ambiguous POST outcome remains blocked, including after restart: do not retry it blindly or delete its state to bypass the block.

State is saved to `algo2_state.json` before POST, using flush/fsync and atomic replacement. Unchanged snapshots skip redundant writes. A previous unresolved session requires independent reconciliation in RIT. Run only one process and avoid concurrent manual trades on ALGO.

## Continuous operation and competition settings

Keep the terminal running. No new command is needed for each clean round; `waiting_for_case`, `round_reset`, and `initialized` describe transitions. Ctrl+C requests cancellation and flattening while ACTIVE, then exits. If the case is already inactive, the bot exits with an explicit state summary; it cannot trade in a stopped market.

A recoverable processing error enters closeout, then logs `recovery_flat` and resumes after a three-second cooldown once no inventory, orders or uncertain writes remain. `shutdown_flat` now identifies a user-requested stop and includes its reason and tick. Transient publication lag waits without entering permanent shutdown. An unresolved reset stops without cancelling old order IDs in a new round. Unknown submissions never auto-clear across resets or restarts.

More participants are not assumed to mean free liquidity. Quoting responds to the observed external book and inventory. Preserve queue priority while competitive, correct stale prices, and stay within the case's five-write-per-second maximum. The API rate adapts downward on throttling. A known OPEN detail record remains reserved even if the open-order list temporarily omits it.

## Optional quoting modes

The standard command keeps one quote per side. While one side is waiting for cancellation confirmation, v3.8 can refill the opposite side if fresh order and position checks confirm that it is safe. A cancellation pending on the same side still blocks replacement there.

- `--adaptive-queue` can improve one quote when a narrow spread has a deep queue and fills have slowed. If a small residual position remains, this applies to its reducing side.
- `--aged-exit` can improve the reducing quote after a ten-second fill gap when inventory is between 500 shares (exclusive) and the configured order size (inclusive).
- `--two-levels` can split each side's existing size between two price levels before taper. Test it separately from the two flags above.

All three flags are off by default.

## Diagnostics and validation

`algo2_events.jsonl` now includes fill deltas, terminal statuses, working-order counts and API waits. The `limit_price` on a fill event is the submitted price, not an actual execution-price estimate. Logs alone are not a realized-P&L ledger.

```powershell
& ".\.venv\Scripts\python.exe" -m unittest test_algo2 test_algo2_continuous
```

92 offline tests cover partial fills, publication delays, reservations, side-specific replacement, transient read errors, lost POST responses, cancellation pacing, fees, state persistence, late quoting, latency-sensitive closeout, pause/resume, clean round resets, and unresolved-reset blocking. These tests do not establish live profitability or guarantee zero fines. The API does not offer atomic post-only checks here, and the price can move before order arrival. If execution status cannot be resolved or RIT becomes unavailable, inventory may require manual intervention.


