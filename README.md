# Rotman Interactive Trader (RIT) | Python Algorithmic Trading Strategy


## Overview

This project implements an automated institutional tender evaluation and smart order routing strategy for the **Rotman Interactive Trader (RIT) ALGO3 Smart Order Routing case**.

The algorithm evaluates institutional block-trade opportunities using real-time limit order book data from two exchanges. It estimates execution costs, available liquidity, exchange fees, and execution time to determine whether accepting a tender is economically justified.

The strategy combines:

- **Institutional tender evaluation** based on estimated net execution profitability.
- **Adaptive liquidity estimation** using order book persistence, replenishment, and observed fill ratios.
- **Smart order routing** across two exchanges with different transaction fees.
- **Order execution management** using smaller child orders and partial-fill monitoring.
- **Inventory risk management** with position limits and deadline-aware liquidation.
- **Order reconciliation** to prevent duplicate execution when API responses are incomplete or delayed.

The implementation extends a course-provided Python skeleton and communicates with RIT through its REST API.

**Current status:** Version 12 has passed 66 offline tests. Earlier versions were exercised in RIT practice sessions, but the latest execution-reconciliation changes have not yet been independently validated in a complete practice round.

---

## 1. Trading Environment

The ALGO3 case simulates institutional equity execution across two trading venues.

| Parameter | Configuration |
|---|---|
| Main exchange | `THOR_M` |
| Alternative exchange | `THOR_A` |
| Main exchange taker fee | $0.010/share |
| Alternative exchange taker fee | $0.005/share |
| Maximum order size | 25,000 shares |
| Standard child order size | 5,000 shares |
| Net position limit | 100,000 shares |
| Gross position limit | 200,000 shares |
| Simulation duration | 300 ticks |
| REST API endpoint | `http://localhost:9999/v1` |

Fee assumptions and position limits reflect the observed case configuration and should be verified against the active simulation.

### Trading Objective

The strategy seeks to capture profitable institutional tender opportunities while minimizing execution costs and residual inventory exposure.

For example, suppose an institution offers to buy 50,000 shares at $50.20.

If the algorithm can acquire those shares across the two exchanges at an average execution cost below $50.20, the difference represents potential trading profit.

However, the quoted spread alone does not establish profitability. The strategy must account for:

1. Available liquidity across both exchanges.
2. Exchange-specific transaction fees.
3. Market impact and adverse price movements.
4. Partial fills and execution uncertainty.
5. Time required to complete the hedge.
6. Position constraints and remaining simulation time.

A tender is accepted only when its estimated net profitability exceeds the required threshold and the remaining admission checks pass.

---

## 2. Strategy Architecture

The system consists of four primary components.

### 2.1 Institutional Tender Evaluation

For each available tender, the algorithm evaluates whether the resulting inventory can be hedged profitably.

The decision process:

1. Retrieve the tender price, quantity, and direction.
2. Obtain current order book information from both exchanges.
3. Aggregate available liquidity across price levels.
4. Estimate the cost of acquiring or liquidating the tender quantity.
5. Incorporate transaction fees and adverse execution allowances.
6. Check position constraints and estimated execution time.
7. Accept the tender only when the estimated net profit satisfies the minimum threshold.

The estimated net profit can be expressed conceptually as:

$$
\widehat{\Pi}_{net}
=
\Pi_{tender}
-
\widehat{C}_{execution}
-
\widehat{C}_{fees}
-
\widehat{C}_{uncertainty}
$$

where:

- $\Pi_{tender}$ represents the cash-flow benefit of the tender relative to hedge execution.
- $\widehat{C}_{execution}$ represents estimated hedge execution costs.
- $\widehat{C}_{fees}$ represents exchange transaction fees.
- $\widehat{C}_{uncertainty}$ represents additional allowances for adverse execution outcomes.

The current strategy requires a minimum estimated net profit of **$1**.

This is an execution-admission threshold rather than a guarantee of realized profit.

### 2.2 Adaptive Liquidity Estimation

Displayed order book quantities do not necessarily represent immediately executable liquidity.

Orders may be canceled, replaced, or consumed before the strategy reaches them. Conversely, liquidity may replenish during the execution process.

The algorithm therefore incorporates historical order book observations into its execution estimates.

#### Order Book Persistence

The algorithm maintains a short history of displayed liquidity and compares quantities at identical price levels and venues across consecutive observations.

Liquidity that remains available across multiple snapshots is treated as more reliable than liquidity that disappears rapidly.

The initial usable-depth assumption is 85% of displayed quantity.

With sufficient recent observations, the estimated usable fraction adapts based on observed persistence and historical aggressive-order fill ratios, subject to a maximum of 95%.

#### Liquidity Replenishment

The strategy measures increases in displayed quantity at previously observed price levels.

A discounted portion of observed replenishment is incorporated into the execution estimate.

Replenishment credit is capped to prevent the model from assuming unlimited future liquidity.

#### Unobserved Execution Costs

When visible order book liquidity is insufficient to cover the full tender, the algorithm may estimate the execution cost of a limited remaining quantity beyond currently usable depth.

These shares are assigned adverse execution prices, with an additional uncertainty allowance.

The strategy rejects tenders when no visible hedge liquidity is available.

**Model qualification:** These liquidity adjustments are adaptive heuristics, not statistically calibrated forecasts of executable volume.

### 2.3 Smart Order Routing

After accepting a tender, the algorithm attempts to hedge the resulting inventory across the two exchanges.

The routing logic compares executable prices and transaction fees.

For example:

| Exchange | Ask Price | Taker Fee | Effective Purchase Cost |
|---|---:|---:|---:|
| THOR_M | $50.10 | $0.010 | $50.110 |
| THOR_A | $50.10 | $0.005 | $50.105 |

At identical quoted prices, the alternative exchange provides a lower effective acquisition cost.

The strategy prioritizes favorable execution prices and uses exchange fees to break price ties.

Instead of relying on a single large execution, the algorithm divides hedges into smaller child orders.

The execution engine:

- Refreshes order book information during execution.
- Selects trading venues based on available prices and liquidity.
- Uses marketable limit orders when appropriate to control execution prices.
- Tracks partial fills and outstanding quantities.
- Cancels and reconciles outstanding orders before continuing.
- Increases liquidation urgency as the simulation deadline approaches.

The objective is to minimize execution costs while managing the risk of holding unhedged inventory.

### 2.4 Inventory and Risk Management

The algorithm includes controls intended to limit speculative exposure and prevent unintended execution.

#### Position Controls

The two THOR instruments represent mirrored inventory within the case configuration.

The algorithm checks both reported positions and accounts for the underlying exposure when evaluating order quantities.

Configured constraints include:

- Maximum net exposure: 100,000 shares.
- Maximum gross exposure: 200,000 shares.
- Maximum individual order: 25,000 shares.
- No independent speculative entries.
- No hedge orders that increase absolute inventory exposure or cross through zero.

#### Deadline Management

The simulation runs for 300 ticks.

| Simulation Tick | Strategy Behavior |
|---|---|
| Before 260 | Evaluate tenders and execute eligible hedges |
| 260 | Stop accepting new tenders and begin liquidation |
| 275 | Switch toward more urgent liquidation orders |
| 285 | Target complete inventory liquidation |
| 300 | Simulation ends |

The early liquidation target reserves time for execution delays, order cancellations, and inventory reconciliation.

These controls reduce exposure to end-of-case penalties but cannot guarantee complete liquidation.

---

## 3. Order Reconciliation and Failure Recovery

A significant challenge in API-driven execution is determining whether an order has executed when its status is unavailable.

For example, an order submission may succeed while the API response is lost.

Submitting the same order again could unintentionally double the execution quantity.

Version 12 introduces a **pending-order reservation mechanism** to address this problem.

Before an order submission, the algorithm records:

- Requested order quantity.
- Order direction.
- Inventory observed before submission.
- Order identifier, when available.

If the order outcome becomes uncertain, its requested quantity remains reserved.

Additional orders are blocked until the strategy establishes a consistent execution state.

The reservation is released only when:

1. A terminal order record agrees with stable inventory and no outstanding THOR orders; or
2. If the order record is unavailable, the entire requested fill is established through inventory reconciliation, with no outstanding THOR orders.

Partial or unchanged inventory observations alone are insufficient to release an unresolved reservation.

The strategy does not blindly resubmit uncertain orders.

If the execution state cannot be resolved, it prioritizes preventing duplicate trades over continued execution.

**Important:** Pending-order reservations are maintained in process memory. They are not persisted to a durable transaction ledger, so restarting the program does not safely resolve an outstanding order.

---

## 4. Implementation

**Programming Language:** Python

**Trading Platform:** Rotman Interactive Trader

**API:** RIT REST API

**Primary Dependency:** Requests

**Execution Environment:** Windows with a local RIT client

### Repository Structure

```text
.
├── algo3_router.py
├── ALGO3_Strategy.ipynb
├── test_router.py
├── requirements.txt
├── PREPARATION.md
└── README.md
```

| File | Description |
|---|---|
| `algo3_router.py` | Main strategy and execution engine |
| `ALGO3_Strategy.ipynb` | Notebook representation of the strategy |
| `test_router.py` | Offline tests of strategy behavior |
| `requirements.txt` | Python dependencies |
| `PREPARATION.md` | Configuration details and development findings |
| `README.md` | Project overview and technical documentation |

---

## 5. Installation and Execution

The strategy requires a running RIT client with its REST API enabled.

### Step 1: Create a Python Environment

Open Windows PowerShell in the project directory.

```powershell
python -m venv .venv
```

Activate the environment:

```powershell
.\.venv\Scripts\Activate.ps1
```

Install the dependencies:

```powershell
python -m pip install -r requirements.txt
```

### Step 2: Configure the RIT API

Retrieve the API key from the RIT client and configure it in PowerShell:

```powershell
$env:RIT_API_KEY = "YOUR_API_KEY"
```

The default API endpoint is:

```text
http://localhost:9999/v1
```

### Step 3: Run Offline Tests

```powershell
python -m unittest test_router
```

### Step 4: Run in Dry-Run Mode

```powershell
python algo3_router.py
```

Dry-run mode retrieves market data and evaluates opportunities without submitting trading actions.

It does not simulate actual order fills or inventory changes.

### Step 5: Enable Live Simulation Execution

```powershell
python -u algo3_router.py --live --confirmed
```

Both flags are required to enable order submissions.

The bot can be launched before the simulation begins. It waits for the RIT case to become active, operates for one round, and exits when the case becomes inactive.

**Run only one bot instance and avoid concurrent manual trades on the same instruments.**

---

## 6. Testing and Validation

The current version has passed **66 offline tests** covering tender evaluation, execution constraints, and recovery behavior.

| Component | Tested Behavior |
|---|---|
| Tender evaluation | Profitability and admission checks |
| Liquidity estimation | Stable and disappearing displayed depth |
| Liquidity replenishment | Persistent versus changing price levels |
| Partial fills | Fill-ratio feedback and reconciliation |
| Position limits | Exposure checks and speculative-order prevention |
| Execution timing | Exclusion of misleading small-order timing samples |
| Order cancellation | Terminal-state verification |
| Duplicate-order protection | Pending-order reservations and delayed fills |
| Recovery | Blocking replacement orders when status remains uncertain |

These tests validate behavior under controlled scenarios rather than establishing trading profitability.

### Findings from Earlier RIT Practice Sessions

Analysis of historical execution logs identified three important problems.

#### A. Overly Conservative Execution-Time Estimates

An earlier version extrapolated execution speed from very small orders.

In one practice run, a 300-share cleanup required approximately 1.33 seconds.

Extrapolating this rate to a 71,000-share tender produced an estimated execution duration exceeding 500 seconds, resulting in tender rejection.

The revised model excludes hedges smaller than 10,000 shares from institutional execution-time calibration and uses recent large-hedge observations.

#### B. Liquidity Estimation and Missed Opportunities

Earlier versions occasionally rejected tenders because their liquidity assumptions and profit thresholds were excessively restrictive.

The revised model incorporates observed order book persistence, replenishment, and fill ratios.

This improves the flexibility of the estimation framework but introduces additional model risk.

#### C. Delayed Fills and Duplicate Execution

A prior execution log showed a sequence consistent with a delayed 25,000-share order fill followed by an additional replacement order.

The resulting inventory exceeded the intended hedge target by 25,000 shares.

This motivated the pending-order reservation mechanism introduced in version 12.

The historical evidence strongly suggests a delayed-fill problem, although concurrent external trades cannot be conclusively excluded.

**Validation boundary:** These findings explain the development of subsequent safeguards. They do not establish that version 12 has eliminated every observed failure.

---

## 7. Limitations

The strategy has several limitations affecting execution quality, reliability, and potential profitability.

### 7.1 Displayed Liquidity May Not Be Executable

The execution model relies on displayed order book quantities and recent liquidity observations.

However, resting orders can disappear before the strategy reaches them.

Furthermore, repeated snapshots cannot reliably distinguish completed trades from cancellations or order replacements.

**Consequence:** The algorithm may overestimate available liquidity and accept a tender whose actual hedge execution cost exceeds its initial estimate.

### 7.2 Liquidity Parameters Are Not Statistically Calibrated

The strategy uses manually specified parameters for liquidity haircuts, replenishment credit, adverse price allowances, and execution uncertainty.

These parameters respond to recent market observations but have not been calibrated using a sufficiently large independent dataset of realized executions.

**Consequence:** Estimated execution costs may be systematically too optimistic or conservative, particularly during periods of rapidly changing market depth.

### 7.3 No Directional Price Prediction

The strategy evaluates institutional tenders rather than forecasting future asset prices.

It does not incorporate momentum signals, fundamental factors, news, or machine learning-based price predictions.

Independent speculative trading is disabled.

**Consequence:** Trading opportunities depend on the availability of favorable institutional tenders. The algorithm cannot generate independent trading opportunities when no suitable tenders are available.

### 7.4 Execution-Time Forecasts Have Limited Historical Data

Execution-time estimates use recent completed hedges and current API/order latency.

Although very small executions are excluded from institutional timing calibration, recent samples may still be unrepresentative of future market conditions.

**Consequence:** The strategy may reject executable opportunities or accept tenders that ultimately require longer than anticipated to hedge.

### 7.5 Unresolved API Responses Can Block Trading

The algorithm avoids blindly retrying uncertain order submissions.

However, an order may remain unresolved when RIT does not provide a conclusive terminal record or sufficient inventory evidence.

In these situations, the pending-order reservation remains active and further order submissions are blocked.

**Consequence:** A single ambiguous execution can prevent subsequent tender evaluation or liquidation, potentially leaving inventory exposed until the simulation ends.

### 7.6 Order Reservations Are Not Crash-Safe

Pending-order reservations are maintained in process memory.

They are not stored in a persistent transaction database.

If the Python process terminates unexpectedly, the strategy loses its internal reservation state.

**Consequence:** Restarting without independently reconciling outstanding exchange orders and inventory may cause incorrect or duplicate execution.

### 7.7 Execution Across Exchanges Is Not Atomic

Child orders are submitted individually, and the order book may change between submissions.

The algorithm cannot guarantee simultaneous execution across both exchanges.

**Consequence:** Actual execution prices and filled quantities may differ from the aggregated order book estimate, leaving residual inventory or reducing realized profitability.

### 7.8 Complete Liquidation Is Not Guaranteed

The strategy begins close-out at tick 260 and targets flat inventory by tick 285.

However, execution depends on available liquidity, API responsiveness, and successful order reconciliation.

**Consequence:** Residual inventory or unresolved orders may remain when the simulation ends, potentially resulting in penalties.

### 7.9 Concurrent Trading Can Invalidate Reconciliation

The strategy assumes that it is the only process modifying the relevant THOR positions.

Some reconciliation procedures use changes in observed inventory to infer order fills.

Concurrent manual trades or another automated strategy could distort these observations.

**Consequence:** The algorithm may incorrectly attribute inventory changes to its own orders, triggering unnecessary recovery or inaccurate execution-state inference.

### 7.10 Offline Testing Does Not Establish Profitability

Passing 66 offline tests demonstrates that the algorithm behaves as expected under specific simulated conditions.

It does not establish positive expected returns, profitable live execution, or robustness across all market conditions.

Historical logs also lack the complete order book history required to reconstruct how the latest implementation would have performed on exactly the same opportunities.

**Consequence:** No reliable improvement in trading profit, Sharpe ratio, execution costs, or penalties can currently be attributed to version 12 without additional practice validation.

---

## 8. Future Improvements

### Empirical Execution-Cost Modeling

Replace manually specified liquidity assumptions with parameters estimated from actual execution data.

A larger sample of order submissions, fills, and market conditions could improve execution-cost estimates.

### Fill-Probability Estimation

Develop a model predicting the probability that a child order executes within a specified time horizon.

Potential features include:

- Order book imbalance.
- Bid-ask spread.
- Available market depth.
- Recent execution activity.
- Short-term liquidity persistence.

### Persistent Order-Management System

Store order identifiers, requested quantities, pending reservations, and reconciliation states in a durable database.

This could improve recovery following unexpected process termination.

### Adaptive Execution Scheduling

Adjust child-order size and execution aggressiveness based on market conditions, residual inventory, and remaining simulation time.

### Out-of-Sample Performance Evaluation

Collect data across multiple practice sessions and evaluate:

- Realized net profit after fees and penalties.
- Estimated versus realized hedge costs.
- Tender acceptance rates.
- Child-order fill rates.
- Execution latency.
- Residual inventory.
- End-of-case liquidation performance.
- Frequency of execution-recovery events.

These metrics would help distinguish execution-quality improvements from improvements that primarily reduce operational risk.

---

## 9. Project Scope

This project is an educational implementation of institutional tender evaluation and smart order routing within the Rotman Interactive Trader simulation environment.

It demonstrates practical applications of:

- Algorithmic trading.
- Market microstructure.
- Limit order book analysis.
- Execution-cost modeling.
- Smart order routing.
- Inventory risk management.
- API-based execution and order reconciliation.

The implementation is specific to the RIT ALGO3 case and is **not intended for production trading or deployment on real financial exchanges**.

The current implementation emphasizes execution discipline, estimated trading economics, and operational risk controls rather than independent directional speculation.
