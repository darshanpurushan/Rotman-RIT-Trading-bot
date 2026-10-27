# Rotman RIT Trading Bots

Developed for **Columbia University's high-frequency trading course**, this project implements two Python strategies for **Rotman Interactive Trader (RIT)**: an inventory-aware market maker and a smart order router for institutional tenders.

The project connects market microstructure decisions to execution: reading limit order books, estimating liquidity and trading costs, choosing prices and quantities, reconciling partial fills, and managing inventory against a fixed deadline. Both bots use the local RIT REST API, wait automatically for a case to start, and operate through a 300-tick round.

## Cases at a glance

| | ALGO2: Market Making | ALGO3: Smart Order Routing |
|---|---|---|
| Main program | [`algo2_market_maker.py`](algo2_market_maker.py) | [`algo3_router.py`](algo3_router.py) |
| Opportunity | Capture the bid–ask spread and passive rebates | Accept favorable institutional tenders and hedge the inventory |
| Market | One stock: `ALGO` | One underlying across `THOR_M` and `THOR_A` |
| Inventory control | Quote prices and quantities respond to current exposure | Child orders reduce accepted tender inventory |
| Execution | Maintain passive buy/sell quotes, with market orders for closeout | Route child orders across venues using price, depth and fees |
| Current version | v2.0, `2.0-publication-grace` | v16, `16-late-tender-aware-unwind` |
| Offline tests | 44 | 130 across strategy, execution, and event-audit tests |
| Detailed guide | [ALGO2 guide](ALGO2_README.md) | [ALGO3 strategy](ALGO3_README.md) and [current unwind policy](LIMIT_UNWINDING.md) |

## Selected practice results

The strongest documented ranking was **highest reported P&L among 10 participants**, with **$41,199.75** in an ALGO3 practice round. Another round reached **$46,681.50**, the highest P&L among three participants, with manual tender decisions alongside bot execution.

| Case / practice report | Ranking by reported P&L | Participants | Reported net P&L | Context |
|---|---|---:|---:|---|
| ALGO3 — `algo4` | 1st / highest P&L | 10 | $41,199.75 | Zero reported fines; final inventory zero |
| ALGO3 — `algo3` | 1st / highest P&L | 8 | $18,755.02 | Zero reported fines; final inventory zero |
| ALGO3 — `algo10mixedmanual` | 1st / highest P&L | 3 | $46,681.50 | Mixed manual tender decisions and bot execution; zero reported fines; final inventory zero |
| ALGO2 — `algomm1` | Single-participant practice | 1 | $429.28 | v1.1; 29 trades; zero limit fines; final inventory zero |
| ALGO2 — `algomm2` | Single-participant practice | 1 | $383.00 | v1.1; 19 trades; zero limit fines; final inventory zero |

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

The actual practice reports specify a 5,000-share order maximum, a $0.01/share fee and a $0.015/share passive rebate. The reports charge commission on all executed shares and credit rebates on passive volume, giving a net $0.005/share credit for a passive fill. Defaults now reflect these reports, and live security metadata overrides them when available.

**Practice status:** v1.1 earned $429.28 and $383.00 in the supplied single-participant reports, both ending flat with no position-limit fines. Their logs exposed immediate cancellation after normal publication delays and an early shutdown after a read timeout. v2.0 adds a 1.5-second known-order publication grace, preserves competitive opposite quotes, paces cancellation retries and resumes after transient read errors. Unknown POST outcomes remain blocked. [Read the evidence and changes](ALGO2_PRACTICE_REVIEW.md). No live P&L improvement is claimed for v2.0 yet.

## ALGO3: Tender evaluation and smart order routing

**Objective:** earn the difference between an institutional tender price and the cost of hedging its inventory across two exchanges, after fees and execution uncertainty.

The router combines the `THOR_M` and `THOR_A` books and compares immediate execution with a patient alternative supported by recent replenishment near current prices. It accepts an eligible tender automatically, then manages the hedge one confirmed child order at a time.

| Stage | What the router does | Why it matters |
|---|---|---|
| Observe | Samples both books while idle and refreshes them for decisions | Builds liquidity history before an offer arrives |
| Estimate | Discounts unstable depth, measures nearby replenishment, and includes fees and adverse-price allowances | Evaluates the economics of the whole tender quantity |
| Admit | Requires positive modeled net profit and checks exposure, expiry, and hedge time | Avoids accepting a block solely because its price looks attractive |
| Price the exit | Revalues residual inventory at executable depth and tracks a conservative profit target | Connects each exit decision to the tender's economics |
| Execute | Weighs passive price improvement against recent movement; otherwise routes marketable limits by price and depth | Balances execution price with the cost of waiting |
| Reconcile | Confirms fills, cancellations, and inventory before replacement | Prevents duplicate or overlapping hedge orders |
| Recover and finish | Resumes tender evaluation after verified recovery; increases urgency near the deadline | Keeps a resolved error from disabling the rest of the round |

**Price-aware unwinding.** The early profit target retains 60% of the highest observed complete-position executable exit estimate, fading toward 10% over a bounded hedge window. The router reserves time for active execution and releases the target when completion requires it. This is an execution objective, not a guaranteed profit floor.

**Bounded passive limits.** Passive attempts target up to 40% of the hedge, with one-second rests, a six-second phase window, and limits on attempts and zero fills. Waiting ends sooner when price movement or remaining time makes it unattractive. Active children normally target 5,000 shares and can grow toward 10,000 under time pressure; nearby same-venue levels can be combined while respecting competing quotes.

**Inventory and deadline discipline.** The two THOR position fields represent mirrored underlying inventory. Ordinary orders must reduce exposure and cannot cross zero. From tick 240, tenders require full discounted displayed hedge depth and stressed positive profit; new tenders stop at tick 275, urgent market children begin at 275, and flat inventory is targeted by 285. Independent speculative entries are disabled. Unresolved submissions remain reserved; ALGO3 reservations are process-local.

Read the [strategy walkthrough](ALGO3_README.md) for the main functions and the [v16 execution policy](LIMIT_UNWINDING.md) for exact controls and limitations. Use [`audit_events.py`](audit_events.py) to summarize versions, decisions, bot acceptances, recoveries, and confirmed child volume from a log.

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
& ".\.venv\Scripts\python.exe" -m unittest test_router test_improvements test_audit_events test_late_tenders
```

ALGO2's 44 offline tests cover partial fills, position bounds, order visibility, lost responses, persistent reservations, quote behavior, and closeout. ALGO3's 130 offline tests cover tender admission, liquidity estimates, price protection, partial fills, uncertain writes, recovery, order sizing, deadlines, and event auditing. Tests validate controlled execution behavior; they do not establish live profitability, queue performance, or a guaranteed flat finish.

### Code quality

Optional development checks use the repository's formatter and import/lint configuration:

```powershell
& ".\.venv\Scripts\python.exe" -m pip install -r requirements-dev.txt
& ".\.venv\Scripts\python.exe" -m black --check algo2_market_maker.py algo3_router.py audit_events.py test_algo2.py test_router.py test_improvements.py test_audit_events.py
& ".\.venv\Scripts\python.exe" -m ruff check algo2_market_maker.py algo3_router.py audit_events.py test_algo2.py test_router.py test_improvements.py test_audit_events.py
```

## Practical limitations

- **Queue and price risk:** quotes may fill predominantly on one side during a trend. The price can move between a book check and order arrival, so an intended passive order can still take liquidity; the API does not provide atomic post-only protection here.
- **Execution uncertainty:** missing terminal records or inconsistent position snapshots can block further orders, including liquidation. Neither bot can guarantee a flat position or zero fines when the API is unavailable or the case ends first.
- **Recovery differs between cases:** ALGO2 persists order reservations to disk and refuses unresolved previous sessions at startup. ALGO3 reservations are process-local; restarting does not resolve an outstanding order.
- **Heuristic parameters:** quote skews, volatility windows, liquidity discounts and refill assumptions have not been calibrated on a large independent execution dataset. Participant count and changing depth can alter outcomes materially.
- **Case configuration:** verify fees, limits, symbols, rate limits and round length against the active simulation. ALGO2 defaults to the five orders per second documented in the supplied reports and slows after explicit rate-limit rejection.
- **Performance evidence:** the selected rankings are historical practice results. ALGO2's two supplied positive-P&L reports are single-participant runs on v1.1; no Sharpe ratio, expected return or latest-version improvement is inferred.

## Repository files

| File | Purpose |
|---|---|
| `algo2_market_maker.py` | Standalone ALGO2 bot |
| `ALGO2_README.md` | ALGO2 setup, strategy and recovery guide |
| `test_algo2.py` | ALGO2 offline tests |
| `requirements_algo2.txt` | ALGO2 standalone dependency list |
| `algo3_router.py` | Standalone ALGO3 bot |
| `ALGO3_README.md` | Detailed ALGO3 technical write-up |
| `ALGO3_Strategy.ipynb` | Earlier strategy notebook; the Python router is the current implementation |
| `OPERATIONS.md` | ALGO3 operational notes |
| `test_router.py`, `test_improvements.py`, `test_audit_events.py`, `test_late_tenders.py` | ALGO3 strategy, execution, and audit tests |
| `LIMIT_UNWINDING.md` | Current ALGO3 price-aware execution policy |
| `audit_events.py` | Summarize execution logs without inventing fill prices or P&L |
| `requirements.txt` | Shared Python dependencies |
| `CHANGELOG.md` | Case-specific development history |

Educational simulation software for RIT. Raw execution logs, runtime state, API credentials and copyrighted case documents are excluded from version control.



