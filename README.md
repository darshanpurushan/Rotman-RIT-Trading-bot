# Rotman RIT Trading Bots

Python trading bots for two **Rotman Interactive Trader (RIT)** cases: market making in **ALGO2** and institutional tender execution in **ALGO3**. Both use the local RIT REST API, wait automatically for a case to start, and manage orders and inventory through a 300-tick round.

## Cases at a glance

| | ALGO2: Market Making | ALGO3: Smart Order Routing |
|---|---|---|
| Main program | [`algo2_market_maker.py`](algo2_market_maker.py) | [`algo3_router.py`](algo3_router.py) |
| Opportunity | Capture the bid–ask spread and passive rebates | Accept favorable institutional tenders and hedge the inventory |
| Market | One stock: `ALGO` | One underlying across `THOR_M` and `THOR_A` |
| Inventory control | Quote prices and quantities respond to current exposure | Child orders reduce accepted tender inventory |
| Execution | Maintain passive buy/sell quotes, with market orders for closeout | Route child orders across venues using price, depth and fees |
| Current version | v1.1, `1.1-order-visibility` | v12, pending-order reservations |
| Offline tests | 30 | 67 |
| Detailed guide | [ALGO2 guide](ALGO2_README.md) | [ALGO3 strategy](ALGO3_README.md) and [operations](OPERATIONS.md) |

## Selected practice results

The strongest documented ranking was **highest reported P&L among 10 participants**, with **$41,199.75** in an ALGO3 practice round. Another round reached **$46,681.50**, the highest P&L among three participants, with manual tender decisions alongside bot execution.

| Case / practice report | Ranking by reported P&L | Participants | Reported net P&L | Context |
|---|---|---:|---:|---|
| ALGO3 — `algo4` | 1st / highest P&L | 10 | $41,199.75 | Zero reported fines; final inventory zero |
| ALGO3 — `algo3` | 1st / highest P&L | 8 | $18,755.02 | Zero reported fines; final inventory zero |
| ALGO3 — `algo10mixedmanual` | 1st / highest P&L | 3 | $46,681.50 | Mixed manual tender decisions and bot execution; zero reported fines; final inventory zero |
| ALGO2 — current practice feedback | Not yet documented | — | Positive P&L reported by the user; amount not supplied | v1.1 reported to be working well; performance report pending |

Rankings are inferred from the trader P&L matching the report's simulation maximum and its participant count; the summary does not establish whether another participant tied. These are selected historical practice outcomes, not competition wins or results from a controlled benchmark. They span earlier ALGO3 versions and different liquidity conditions, so they do not establish the performance of the latest code. Some other rounds had missed opportunities, execution failures or fines. Raw reports and case PDFs are excluded from this repository.

## ALGO2: Market making

The market maker aims to earn the spread as opposing orders fill over time. It continuously reassesses the best available prices, current inventory and outstanding orders rather than waiting indefinitely for an original pair to complete.

- **Competitive quotes:** normally quote up to 4,000 shares on each side; improve a displayed price by one cent when the spread permits. Check the opposite quote immediately before submission to avoid intentionally crossing it.
- **Inventory-aware prices and sizes:** move quotes and reduce the inventory-increasing side as exposure builds. At 12,000 shares, quote only the reducing side. The operating cap is 20,000 shares, below the case's 25,000-share limit.
- **Market adaptation:** recent midpoint volatility widens quotes; short-term movement reduces aggressiveness on the vulnerable side. These are heuristic controls, not a trained forecasting model.
- **Order accounting:** count remaining buys and sells separately when calculating exposure; do not assume that opposite orders will fill together. Reconcile cumulative partial fills before replacement.
- **Order visibility:** when `/orders/{id}` returns 404, search open, transacted and cancelled order lists. If execution remains uncertain, retain the reservation and block additional orders.
- **Persistent state:** write submission uncertainty and known orders to `algo2_state.json` before sending orders. An unknown submission outcome remains blocked after restart until independently reconciled.
- **Closeout:** stop increasing inventory at tick 260; start sequential market-order liquidation at tick 275 and continue while the case is active.

The supplied brief specifies a 5,000-share order maximum, a $0.01/share market-order fee, and a $0.005/share passive-fill rebate. Two passive legs therefore add a combined $0.01/share rebate to the captured price difference. Adverse price movement can more than offset the spread and rebates.

**Practice status:** the user reports that ALGO2 v1.1 works well with positive P&L. No numerical P&L, participant count or ranking is claimed until an ALGO2 performance report is available.

## ALGO3: Tender evaluation and smart order routing

The tender engine evaluates institutional block trades using order books from both THOR venues. It estimates hedge proceeds or costs, fees, liquidity uncertainty and execution time before accepting an offer.

- Discount displayed liquidity according to recent persistence and aggressive-order fill ratios.
- Credit a capped portion of observed replenishment; stress prices for a limited unseen tail.
- Require positive estimated net profit above a $1 numerical floor and pass inventory, timing and expiry checks.
- Hedge through smaller child orders, prioritize favorable prices and use venue fees to break price ties.
- Treat `THOR_M` and `THOR_A` position fields as mirrored underlying inventory; do not sum them as independent exposures.
- Reserve unresolved submissions to prevent replacement trades against fills that have not yet appeared in inventory.
- Stop accepting tenders at tick 260, increase closeout urgency at tick 275 and target flat inventory by tick 285.

Independent directional and cross-venue entry experiments were removed from ALGO3 following case-fine evidence. They are not part of the current strategy. See the [detailed strategy and limitations](ALGO3_README.md) for the estimation model and reconciliation rules.

## Setup and execution

Requires Python 3.10+ and a running RIT client with REST API access enabled. Open PowerShell in the repository folder:

```powershell
python -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
$env:RIT_API_KEY = "YOUR_RIT_API_KEY"
```

If the Windows launcher selects Spyder's runtime, use your installed standard Python version explicitly when creating the environment, for example `py -3.14 -m venv .venv`.

Run the bot for the active case:

```powershell
# ALGO2 — Market Making
& ".\.venv\Scripts\python.exe" -u ".\algo2_market_maker.py" --live --confirmed

# ALGO3 — Smart Order Routing
& ".\.venv\Scripts\python.exe" -u ".\algo3_router.py" --live --confirmed
```

Run one bot process for the relevant case. Do not trade manually alongside it: external orders and inventory changes can invalidate reconciliation. Launch before the round starts; both bots wait for ACTIVE, operate for one round and exit when it stops. Omitting `--live --confirmed` enables read-only observation, not a fill simulation or a backtest. The default API endpoint is `http://localhost:9999/v1`. API keys stay in environment variables.

For ALGO2, Ctrl+C requests cancellation and flattening while the market is active. Preserve its state file during upgrades. Do not delete unresolved state merely to bypass a block; consult the [recovery instructions](ALGO2_README.md).

## Validation

```powershell
& ".\.venv\Scripts\python.exe" -m unittest test_algo2
& ".\.venv\Scripts\python.exe" -m unittest test_router
```

ALGO2's 30 offline tests cover partial fills, position bounds, stale positions, order-status 404 fallbacks, lost responses, restart blocking, passive quote prices, configured quote-size bounds, midpoint sampling, state-write recovery and closeout sizing. ALGO3's 67 tests cover tender admission, liquidity heuristics, timing, cancellation and pending-order protection. Tests use controlled API behavior and do not establish live profitability or queue performance. The cleanup adds no new performance claim; quote sizes below 500 shares and dry-run midpoint sampling now follow the configured behavior, and unchanged ALGO2 state no longer triggers repeated disk writes.

### Code quality

Optional development checks use the repository's formatter and import/lint configuration:

```powershell
& ".\.venv\Scripts\python.exe" -m pip install -r requirements-dev.txt
& ".\.venv\Scripts\python.exe" -m black --check algo2_market_maker.py algo3_router.py test_algo2.py test_router.py
& ".\.venv\Scripts\python.exe" -m ruff check algo2_market_maker.py algo3_router.py test_algo2.py test_router.py
```

## Practical limitations

- **Queue and price risk:** quotes may fill predominantly on one side during a trend. The price can move between a book check and order arrival, so an intended passive order can still take liquidity; the API does not provide atomic post-only protection here.
- **Execution uncertainty:** missing terminal records or inconsistent position snapshots can block further orders, including liquidation. Neither bot can guarantee a flat position or zero fines when the API is unavailable or the case ends first.
- **Recovery differs between cases:** ALGO2 persists order reservations to disk and refuses unresolved previous sessions at startup. ALGO3 v12 reservations are process-local; restarting does not resolve an outstanding order.
- **Heuristic parameters:** quote skews, volatility windows, liquidity discounts and refill assumptions have not been calibrated on a large independent execution dataset. Participant count and changing depth can alter outcomes materially.
- **Case configuration:** verify fees, limits, symbols, rate limits and round length against the active simulation. ALGO2 defaults to five write requests per second and slows after explicit rate-limit rejection; the configured rate is not a claim about the server's maximum.
- **Performance evidence:** the selected rankings are historical practice results. ALGO2's positive P&L is user-reported without a report; no Sharpe ratio, expected return or latest-version improvement is inferred.

## Repository files

| File | Purpose |
|---|---|
| `algo2_market_maker.py` | Standalone ALGO2 bot |
| `ALGO2_README.md` | ALGO2 setup, strategy and recovery guide |
| `test_algo2.py` | ALGO2 offline tests |
| `requirements_algo2.txt` | ALGO2 standalone dependency list |
| `algo3_router.py` | Standalone ALGO3 bot |
| `ALGO3_README.md` | Detailed ALGO3 technical write-up |
| `ALGO3_Strategy.ipynb` | ALGO3 notebook |
| `OPERATIONS.md` | ALGO3 operational notes |
| `test_router.py` | ALGO3 offline tests |
| `requirements.txt` | Shared Python dependencies |
| `CHANGELOG.md` | Case-specific development history |

Educational simulation software for RIT. Raw execution logs, runtime state, API credentials and copyrighted case documents are excluded from version control.

