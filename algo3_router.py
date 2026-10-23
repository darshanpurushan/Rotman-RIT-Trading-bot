"""ALGO3 educational router, extended from ALGO3_Skeleton.ipynb.
Read OPERATIONS.md before live use. Dry-run is the default.
"""

import argparse
import json
import math
import os
import time
from pathlib import Path

import requests

BASE_URL = os.getenv("RIT_BASE_URL", "http://localhost:9999/v1").rstrip("/")
API_KEY = os.getenv("RIT_API_KEY", "")
MAIN, ALT = "THOR_M", "THOR_A"  # Confirm these against /securities.
MARKETS = (MAIN, ALT)
TAKE_FEE = {MAIN: 0.01, ALT: 0.005}
MAX_ORDER = 25_000
CHILD_SIZE = 5_000
NET_LIMIT, GROSS_LIMIT = 100_000, 200_000  # Actual performance report configuration.
MIN_PROFIT = 1.0
CASE_END = 300
NO_NEW_TENDERS_TICK = 260
FLATTEN_TICK = 260
URGENT_TICK = 275
FLAT_TARGET_TICK = 285  # Reserve 15 seconds for cancellations/reconciliation.
PASSIVE_SECONDS = 1.0
PASSIVE_TOTAL_SECONDS = 6.0
PASSIVE_MAX_ATTEMPTS = 4
PASSIVE_MAX_MISSES = 2
PASSIVE_TARGET_FRACTION = 0.40
PASSIVE_CHILD_SIZE = 10_000
PASSIVE_ADVERSE_MOVE = 0.02
EARLY_PROFIT_RETENTION = 0.60
RECHECK_SECS = 1.0
POLL_SECS = 0.2
BOOK_SAMPLE_SECONDS = 0.5
NEAR_PRICE_BAND = 0.03
LOG = Path("algo3_events.jsonl")


def log(event, **data):
    row = dict(time=time.time(), event=event, **data)
    line = json.dumps(row)
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


class API:
    def __init__(self, live=False):
        self.live = live
        self.hedge_plan = None
        self.pending_tender = None
        self.tender_cache = {}
        self.latency = 0.15
        self.child_seconds = 0.6
        self.liquidity = LiquidityEstimator()
        self.s = requests.Session()
        self.s.headers["X-API-Key"] = API_KEY

    def request(self, method, path, params=None):
        # Only reads are retried. An ambiguous write must never be duplicated.
        if method != "GET" and not self.live:
            raise RuntimeError("Write attempted in dry-run mode")
        if method == "POST" and getattr(self, "pending_order", None):
            resolve_pending_order(self)
        if method == "POST" and path == "/orders":
            before = self.positions()
            validate_reducing_order(params, before)
            if any(
                o.get("ticker") in MARKETS
                for o in self.get("/orders", {"status": "OPEN"})
            ):
                raise RuntimeError(
                    "Open THOR order exists; reconcile before another order"
                )
            self.pending_order = dict(
                order_id=None,
                before=before,
                action=params["action"],
                quantity=int(params["quantity"]),
                ticker=params["ticker"],
                price=params.get("price"),
            )
        for attempt in range(5 if method == "GET" else 1):
            started = time.monotonic()
            r = self.s.request(method, BASE_URL + path, params=params, timeout=3)
            self.latency = max(
                0.05, 0.8 * self.latency + 0.2 * (time.monotonic() - started)
            )
            if method == "GET" and r.status_code == 429:
                time.sleep(0.3 * (attempt + 1))
                continue
            r.raise_for_status()
            result = r.json() if r.content else {}
            if method == "POST" and path == "/orders":
                self.pending_order["order_id"] = int(result["order_id"])
            return result
        raise RuntimeError("Read rate limit did not clear")

    def get(self, path, params=None):
        return self.request("GET", path, params)

    def positions(self):
        rows = self.get("/securities")
        found = {x["ticker"]: int(x["position"]) for x in rows}
        if not all(t in found for t in MARKETS):
            raise RuntimeError(
                f"Expected {MARKETS}; found {list(found)}. Check ticker configuration."
            )
        return {t: found[t] for t in MARKETS}

    def books(self):
        books = {
            t: self.get("/securities/book", {"ticker": t, "limit": 100})
            for t in MARKETS
        }
        self.liquidity.observe(books)
        self.last_book_observation = time.monotonic()
        return books


def aggregate(books, buying, fraction=1.0):
    depth = {}
    for ticker, book in books.items():
        for row in book.get("asks" if buying else "bids", []):
            qty = int(row["quantity"]) - int(row.get("quantity_filled", 0))
            if qty > 0:
                key = (float(row["price"]), ticker)
                depth[key] = depth.get(key, 0) + qty
    # Several resting orders can share one executable price. Apply the haircut
    # after aggregation, avoiding rounding loss on many tiny orders.
    levels = [
        (price, int(qty * fraction), ticker)
        for (price, ticker), qty in depth.items()
        if int(qty * fraction) > 0
    ]
    # Best displayed price first (NBBO principle); cheaper fee breaks ties.
    return sorted(levels, key=lambda x: (x[0] if buying else -x[0], TAKE_FEE[x[2]]))


def book_summary(books, levels=5):
    """Public price/quantity diagnostics only; omit resting trader identifiers."""
    return {
        side: [
            dict(price=p, quantity=q, ticker=t)
            for p, q, t in aggregate(books, buying)[:levels]
        ]
        for side, buying in (("asks", True), ("bids", False))
    }


def walk(levels, qty, buying):
    remaining, value, route = qty, 0.0, []
    for price, available, ticker in levels:
        take = min(remaining, available)
        if take <= 0:
            break
        value += take * (
            price + TAKE_FEE[ticker] if buying else price - TAKE_FEE[ticker]
        )
        route.append((ticker, take, price))
        remaining -= take
    return qty - remaining, value, route


def position_value(pos):
    """This client's two tickers mirror ONE underlying inventory."""
    if pos[MAIN] != pos[ALT]:
        raise RuntimeError(f"Mirrored position fields disagree: {pos}")
    return pos[MAIN]


def shifted(pos, change):
    value = position_value(pos) + change
    return {MAIN: value, ALT: value}


def within_limits(pos):
    # Count directional inventory once. Conservatively count both reported
    # absolute positions for gross until live /limits accounting is verified.
    return (
        abs(position_value(pos)) <= NET_LIMIT
        and sum(abs(v) for v in pos.values()) <= GROSS_LIMIT
    )


def validate_reducing_order(params, pos):
    """Block speculative entries and crossing zero at the write boundary.

    Reads cannot make submission atomic with concurrent manual activity.
    Use one dedicated bot; unexpected changes still enter recovery.
    """
    if not params or params.get("ticker") not in MARKETS:
        raise RuntimeError("Only THOR inventory-reducing orders are supported")
    quantity = params.get("quantity", 0)
    if (
        not isinstance(quantity, (int, float))
        or isinstance(quantity, bool)
        or not 0 < quantity <= MAX_ORDER
        or int(quantity) != quantity
    ):
        raise RuntimeError("Invalid reducing order quantity")
    value = position_value(pos)
    action = params.get("action")
    if value == 0:
        raise RuntimeError("Speculative order blocked: position is flat")
    if action != ("SELL" if value > 0 else "BUY"):
        raise RuntimeError("Order blocked: would increase exposure")
    if quantity > abs(value):
        raise RuntimeError("Order blocked: would cross zero into new exposure")


def execution_budget(qty, child_seconds=0.6):
    return max(
        12.0,
        ((qty + CHILD_SIZE - 1) // CHILD_SIZE) * child_seconds * 1.5
        + PASSIVE_TOTAL_SECONDS
        + 5,
    )


def submission_ready(t, tick, latency):
    remaining = int(t.get("expires", tick + 30)) - tick
    return remaining > max(2, 3 * latency), remaining


class LiquidityEstimator:
    """Recent displayed persistence/additions, not guaranteed future liquidity.

    Additions count only price/venue keys present in consecutive snapshots;
    quote movement is not called replenishment. Cancellations and executions
    cannot be separated without trade data. History expires after long gaps.
    """

    def __init__(self):
        self.history = []
        self.fill_ratio = 1.0

    def observe(self, books, stamp=None):
        stamp = time.monotonic() if stamp is None else stamp
        if self.history and stamp - self.history[-1][0] < 0.2:
            return
        if self.history and stamp - self.history[-1][0] > 3:
            self.history = []
        snapshot = {}
        for buying in (True, False):
            snapshot[buying] = {(p, t): q for p, q, t in aggregate(books, buying)}
        self.history = [(ts, b) for ts, b in self.history if stamp - ts <= 8]
        self.history.append((stamp, snapshot))

    def record_fill(self, filled, requested):
        if requested > 0:
            self.fill_ratio = 0.7 * self.fill_ratio + 0.3 * min(1, filled / requested)

    def near_price_profile(self, buying, horizon):
        h = self.history
        elapsed = h[-1][0] - h[0][0] if len(h) > 1 else 0
        if len(h) < 4 or elapsed < 3 or time.monotonic() - h[-1][0] > 2:
            return dict(
                near_refill_capacity=0,
                near_price_band=NEAR_PRICE_BAND,
                adverse_per_second=0.0,
                price_noise_per_second=0.0,
            )
        references = []
        for _, snapshot in h:
            # Use the marginal price for 1,000 shares to avoid one-share quotes
            # dominating the price movement estimate. Routing still honors NBBO.
            levels = sorted(
                snapshot[buying].items(),
                key=lambda item: item[0][0],
                reverse=not buying,
            )
            cumulative = 0
            for (price, _), quantity in levels:
                cumulative += quantity
                if cumulative >= 1000:
                    references.append(price)
                    break
            else:
                if levels:
                    references.append(levels[-1][0][0])
        if not references:
            return dict(
                near_refill_capacity=0,
                near_price_band=NEAR_PRICE_BAND,
                adverse_per_second=0.0,
                price_noise_per_second=0.0,
            )
        reference = references[-1]

        def near(price):
            return abs(price - reference) <= NEAR_PRICE_BAND + 1e-9

        additions = 0
        for (_, a), (_, b) in zip(h, h[1:]):
            old, new = a[buying], b[buying]
            additions += sum(
                max(0, new[k] - old[k]) for k in old.keys() & new.keys() if near(k[0])
            )
        movement = (references[-1] - references[0]) * (1 if buying else -1)
        return dict(
            near_refill_capacity=int(
                0.25 * additions / elapsed * min(8, max(0, horizon))
            ),
            near_price_band=NEAR_PRICE_BAND,
            adverse_per_second=max(0.0, movement / elapsed),
            price_noise_per_second=(max(references) - min(references)) / elapsed,
        )

    def profile(self, buying, horizon):
        now = time.monotonic()
        h = self.history
        mature = len(h) >= 4 and h[-1][0] - h[0][0] >= 3 and now - h[-1][0] <= 2
        fraction, refill_rate, stress = 0.85, 0.0, 0.02
        persistence = None
        if mature:
            retained, additions, oldtotal = 0, 0, 0
            prices = []
            for _, s in h:
                keys = s[buying]
                if keys:
                    prices.append(
                        min(p for p, t in keys) if buying else max(p for p, t in keys)
                    )
            for (_, a), (_, b) in zip(h, h[1:]):
                a, b = a[buying], b[buying]
                oldtotal += sum(a.values())
                retained += sum(min(q, b.get(k, 0)) for k, q in a.items())
                additions += sum(max(0, b[k] - a[k]) for k in a.keys() & b.keys())
            persistence = retained / max(1, oldtotal)
            fraction = max(0.5, min(0.95, 0.5 + 0.45 * persistence))
            refill_rate = 0.25 * additions / (h[-1][0] - h[0][0])
            if prices:
                # Larger observed movement widens the unseen-tail allowance.
                stress = max(0.01, (max(prices) - min(prices)) * 0.5)
        fraction = max(0.5, min(fraction, 0.5 + 0.45 * self.fill_ratio))
        stress += 0.02 * (1 - self.fill_ratio)
        return dict(
            depth_fraction=fraction,
            tail_stress=stress,
            refill_capacity=int(refill_rate * min(8, max(0, horizon))),
            mature=mature,
            persistence=persistence,
            fill_ratio=self.fill_ratio,
            history_samples=len(h),
            **self.near_price_profile(buying, horizon),
        )


def estimate_sweep(t, books, profile):
    qty = int(t["quantity"])
    buying = t["action"] == "SELL"
    levels = aggregate(books, buying, profile["depth_fraction"])
    filled, value, route = walk(levels, qty, buying)
    missing = qty - filled
    # Initial tail allowance: 35%; persistent observed additions can support
    # up to another 30%. Never assume the entire tender can use future depth.
    refill = min(profile["refill_capacity"], int(qty * 0.30))
    allowed_missing = int(qty * 0.35) + refill
    details = dict(
        profile,
        visible_shares=filled,
        unobserved_shares=missing,
        credited_refill_shares=refill,
        allowed_unobserved_shares=allowed_missing,
    )
    raw_coverage = min(qty, sum(q for _, q, _ in aggregate(books, buying)))
    observed_capacity = min(
        int(qty * 0.75), int(profile.get("recent_execution_capacity", 0))
    )
    allowed_missing = max(allowed_missing, raw_coverage - filled)
    if filled >= qty * 0.25:
        allowed_missing = max(allowed_missing, observed_capacity)
    details.update(
        current_displayed_coverage=raw_coverage,
        recent_execution_capacity=observed_capacity,
        allowed_unobserved_shares=allowed_missing,
    )
    if not route or missing > allowed_missing:
        return None, details
    if missing:
        ticker, _, price = route[-1]
        stressed = (
            price + profile["tail_stress"]
            if buying
            else max(0.01, price - profile["tail_stress"])
        )
        value += missing * (
            stressed + TAKE_FEE[ticker] if buying else stressed - TAKE_FEE[ticker]
        )
    edge = (
        qty * float(t["price"]) - value if buying else value - qty * float(t["price"])
    )
    # Small execution uncertainty charged on all shares. This is a model
    # assumption, separate from fees and from the unseen-tail allowance.
    uncertainty = qty * max(0.0025, profile["tail_stress"] * 0.10)
    details.update(
        estimated_hedge_value=value,
        uncertainty_cost=uncertainty,
        estimated_net_edge=edge - uncertainty,
    )
    return edge - uncertainty, details


def estimate_unwind(t, books, profile):
    """Compare an immediate sweep with a supported, bounded replenishment plan.

    Future shares require recent additions near the current price. Participant
    count, deep-book additions and prior hedge speed do not create cheap depth.
    """
    sweep_edge, details = estimate_sweep(t, books, profile)
    details.update(
        execution_model="sweep",
        sweep_net_edge=sweep_edge,
        patient_net_edge=None,
        future_depth_guaranteed=False,
    )
    qty = int(t["quantity"])
    buying = t["action"] == "SELL"
    levels = aggregate(books, buying, profile["depth_fraction"])
    if not levels or not profile.get("mature"):
        return sweep_edge, details
    touch = levels[0][0]
    band = min(NEAR_PRICE_BAND, max(0, profile.get("near_price_band", 0)))
    near = [level for level in levels if abs(level[0] - touch) <= band + 1e-9]
    visible, value, route = walk(near, qty, buying)
    missing = qty - visible
    capacity = min(int(profile.get("near_refill_capacity", 0)), int(qty * 0.65))
    if visible < qty * 0.25 or missing <= 0 or missing > capacity:
        return sweep_edge, details
    venue, _, worst_price = route[-1]
    stress = max(profile["tail_stress"], 8 * profile.get("adverse_per_second", 0))
    future_price = worst_price + (stress if buying else -stress)
    if not 5 <= future_price <= 20:
        return sweep_edge, details
    value += missing * (
        future_price + TAKE_FEE[venue] if buying else future_price - TAKE_FEE[venue]
    )
    uncertainty = qty * max(0.0025, stress * 0.10)
    edge = (
        qty * float(t["price"]) - value if buying else value - qty * float(t["price"])
    ) - uncertainty
    details.update(
        patient_net_edge=edge,
        patient_visible_shares=visible,
        patient_future_shares=missing,
        near_refill_capacity=capacity,
        patient_future_price=future_price,
    )
    if sweep_edge is None or edge > sweep_edge:
        details.update(
            execution_model="observed_near_price_replenishment",
            visible_shares=visible,
            unobserved_shares=missing,
            credited_refill_shares=missing,
            estimated_hedge_value=value,
            uncertainty_cost=uncertainty,
            estimated_net_edge=edge,
        )
        return edge, details
    return sweep_edge, details


def should_accept(t, books, pos, tick, child_seconds=0.6, profile=None):
    if tick >= NO_NEW_TENDERS_TICK or FLAT_TARGET_TICK - tick < execution_budget(
        int(t["quantity"]), child_seconds
    ):
        return False, "insufficient execution time remaining", None
    if position_value(pos) != 0 or not within_limits(pos):
        return False, "existing net exposure or limit breach", None
    if t.get("is_fixed_bid") is False or t.get("price") is None:
        return False, "only fixed-price offers supported", None
    if t.get("ticker") not in MARKETS or t.get("action") not in ("BUY", "SELL"):
        return False, "unknown ticker or action", None
    qty = int(t["quantity"])
    if qty <= 0 or tick >= int(t.get("expires", tick + 1)):
        return False, "invalid quantity or expired", None
    # RIT trader-perspective convention: BUY tender creates a long position.
    sign = 1 if t["action"] == "BUY" else -1
    buying = sign < 0
    projected = shifted(pos, sign * qty)
    if not within_limits(projected):
        return False, "tender breaches limits", None
    if profile is None:
        profile = LiquidityEstimator().profile(
            buying, execution_budget(qty, child_seconds)
        )
    profit, details = estimate_unwind(t, books, profile)
    if profit is None:
        return False, "insufficient adaptive depth and observed replenishment", None
    return (
        profit >= MIN_PROFIT,
        f"estimated net edge ${profit:.2f}; required ${MIN_PROFIT:.2f}",
        profit,
    )


def wait_positions(api, expected, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        p = api.positions()
        if p == expected:
            return p
        time.sleep(0.1)
    raise RuntimeError(
        f"Position reconciliation failed: expected {expected}, observed {p}"
    )


def is_missing_order(exc):
    return (
        isinstance(exc, requests.HTTPError)
        and exc.response is not None
        and exc.response.status_code == 404
    )


def get_order_detail(api, oid):
    try:
        return api.get(f"/orders/{oid}")
    except requests.HTTPError as exc:
        if not is_missing_order(exc):
            raise
        for status in ("TRANSACTED", "CANCELLED", "OPEN"):
            for order in api.get("/orders", {"status": status}):
                if int(order["order_id"]) == oid and order.get("ticker") in MARKETS:
                    log(
                        "order_status_fallback",
                        order_id=oid,
                        status=order.get("status"),
                    )
                    return order
        raise


def reconcile_missing_order(api, oid, before, action, requested):
    """A missing detail record is not evidence of a fill.

    One child at a time, dedicated session: cancel any remaining child, require
    no THOR orders, then attribute a stable bounded inventory change to it.
    Zero/invalid deltas stay unresolved and enter recovery; never replay POST.
    """
    if before is None:
        raise RuntimeError(
            f"Cannot reconcile missing order {oid} without pre-order inventory"
        )
    # A known child can reappear briefly after cancellation due to delayed
    # order-list visibility. Retry only that ID; unrelated orders remain fatal.
    for attempt in range(3):
        orders = [
            o
            for o in api.get("/orders", {"status": "OPEN"})
            if o.get("ticker") in MARKETS
        ]
        if any(int(o["order_id"]) != oid for o in orders):
            raise RuntimeError(
                "Unexpected concurrent THOR order during fill reconciliation"
            )
        for order in orders:
            cancel_order(api, oid)
        after = stable_positions(api)
        remaining = [
            o
            for o in api.get("/orders", {"status": "OPEN"})
            if o.get("ticker") in MARKETS
        ]
        if any(int(o["order_id"]) != oid for o in remaining):
            raise RuntimeError(
                "Unexpected concurrent THOR order during fill reconciliation"
            )
        if not remaining:
            break
        log(
            "reconciliation_wait",
            order_id=oid,
            attempt=attempt + 1,
            reason="Known child still visible; no replacement order submitted",
        )
        time.sleep(0.15)
    else:
        raise RuntimeError(f"Order {oid} still open after bounded reconciliation")
    direction = 1 if action == "BUY" else -1
    filled = direction * (position_value(after) - position_value(before))
    if not 0 < filled <= requested:
        raise RuntimeError(
            f"Unresolved missing order {oid}: inferred fill {filled}, requested {requested}"
        )
    log(
        "order_reconciled",
        order_id=oid,
        source="stable_position_change",
        filled=filled,
        positions=after,
    )
    return filled


def execute_child(
    api, ticker, action, quantity, price=None, rest_seconds=0.1, before=None
):
    started = time.monotonic()
    params = dict(
        ticker=ticker,
        type="MARKET" if price is None else "LIMIT",
        quantity=quantity,
        action=action,
    )
    if price is not None:
        params["price"] = price
    result = api.request("POST", "/orders", params)
    oid = int(result["order_id"])
    # Marketable limit bounds price, but may rest. Cancel and confirm final fills.
    time.sleep(rest_seconds)
    source = "order_status"
    try:
        order = get_order_detail(api, oid)
        if order["status"] == "OPEN":
            cancel_order(api, oid)
        deadline = time.monotonic() + 3
        while order["status"] not in ("TRANSACTED", "CANCELLED"):
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Order {oid} not terminal; inspect before restarting"
                )
            time.sleep(0.1)
            order = get_order_detail(api, oid)
        filled = int(order["quantity_filled"])
    except requests.HTTPError as exc:
        if not is_missing_order(exc):
            raise
        filled = reconcile_missing_order(api, oid, before, action, quantity)
        source = "stable_position_change"
    if not 0 <= filled <= quantity:
        raise RuntimeError(f"Invalid reported fill for order {oid}: {filled}")
    if getattr(api, "pending_order", None):
        resolve_pending_order(api, order if source == "order_status" else None)
    else:
        credit_hedge_fill(api, ticker, action, filled, price)
    if hasattr(api, "liquidity") and price is not None and rest_seconds <= 0.1:
        api.liquidity.record_fill(filled, quantity)
    log(
        "child",
        order_id=oid,
        ticker=ticker,
        action=action,
        requested=quantity,
        filled=filled,
        price=price,
        fill_source=source,
        price_is_submitted_limit=True,
        rest_seconds=rest_seconds,
    )
    if hasattr(api, "child_seconds"):
        api.child_seconds = max(
            0.2, 0.7 * api.child_seconds + 0.3 * (time.monotonic() - started)
        )
    return filled


def resolve_pending_order(api, known_detail=None):
    pending = getattr(api, "pending_order", None)
    if not pending:
        return
    oid = pending["order_id"]
    if oid is None:
        raise RuntimeError(
            "Uncertain order submission without ID: new orders blocked; inspect RIT"
        )
    try:
        detail = (
            known_detail if known_detail is not None else get_order_detail(api, oid)
        )
    except requests.HTTPError as exc:
        if not is_missing_order(exc):
            raise
        detail = None
    if detail is not None and detail.get("status") == "OPEN":
        cancel_order(api, oid)
        raise RuntimeError(
            f"Pending order {oid}: awaiting terminal confirmation; no replacement"
        )
    orders = [
        o for o in api.get("/orders", {"status": "OPEN"}) if o.get("ticker") in MARKETS
    ]
    if orders:
        raise RuntimeError(
            f"Pending order {oid}: open THOR orders remain; no replacement"
        )
    after = stable_positions(api)
    direction = 1 if pending["action"] == "BUY" else -1
    delta = direction * (position_value(after) - position_value(pending["before"]))
    terminal = detail is not None and detail.get("status") in (
        "TRANSACTED",
        "CANCELLED",
    )
    expected = (
        int(detail.get("quantity_filled", -1)) if terminal else pending["quantity"]
    )
    # Missing detail + unchanged/partial inventory is NOT proof of completion.
    # Only a terminal record with matching inventory, or the entire requested
    # quantity observed filled with no open orders, releases the reservation.
    if not 0 <= expected <= pending["quantity"] or delta != expected:
        raise RuntimeError(
            f"Pending order {oid}: unresolved fill {delta}; reservation retained"
        )
    if any(o.get("ticker") in MARKETS for o in api.get("/orders", {"status": "OPEN"})):
        raise RuntimeError(
            f"Pending order {oid}: order list changed; reservation retained"
        )
    credit_hedge_fill(
        api, pending.get("ticker"), pending["action"], delta, pending.get("price")
    )
    api.pending_order = None
    log(
        "pending_order_resolved",
        order_id=oid,
        filled=delta,
        positions=after,
        source="terminal_record" if terminal else "full_requested_quantity_observed",
    )


def cancel_order(api, oid):
    try:
        api.request("DELETE", f"/orders/{oid}")
    except requests.HTTPError as cancel_error:
        # A fill can win the cancellation race. Confirm that before proceeding.
        try:
            order = get_order_detail(api, oid)
        except requests.HTTPError as read_error:
            if not is_missing_order(read_error):
                raise
            if any(
                int(o["order_id"]) == oid
                for o in api.get("/orders", {"status": "OPEN"})
            ):
                raise cancel_error
            return  # Caller still must reconcile inventory before another child.
        if order["status"] not in ("TRANSACTED", "CANCELLED"):
            raise


def cancel_open_orders(api):
    """Dedicated bot session: cancel THOR orders, then verify none remain."""
    for order in api.get("/orders", {"status": "OPEN"}):
        if order.get("ticker") in MARKETS:
            cancel_order(api, int(order["order_id"]))
    remaining = [
        o for o in api.get("/orders", {"status": "OPEN"}) if o.get("ticker") in MARKETS
    ]
    if remaining:
        raise RuntimeError("Cancellation not yet confirmed; no new orders submitted")


def stable_positions(api, seconds=3):
    deadline = time.monotonic() + seconds
    last = None
    consecutive = 0
    while time.monotonic() < deadline:
        p = api.positions()
        if p[MAIN] == p[ALT] and p == last:
            consecutive += 1
        else:
            consecutive = 0
        last = p
        if consecutive >= 2:
            return p
        time.sleep(0.15)
    raise RuntimeError(f"Position snapshot did not stabilize: {last}")


def update_exit_value(api, books, remaining, tick):
    """Mark the residual at the current executable book, not last-trade P&L."""
    plan = getattr(api, "hedge_plan", None)
    if not plan or plan.get("profit_bound") is None:
        return None
    buying = plan["action"] == "SELL"
    filled, value, route = walk(aggregate(books, buying), remaining, buying)
    projected = None
    if filled == remaining:
        projected = plan["profit_bound"] + (
            remaining * plan["entry_price"] - value
            if buying
            else value - remaining * plan["entry_price"]
        )
        plan["peak_exit_estimate"] = max(plan.get("peak_exit_estimate", 0), projected)
    plan["current_exit_estimate"] = projected
    start = plan.get("start_tick", tick)
    duration = max(1, plan["soft_deadline"] - start)
    progress = max(0.0, min(1.0, (tick - start) / duration))
    retention = EARLY_PROFIT_RETENTION * (1 - progress) + 0.10 * progress
    plan["active_profit_target"] = max(
        plan["keep_profit"], max(0, plan.get("peak_exit_estimate", 0)) * retention
    )
    log(
        "exit_valuation",
        tick=tick,
        tender_id=plan["tender_id"],
        remaining=remaining,
        displayed_coverage=filled,
        projected_net_bound=projected,
        peak_exit_estimate=plan.get("peak_exit_estimate", 0),
        active_profit_target=plan["active_profit_target"],
        last_displayed_price=route[-1][2] if route else None,
        book=book_summary(books),
        actual_fill_prices_available=False,
    )
    return projected


def begin_hedge_plan(
    api,
    tender,
    books,
    tick,
    source="accepted_tender",
    profile=None,
    estimated_edge=None,
):
    buying = tender["action"] == "SELL"
    profile = profile or (
        getattr(api, "liquidity", None) or LiquidityEstimator()
    ).profile(buying, 8)
    edge, details = estimate_unwind(tender, books, profile)
    if estimated_edge is not None:
        edge = estimated_edge
    plan = dict(
        tender_id=tender.get("tender_id"),
        action=tender["action"],
        entry_price=float(tender["price"]),
        quantity=int(tender["quantity"]),
        profit_bound=0.0,
        estimated_entry_edge=edge,
        start_tick=tick,
        peak_exit_estimate=0.0,
        execution_model=details.get("execution_model"),
        keep_profit=max(1.0, max(0.0, edge or 0) * 0.10),
        soft_deadline=min(
            FLATTEN_TICK,
            tick
            + max(
                8,
                min(
                    30,
                    int(
                        execution_budget(
                            int(tender["quantity"]), getattr(api, "child_seconds", 0.6)
                        )
                    ),
                ),
            ),
        ),
        source=source,
    )
    api.hedge_plan = plan
    update_exit_value(api, books, int(tender["quantity"]), tick)
    log("hedge_plan", **plan)
    return plan


def credit_hedge_fill(api, ticker, action, filled, price):
    plan = getattr(api, "hedge_plan", None)
    if not plan or not filled or plan["profit_bound"] is None:
        return
    if price is None or ticker not in TAKE_FEE:
        plan["profit_bound"] = None
        return
    if action != ("BUY" if plan["action"] == "SELL" else "SELL"):
        raise RuntimeError("Hedge fill direction does not match tender plan")
    per_share = (
        plan["entry_price"] - price - TAKE_FEE[ticker]
        if action == "BUY"
        else price - TAKE_FEE[ticker] - plan["entry_price"]
    )
    plan["profit_bound"] += filled * per_share
    log(
        "hedge_budget",
        tender_id=plan["tender_id"],
        conservative_profit_bound=plan["profit_bound"],
        target_profit=plan["keep_profit"],
        remaining_budget_is_estimate=True,
    )


def protect_exit(api, books, buying, remaining, tick, ticker, price, rest_seconds=0.1):
    plan = getattr(api, "hedge_plan", None)
    if not plan or plan["profit_bound"] is None or tick >= plan["soft_deadline"]:
        return ticker, price, rest_seconds
    if buying != (plan["action"] == "SELL"):
        raise RuntimeError(
            "Inventory direction changed; tender price attribution invalid"
        )

    # Reserve time for active execution; a stale profit target cannot consume
    # the entire hedge window or push all residual shares into the hard deadline.
    active_seconds = (
        math.ceil(remaining / CHILD_SIZE) * max(0.2, getattr(api, "child_seconds", 0.6))
        + 2
    )
    if "start_tick" in plan and tick + active_seconds >= plan["soft_deadline"]:
        log(
            "profit_target_released",
            tick=tick,
            reason="active_execution_time_reserved",
            target=plan.get("active_profit_target", plan["keep_profit"]),
        )
        return ticker, price, rest_seconds

    def bound(venue):
        allowance = (
            plan["profit_bound"] - plan.get("active_profit_target", plan["keep_profit"])
        ) / max(1, remaining)
        return (
            plan["entry_price"] - TAKE_FEE[venue] + allowance
            if buying
            else plan["entry_price"] + TAKE_FEE[venue] - allowance
        )

    limit = bound(ticker)
    if (buying and price <= limit) or (not buying and price >= limit):
        return ticker, price, rest_seconds
    # A patient, bounded quote replaces a blind sweep while time remains.
    ticker = ALT
    limit = bound(ticker)
    price = (
        math.floor((limit + 1e-9) * 100) / 100
        if buying
        else math.ceil((limit - 1e-9) * 100) / 100
    )
    if not 5 <= price <= 20:
        raise RuntimeError(
            "Protected exit outside case price bounds; manual review required"
        )
    log(
        "profit_protection",
        tick=tick,
        tender_id=plan["tender_id"],
        price=price,
        quantity_remaining=remaining,
        soft_deadline=plan["soft_deadline"],
        source=plan["source"],
    )
    return ticker, price, max(0.5, rest_seconds)


def matching_manual_tender(api, position, tick):
    action = "BUY" if position > 0 else "SELL"
    candidates = [
        t
        for t in getattr(api, "tender_cache", {}).values()
        if t.get("action") == action
        and int(t.get("quantity", 0)) == abs(position)
        and t.get("price") is not None
        and t.get("ticker") in MARKETS
        and tick <= int(t.get("expires", tick)) + 5
    ]
    return candidates[0] if len(candidates) == 1 else None


def passive_exit_quote(books, buying, remaining, target_left):
    bids, asks = aggregate(books, False), aggregate(books, True)
    if not bids or not asks or ALT not in books:
        return None
    bid, ask = bids[0][0], asks[0][0]
    if bid >= ask:
        return None
    improve = 0.01 if ask - bid >= 0.03 - 1e-9 else 0.0
    raw = min(bid + improve, ask - 0.01) if buying else max(ask - improve, bid + 0.01)
    price = (
        math.floor((raw + 1e-9) * 100) / 100
        if buying
        else math.ceil((raw - 1e-9) * 100) / 100
    )
    if (
        not 5 <= price <= 20
        or (buying and price >= ask)
        or (not buying and price <= bid)
    ):
        return None
    qty = min(int(remaining), int(target_left), PASSIVE_CHILD_SIZE, MAX_ORDER)
    return (ALT, price, qty) if qty > 0 else None


def unwind_schedule(api, initial, buying):
    schedule = getattr(api, "unwind_schedule", None)
    if (
        schedule is None
        or schedule["buying"] != buying
        or initial > schedule["initial"]
    ):
        schedule = dict(
            initial=initial,
            buying=buying,
            started=time.monotonic(),
            attempts=0,
            misses=0,
            passive_filled=0,
            midpoint=None,
            adverse=False,
        )
        api.unwind_schedule = schedule
    return schedule


def active_exit_child(api, books, buying, remaining, tick):
    """Combine nearby same-venue levels without trading through another venue."""
    levels = aggregate(books, buying)
    if not levels:
        return None
    best, _, venue = levels[0]
    seconds_left = max(1, FLAT_TARGET_TICK - tick)
    desired = math.ceil(
        remaining * max(0.2, getattr(api, "child_seconds", 0.6)) / seconds_left
    )
    cap = min(remaining, MAX_ORDER, max(CHILD_SIZE, min(10000, desired)))
    quantity, limit = 0, best
    for price, available, ticker in levels:
        # Price priority across venues remains authoritative. Stop before the
        # next venue; never sweep through a better competing displayed quote.
        if ticker != venue or abs(price - best) > 0.01 + 1e-9:
            break
        take = min(cap - quantity, available)
        quantity += take
        limit = price
        if quantity >= cap:
            break
    return venue, limit, quantity


def choose_unwind_order(api, books, buying, remaining, tick, schedule):
    if tick >= URGENT_TICK:
        levels = aggregate(books, buying)
        return dict(
            ticker=levels[0][2] if levels else ALT,
            price=None,
            quantity=min(remaining, MAX_ORDER),
            rest=0.1,
            passive=False,
            reason="urgent_deadline",
        )
    levels = aggregate(books, buying)
    if not levels:
        return None
    bid, ask = aggregate(books, False), aggregate(books, True)
    if bid and ask and bid[0][0] < ask[0][0]:
        midpoint = (bid[0][0] + ask[0][0]) / 2
        if schedule["midpoint"] is None:
            schedule["midpoint"] = midpoint
        movement = midpoint - schedule["midpoint"]
        if (movement if buying else -movement) >= PASSIVE_ADVERSE_MOVE - 1e-9:
            schedule["adverse"] = True
    target_left = max(
        0,
        math.ceil(schedule["initial"] * PASSIVE_TARGET_FRACTION)
        - schedule["passive_filled"],
    )
    elapsed = time.monotonic() - schedule["started"]
    time_needed = execution_budget(
        remaining, max(0.6, getattr(api, "child_seconds", 0.6))
    )
    plan = getattr(api, "hedge_plan", None)
    can_wait = (
        tick < FLATTEN_TICK
        and FLATTEN_TICK - tick > time_needed + PASSIVE_SECONDS + 2
        and elapsed + PASSIVE_SECONDS <= PASSIVE_TOTAL_SECONDS
        and schedule["attempts"] < PASSIVE_MAX_ATTEMPTS
        and schedule["misses"] < PASSIVE_MAX_MISSES
        and not schedule["adverse"]
        and target_left > 0
        and (not plan or tick + PASSIVE_SECONDS < plan["soft_deadline"])
    )
    quote = (
        passive_exit_quote(books, buying, remaining, target_left) if can_wait else None
    )
    wait_cost = 0.0
    advantage = 0.0
    if quote:
        quote_venue, quote_price, _ = quote
        active_price, _, active_venue = levels[0]
        advantage = (
            (active_price + TAKE_FEE[active_venue])
            - (quote_price + TAKE_FEE[quote_venue])
            if buying
            else (quote_price - TAKE_FEE[quote_venue])
            - (active_price - TAKE_FEE[active_venue])
        )
        estimator = getattr(api, "liquidity", None)
        if estimator:
            profile = estimator.profile(buying, PASSIVE_SECONDS)
            wait_cost = PASSIVE_SECONDS * max(
                profile.get("adverse_per_second", 0),
                0.5 * profile.get("price_noise_per_second", 0),
            )
        # A favorable movement of two cents is an opportunity to lock an exit.
        favorable = (
            (
                schedule["midpoint"] - midpoint
                if buying
                else midpoint - schedule["midpoint"]
            )
            if bid and ask and bid[0][0] < ask[0][0]
            else 0
        )
        if advantage <= wait_cost or favorable >= PASSIVE_ADVERSE_MOVE - 1e-9:
            quote = None
    if quote:
        ticker, price, qty = quote
        return dict(
            ticker=ticker,
            price=price,
            quantity=qty,
            rest=PASSIVE_SECONDS,
            passive=True,
            reason="bounded_passive_phase",
            passive_price_advantage=advantage,
            observed_wait_cost=wait_cost,
        )
    ticker, price, quantity = active_exit_child(api, books, buying, remaining, tick)
    reason = (
        "adverse_price_move"
        if schedule["adverse"]
        else (
            "passive_no_fill"
            if schedule["misses"] >= PASSIVE_MAX_MISSES
            else "execution_schedule"
        )
    )
    return dict(
        ticker=ticker,
        price=price,
        quantity=quantity,
        rest=0.1,
        passive=False,
        reason=reason,
    )


def cover(api, before, after, liquidating=False):
    """Close shared underlying inventory, counting mirrored fields only once."""
    target = position_value(before)
    p = dict(after)
    initial = abs(position_value(p) - target)
    schedule = unwind_schedule(api, initial, position_value(p) < target)
    started = time.monotonic()
    while position_value(p) != target:
        case = api.get("/case")
        if case["status"] != "ACTIVE":
            raise RuntimeError(f"Case ended before flattening: {p}")
        tick = int(case["tick"])
        delta = position_value(p) - target
        buying = delta < 0
        try:
            books = api.books()
        except requests.RequestException:
            if tick < URGENT_TICK:
                raise
            # At the hard close-out deadline, book-read failure must not block
            # reducing known inventory through a configured venue.
            books = {t: {} for t in MARKETS}
            log("urgent_book_unavailable", tick=tick, positions=p)
        # Refresh time after book reads: calls may be slow near the deadline.
        case = api.get("/case")
        if case["status"] != "ACTIVE":
            raise RuntimeError(f"Case ended before flattening: {p}")
        tick = int(case["tick"])
        urgent = tick >= URGENT_TICK
        force = tick >= FLATTEN_TICK
        update_exit_value(api, books, abs(delta), tick)
        selection = choose_unwind_order(api, books, buying, abs(delta), tick, schedule)
        if selection is None:
            time.sleep(POLL_SECS)
            continue
        ticker, price, qty, rest = (
            selection[k] for k in ("ticker", "price", "quantity", "rest")
        )
        if not urgent:
            ticker, price, rest = protect_exit(
                api, books, buying, abs(delta), tick, ticker, price, rest_seconds=rest
            )
        if selection["passive"]:
            schedule["attempts"] += 1
        log(
            "unwind_order",
            tick=tick,
            ticker=ticker,
            quantity=qty,
            price=price,
            execution_style=(
                "passive_limit"
                if selection["passive"]
                else (
                    "market"
                    if urgent
                    else "protected_limit" if rest > 0.1 else "marketable_limit"
                )
            ),
            wait_seconds=rest,
            reason=selection["reason"],
            passive_attempts=schedule["attempts"],
            passive_filled=schedule["passive_filled"],
            passive_price_advantage=selection.get("passive_price_advantage"),
            observed_wait_cost=selection.get("observed_wait_cost"),
        )
        # Every order must strictly reduce actual shared exposure, including recovery.
        change = qty if buying else -qty
        proposed = shifted(p, change)
        if abs(position_value(proposed) - target) >= abs(delta):
            raise RuntimeError("Order would not reduce exposure")
        if not within_limits(proposed) and abs(position_value(proposed)) >= abs(
            position_value(p)
        ):
            raise RuntimeError("Order would worsen a position limit breach")
        filled = execute_child(
            api, ticker, "BUY" if buying else "SELL", qty, price, rest, before=p
        )
        if selection["passive"]:
            schedule["passive_filled"] += filled
            if not filled:
                schedule["misses"] += 1
        p = wait_positions(api, shifted(p, filled if buying else -filled))
        log(
            "inventory",
            tick=tick,
            positions=p,
            underlying_position=position_value(p),
            liquidating=force,
            urgent=urgent,
            elapsed=round(time.monotonic() - started, 2),
        )
        if not filled:
            time.sleep(POLL_SECS)
    cancel_open_orders(api)
    # Validate both raw fields after all cancellations (not merely a net sum).
    p = wait_positions(api, before)
    elapsed = time.monotonic() - started
    if initial:
        # Includes book reads, partial fills, cancellation and reconciliation.
        record_hedge_timing(api, initial, elapsed)
        log(
            "hedge_summary",
            shares=initial,
            seconds=round(elapsed, 3),
            used_for_tender_timing=initial >= 10000,
            underlying_position=position_value(p),
        )
    if target == 0:
        log_flat(api, p, force=True)
        api.hedge_plan = None
        api.unwind_schedule = None
    else:
        log(
            "hedge_complete",
            positions=p,
            underlying_position=position_value(p),
            open_orders=0,
        )
    return p


def record_hedge_timing(api, shares, seconds):
    # Setup/reconciliation overhead dominates tiny residuals. Never extrapolate
    # their seconds/share to institutional tenders. Keep logging them separately.
    if shares < 10000:
        return
    recent = (getattr(api, "recent_hedge_rates", []) + [seconds / shares])[-3:]
    api.recent_hedge_rates = recent
    api.hedge_seconds_per_share = max(recent)
    observations = getattr(api, "hedge_observations", [])
    api.hedge_observations = (
        observations + [(time.monotonic(), shares / max(0.1, seconds))]
    )[-3:]


def log_flat(api, p, force=False):
    now = time.monotonic()
    if force or now - getattr(api, "_last_flat_log", -100) >= 5:
        log("flat_confirmed", positions=p, underlying_position=0, open_orders=0)
        api._last_flat_log = now


def recovery_step(api):
    # Never replay a failed POST. Reconcile server state, cancel live orders,
    # and reduce only the inventory actually observed. Called repeatedly until
    # case close, so delayed changes trigger another close-out attempt.
    resolve_pending_order(api)
    cancel_open_orders(api)
    p = stable_positions(api)
    pending = getattr(api, "pending_tender", None)
    if pending:
        expected = shifted(pending["before"], pending["change"])
        if p != expected:
            raise RuntimeError(
                "Uncertain tender submission: position not confirmed; no acceptance retry or rearm"
            )
        api.pending_tender = None
    if position_value(p):
        return cover(api, dict.fromkeys(MARKETS, 0), p, liquidating=True)
    log_flat(api, p)
    return p


def run(live=False, confirmed=False):
    if not API_KEY:
        raise RuntimeError("Set RIT_API_KEY from the Windows RIT client API panel")
    if live and not confirmed:
        raise RuntimeError("Live mode requires --confirmed")
    api = API(live)
    seen, last_check, last_log = set(), {}, {}
    heartbeat = 0.0
    recovery = False
    closing = False
    final_check = None
    has_started = False
    stop_requested = False
    waiting_log = -100.0
    log(
        "start",
        version="14-passive-limit-unwind",
        live=live,
        base_url=BASE_URL,
        position_mode="mirrored",
        no_new_tenders_tick=NO_NEW_TENDERS_TICK,
        flatten_tick=FLATTEN_TICK,
        urgent_tick=URGENT_TICK,
        flat_target_tick=FLAT_TARGET_TICK,
    )
    log(
        "strategy_policy",
        independent_entries=False,
        reason="Case fine evidence: trade only to reduce existing tender inventory",
    )
    while True:
        try:
            case = api.get("/case")
            tick = int(case["tick"])
            if case["status"] != "ACTIVE":
                if not has_started:
                    now = time.monotonic()
                    if now - waiting_log >= 5:
                        log(
                            "waiting_for_case",
                            status=case["status"],
                            message="Ready. Trading starts automatically when RIT becomes ACTIVE.",
                        )
                        waiting_log = now
                    time.sleep(POLL_SECS)
                    continue
                log(
                    "case_inactive",
                    status=case["status"],
                    last_active_flat_check=final_check,
                )
                return
            if not has_started:
                has_started = True
                log("case_started", tick=tick, live=live)
            p = api.positions()
            orders = api.get("/orders", {"status": "OPEN"})
            thor_orders = [o for o in orders if o.get("ticker") in MARKETS]
            tenders = api.get("/tenders") if tick < NO_NEW_TENDERS_TICK else []
            cache = getattr(api, "tender_cache", {})
            cache.update({int(t["tender_id"]): dict(t) for t in tenders})
            api.tender_cache = cache
            if live and (
                recovery or tick >= FLATTEN_TICK or any(p.values()) or thor_orders
            ):
                if any(p.values()) and not getattr(api, "hedge_plan", None):
                    matched = matching_manual_tender(api, position_value(p), tick)
                    if matched:
                        begin_hedge_plan(
                            api,
                            matched,
                            api.books(),
                            tick,
                            source="matched_recent_manual_tender",
                        )
                    else:
                        log(
                            "unattributed_inventory",
                            tick=tick,
                            positions=p,
                            message="No unique tender price available; use depth-aware exits without claiming a profit floor",
                        )
                if not closing:
                    log(
                        "liquidation_mode",
                        tick=tick,
                        positions=p,
                        reason=(
                            "recovery" if recovery else "deadline or existing inventory"
                        ),
                    )
                    closing = True
                recovery = True
                final_p = recovery_step(api)
                final_check = dict(
                    tick=int(api.get("/case")["tick"]), positions=final_p
                )
                fresh_tick = int(api.get("/case")["tick"])
                if (
                    stop_requested
                    and not any(final_p.values())
                    and not getattr(api, "pending_order", None)
                    and not getattr(api, "pending_tender", None)
                ):
                    log("shutdown_flat", positions=final_p)
                    return
                if (
                    not stop_requested
                    and fresh_tick < NO_NEW_TENDERS_TICK
                    and not any(final_p.values())
                    and not getattr(api, "pending_order", None)
                    and not getattr(api, "pending_tender", None)
                    and not any(
                        o.get("ticker") in MARKETS
                        for o in api.get("/orders", {"status": "OPEN"})
                    )
                ):
                    recovery = False
                    closing = False
                    api.hedge_plan = None
                    log(
                        "recovery_rearmed",
                        tick=fresh_tick,
                        positions=final_p,
                        message="Flat and reconciled; tender evaluation resumes",
                    )
                time.sleep(0.2)
                continue
            # Keep learning while flat, including the gaps between tenders.
            if (
                time.monotonic() - getattr(api, "last_book_observation", -100)
                >= BOOK_SAMPLE_SECONDS
            ):
                api.books()
                api.last_book_observation = time.monotonic()
            if time.monotonic() - heartbeat > 5:
                heartbeat = time.monotonic()
                log("heartbeat", tick=tick, positions=p)
            if tick >= NO_NEW_TENDERS_TICK:
                time.sleep(POLL_SECS)
                continue
            for t in tenders:
                tid = int(t["tender_id"])
                now = time.monotonic()
                if tid in seen or now - last_check.get(tid, -100) < RECHECK_SECS:
                    continue
                last_check[tid] = now
                before = api.positions()
                books = api.books()
                fresh_case = api.get("/case")
                tick = int(fresh_case["tick"])
                if fresh_case["status"] != "ACTIVE":
                    return
                speed = max(
                    api.child_seconds,
                    6 * api.latency,
                    CHILD_SIZE * getattr(api, "hedge_seconds_per_share", 0),
                )
                estimator = getattr(api, "liquidity", None)
                profile = (estimator or LiquidityEstimator()).profile(
                    t.get("action") == "SELL",
                    execution_budget(int(t["quantity"]), speed),
                )
                observations = [
                    (ts, rate)
                    for ts, rate in getattr(api, "hedge_observations", [])
                    if time.monotonic() - ts <= 120
                ]
                if observations:
                    horizon = min(8, max(0, FLAT_TARGET_TICK - tick))
                    profile["recent_execution_capacity"] = int(
                        min(rate for _, rate in observations) * horizon * 0.5
                    )
                ok, reason, edge = should_accept(t, books, before, tick, speed, profile)
                _, estimate_details = (
                    estimate_unwind(t, books, profile)
                    if t.get("price") is not None
                    and t.get("action") in ("BUY", "SELL")
                    and int(t["quantity"]) > 0
                    else (None, {})
                )
                profitable_and_eligible = ok
                ready, remaining = submission_ready(t, tick, api.latency)
                if ok and not ready:
                    ok = False
                    reason = "submission blocked: tender too close to expiry"
                signature = (ok, reason.split("$")[0])
                previous = last_log.get(tid, (None, None, -100))
                if signature != previous[:2] or now - previous[2] > 5:
                    log(
                        "decision",
                        tick=tick,
                        tender=t,
                        accept=ok,
                        reason=reason,
                        estimated_edge=edge,
                        displayed_fraction=profile["depth_fraction"],
                        liquidity_estimate=estimate_details,
                        book=book_summary(books),
                        execution_budget=execution_budget(int(t["quantity"]), speed),
                        tender_seconds_remaining=remaining,
                        profitable_and_eligible=profitable_and_eligible,
                        usable_hedge_depth=sum(
                            level[1]
                            for level in aggregate(
                                books,
                                t.get("action") == "SELL",
                                profile["depth_fraction"],
                            )
                        ),
                    )
                    last_log[tid] = (*signature, now)
                if not live or not ok:
                    continue
                seen.add(tid)
                change = int(t["quantity"]) * (1 if t["action"] == "BUY" else -1)
                begin_hedge_plan(
                    api, t, books, tick, profile=profile, estimated_edge=edge
                )
                api.pending_tender = dict(tender_id=tid, before=before, change=change)
                try:
                    api.request("POST", f"/tenders/{tid}")
                except requests.HTTPError as exc:
                    if exc.response is not None and exc.response.status_code in (
                        400,
                        401,
                        403,
                        404,
                        409,
                        429,
                    ):
                        api.pending_tender = None
                        api.hedge_plan = None
                        if exc.response.status_code == 429:
                            seen.discard(tid)
                    raise
                log(
                    "tender_accepted",
                    tender_id=tid,
                    tick=tick,
                    action=t["action"],
                    quantity=int(t["quantity"]),
                    price=float(t["price"]),
                    source="bot",
                )
                after = wait_positions(api, shifted(before, change))
                api.pending_tender = None
                flat = cover(api, before, after)
                final_check = dict(tick=int(api.get("/case")["tick"]), positions=flat)
            time.sleep(POLL_SECS)
        except (Exception, KeyboardInterrupt) as exc:
            if not has_started:
                if isinstance(exc, KeyboardInterrupt):
                    log("waiting_cancelled")
                    return
                if time.monotonic() - waiting_log >= 5:
                    log("waiting_for_connection", reason=str(exc))
                    waiting_log = time.monotonic()
                time.sleep(0.5)
                continue
            if not live:
                raise
            if isinstance(exc, KeyboardInterrupt):
                stop_requested = True
            recovery = True
            log(
                "recovery",
                reason=str(exc),
                action="Reconcile uncertain writes, manage price-aware exits, resume only after verified flat",
            )
            # Stay alive while the case is active; transient read errors must not
            # abandon an accepted tender. Never blindly repeat an uncertain write.
            time.sleep(0.5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirmed", action="store_true")
    args = parser.parse_args()
    try:
        run(args.live, args.confirmed)
    except (Exception, KeyboardInterrupt) as exc:
        log(
            "STOP",
            reason=str(exc),
            action="Inspect positions and open orders in RIT before restarting; no automatic retry of writes.",
        )
        raise
