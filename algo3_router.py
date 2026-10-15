"""ALGO3 educational router, extended from ALGO3_Skeleton.ipynb.
Read OPERATIONS.md before live use. Dry-run is the default.
"""

import argparse
import json
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
PASSIVE_SECONDS = 3.0
RECHECK_SECS = 1.0
POLL_SECS = 0.2
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
        if method == "POST" and path == "/orders":
            self.pending_order = dict(
                order_id=None,
                before=before,
                action=params["action"],
                quantity=int(params["quantity"]),
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
        + PASSIVE_SECONDS
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
        )


def estimate_unwind(t, books, profile):
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
        order = api.get(f"/orders/{oid}")
        if order["status"] == "OPEN":
            cancel_order(api, oid)
        deadline = time.monotonic() + 3
        while order["status"] not in ("TRANSACTED", "CANCELLED"):
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Order {oid} not terminal; inspect before restarting"
                )
            time.sleep(0.1)
            order = api.get(f"/orders/{oid}")
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
        detail = known_detail if known_detail is not None else api.get(f"/orders/{oid}")
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
            order = api.get(f"/orders/{oid}")
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


def cover(api, before, after, liquidating=False):
    """Close shared underlying inventory, counting mirrored fields only once."""
    target = position_value(before)
    p = dict(after)
    initial = abs(position_value(p) - target)
    passive_tried = False
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
        force = liquidating or tick >= FLATTEN_TICK
        levels = aggregate(books, buying)
        if not levels and not urgent:
            time.sleep(POLL_SECS)
            continue
        ticker = levels[0][2] if levels else ALT
        rest = 0.1
        if urgent:
            # Completion takes priority: market children, never above case max.
            price = None
            qty = min(abs(delta), MAX_ORDER)
        else:
            price, available, ticker = levels[0]
            qty = min(abs(delta), available, CHILD_SIZE, MAX_ORDER)
        if (
            not force
            and not passive_tried
            and abs(delta) <= initial * 0.4
            and FLATTEN_TICK - tick > PASSIVE_SECONDS + 5
        ):
            passive_tried = True
            same_side = aggregate({ALT: books[ALT]}, not buying)
            if same_side:
                ticker, price = ALT, same_side[0][0]
                qty, rest = min(abs(delta), CHILD_SIZE), PASSIVE_SECONDS
                log(
                    "passive_tail",
                    ticker=ticker,
                    quantity=qty,
                    price=price,
                    seconds=rest,
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
    waiting_log = -100.0
    log(
        "start",
        version="12-pending-order-reservation",
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
            if live and (
                recovery or tick >= FLATTEN_TICK or any(p.values()) or thor_orders
            ):
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
                # Once close-out begins, take no more tenders this round.
                recovery = True
                final_p = recovery_step(api)
                final_check = dict(
                    tick=int(api.get("/case")["tick"]), positions=final_p
                )
                time.sleep(0.5)
                continue
            if time.monotonic() - heartbeat > 5:
                heartbeat = time.monotonic()
                log("heartbeat", tick=tick, positions=p)
            if tick >= NO_NEW_TENDERS_TICK:
                time.sleep(POLL_SECS)
                continue
            for t in api.get("/tenders"):
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
                api.request("POST", f"/tenders/{tid}")
                log("tender_accepted", tender_id=tid)
                change = int(t["quantity"]) * (1 if t["action"] == "BUY" else -1)
                after = wait_positions(api, shifted(before, change))
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
            recovery = True
            log(
                "recovery",
                reason=str(exc),
                action="Cancel open THOR orders, reconcile, flatten; no new tenders",
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
