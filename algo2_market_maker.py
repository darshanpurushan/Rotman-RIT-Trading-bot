"""ALGO2 market maker v3.8. Python 3.10+, pip install requests.
Run: python algo2_market_maker.py --live --confirmed
RIT_API_KEY must contain your RIT client API key. Dry run is read-only.
See ALGO2_README.md for strategy, recovery limits and case assumptions.
"""

import argparse
import json
import math
import os
import signal
import statistics
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import requests

TERMINAL = {"TRANSACTED", "CANCELLED"}


class Uncertain(RuntimeError):
    pass


class RoundResetBlocked(Uncertain):
    pass


class VisibilityPending(Uncertain):
    pass


class CancellationPending(VisibilityPending):
    def __init__(self, snapshot):
        super().__init__("Cancellation awaiting terminal confirmation")
        self.snapshot = snapshot


class RateLimited(RuntimeError):
    pass


@dataclass
class Config:
    ticker: str = "ALGO"
    limit: int = 20000  # 5,000-share buffer below the case limit
    size: int = 4000
    soft: int = 12000
    stop_entry: int = 290
    flatten: int = 294
    end: int = 300
    interval: float = 0.15
    rate: float = 5.0
    repeat: bool = True
    taper_start: int = 240
    recovery_cooldown: float = 3.0
    visibility_grace: float = 1.5
    missing_timeout: float = 8.0
    min_quote_life: float = 0.75
    cancel_retry: float = 1.0
    adaptive_queue: bool = False
    aged_exit: bool = False
    two_levels: bool = False
    rebate: float = 0.015
    fee: float = 0.01


def remaining(o):
    return max(0, int(o["quantity"]) - int(o["quantity_filled"]))


def capacity(position, orders, limit):
    """Worst case independently: all buys fill OR all sells fill."""
    buys = sum(remaining(o) for o in orders if o["action"] == "BUY")
    sells = sum(remaining(o) for o in orders if o["action"] == "SELL")
    return max(0, limit - position - buys), max(0, limit + position - sells)


def best_prices(book):
    bids = [float(o["price"]) for o in book["bids"] if remaining(o)]
    asks = [float(o["price"]) for o in book["asks"] if remaining(o)]
    if not bids or not asks:
        return None
    bid, ask = max(bids), min(asks)
    return (bid, ask) if 0 < bid < ask else None


def quotes(book, position, history, cfg, tick):
    prices = best_prices(book)
    if prices is None:
        return {}
    bid, ask = prices
    mid = (bid + ask) / 2
    values = list(history)
    changes = [b - a for a, b in zip(values, values[1:])]
    vol = statistics.pstdev(changes) if len(changes) >= 3 else 0
    trend = (values[-1] - values[0]) if len(values) > 1 else 0
    skew = max(-0.05, min(0.05, position / cfg.soft * 0.03))
    half = max(0.005, cfg.fee - cfg.rebate + 0.005, min(0.05, 1.5 * vol))
    # On a three-cent book, improving both sides gives away two thirds
    # of the spread. Spend at most one tick there, on the reducing side.
    spread_ticks = round((ask - bid) * 100)
    buy_inside = spread_ticks >= 4 or (spread_ticks == 3 and position < 0)
    sell_inside = spread_ticks >= 4 or (spread_ticks == 3 and position > 0)
    buy = min(bid + 0.01 * buy_inside, mid - half - skew, ask - 0.01)
    sell = max(ask - 0.01 * sell_inside, mid + half - skew, bid + 0.01)
    if trend < -0.04:
        buy -= min(0.04, abs(trend) / 2)
    if trend > 0.04:
        sell += min(0.04, trend / 2)
    buy = math.floor((buy + 1e-8) * 100) / 100
    sell = math.ceil((sell - 1e-8) * 100) / 100
    if buy <= 0 or sell <= buy:
        return {}
    # Shrink the inventory target toward the end, without giving up quoting
    # for the last forty seconds of every round.
    fraction = max(
        0.0,
        min(1.0, (cfg.stop_entry - tick) / max(1, cfg.stop_entry - cfg.taper_start)),
    )
    soft = max(min(500, cfg.size), int(cfg.soft * fraction))
    size = max(min(500, cfg.size), int(cfg.size * fraction))
    buycap, sellcap = capacity(position, [], min(cfg.limit, soft))
    floor = min(500, cfg.size)
    bq = min(buycap, size, max(floor, int(size * (1 - max(0, position) / soft))))
    sq = min(
        sellcap,
        size,
        max(floor, int(size * (1 - max(0, -position) / soft))),
    )
    if position >= soft:
        bq = 0
    if position <= -soft:
        sq = 0
    if tick >= max(cfg.taper_start, cfg.stop_entry - 15):
        if position < 0:
            bq = min(bq, -position)
        elif position > 0:
            sq = min(sq, position)
    if tick >= cfg.stop_entry:
        bq = min(bq, -position) if position < 0 else 0
        sq = min(sq, position) if position > 0 else 0
    return {
        side: (price, qty)
        for side, price, qty in [("BUY", buy, bq), ("SELL", sell, sq)]
        if qty > 0
    }


def displayed_depth(book, action, price, own_ids=()):
    rows = book["bids" if action == "BUY" else "asks"]
    return sum(
        remaining(o)
        for o in rows
        if str(o.get("order_id")) not in own_ids
        and (
            float(o["price"]) >= price
            if action == "BUY"
            else float(o["price"]) <= price
        )
    )


def queue_adjusted(target, book, position=0, aged_exit=False):
    prices = best_prices(book)
    if set(target) != {"BUY", "SELL"} or prices is None:
        return target
    bid, ask = prices
    spread_ticks = round((ask - bid) * 100)
    buy_depth = displayed_depth(book, "BUY", bid)
    sell_depth = displayed_depth(book, "SELL", ask)
    if position:
        # A small odd lot can persist for most of a round. Only improve
        # the reducing side, and never buy or sell through flat.
        if spread_ticks not in (2, 3) or (abs(position) > 500 and not aged_exit):
            return target
        side = "SELL" if position > 0 else "BUY"
        depth = sell_depth if position > 0 else buy_depth
        if depth < 2 * target[side][1]:
            return target
        price = round(ask - 0.01 if position > 0 else bid + 0.01, 2)
        reducing_touch = ask if position > 0 else bid
        if target[side][0] not in (reducing_touch, price):
            return target  # Preserve volatility/trend protection.
        if aged_exit and target[side][0] != reducing_touch:
            return target  # The reducing side already improved.
        other = target["BUY" if position > 0 else "SELL"][0]
        if (
            round((price - other if position > 0 else other - price) * 100)
            < spread_ticks - 1
        ):
            return target
        return dict(target, **{side: (price, min(abs(position), target[side][1]))})
    if spread_ticks != 3 or (target["BUY"][0], target["SELL"][0]) != prices:
        return target
    for side, depth, opposite, price in (
        ("BUY", buy_depth, sell_depth, round(bid + 0.01, 2)),
        ("SELL", sell_depth, buy_depth, round(ask - 0.01, 2)),
    ):
        qty = target[side][1]
        if depth >= 2 * qty and depth >= 1.5 * opposite:
            return dict(target, **{side: (price, qty)})
    return target


class API:
    def __init__(self, url, key, rate):
        self.url = url.rstrip("/")
        self.s = requests.Session()
        self.s.headers["X-API-Key"] = key
        self.gap = 1 / min(rate, 5)
        self.latency = 0.05
        self.next_write = 0

    def call(self, method, path, **params):
        if method != "GET":
            time.sleep(max(0, self.next_write - time.monotonic()))
            self.next_write = time.monotonic() + self.gap
        began = time.monotonic()
        r = self.s.request(method, self.url + path, params=params, timeout=(1, 2))
        elapsed = time.monotonic() - began
        self.latency = max(elapsed, self.latency * 0.9 + elapsed * 0.1)
        if r.status_code == 429:
            # Never blindly retry a POST; caller retains its reservation.
            self.gap = min(2, self.gap * 1.5)
            time.sleep(min(2, max(0.2, self.gap)))
        r.raise_for_status()
        return r.json() if r.content else {}

    def case(self):
        return self.call("GET", "/case")

    def security(self, ticker):
        rows = self.call("GET", "/securities", ticker=ticker)
        return next(s for s in rows if s["ticker"] == ticker)

    def orders(self, ticker):
        return [
            o
            for o in self.call("GET", "/orders", status="OPEN")
            if o["ticker"] == ticker
        ]

    def book(self, ticker):
        return self.call("GET", "/securities/book", ticker=ticker, limit=50)


class Bot:
    def __init__(self, api, cfg, live, state, logfile):
        self.api, self.c, self.live = api, cfg, live
        self.path, self.logfile = Path(state), Path(logfile)
        self.records = {}
        self.expected = 0
        self.unknown = False
        self.history = deque(maxlen=25)
        self.last_history = 0
        self.closing = False
        self.started = False
        self.last_status = 0
        self.ladder_blocks = {}
        self._saved_state = None
        self.last_wait = 0.0
        self.last_wait_message = None
        self.stop_requested = False
        self.close_reason = None
        self.last_tick = None
        self.inactive_logged = False
        self.reset_seen = False
        self.round_number = 0
        self.resume_at = 0.0
        self.last_fill = time.monotonic()
        self.markouts = deque(maxlen=512)

    def log(self, event, **kw):
        line = json.dumps(dict(time=time.time(), event=event, **kw))
        print(line, flush=True)
        with self.logfile.open("a", encoding="utf8") as f:
            f.write(line + "\n")

    def save(self):
        if not self.live:
            return
        snapshot = json.dumps(
            dict(
                expected=self.expected,
                records=self.records,
                unknown=self.unknown,
                ticker=self.c.ticker,
            ),
            sort_keys=True,
        )
        if snapshot == self._saved_state and self.path.exists():
            return
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf8") as f:
            f.write(snapshot)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        self._saved_state = snapshot

    def timing(self, position, opened=()):
        buys = sum(remaining(o) for o in opened if o["action"] == "BUY")
        sells = sum(remaining(o) for o in opened if o["action"] == "SELL")
        exposure = max(abs(position + buys), abs(position - sells))
        children = math.ceil(exposure / max(1, self.c.size))
        child_time = max(
            1.5,
            10 * getattr(self.api, "latency", 0.05),
            3 * getattr(self.api, "gap", 0.2),
        )
        budget = max(3.0, children * child_time * 1.5 + 2.0)
        return min(self.c.stop_entry, self.c.end - budget - 3), min(
            self.c.flatten, self.c.end - budget
        )

    def request_stop(self, *_):
        self.stop_requested = True
        self.closing = True
        self.close_reason = "user_interrupt"

    def check_saved_state(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
            except (ValueError, OSError) as exc:
                raise Uncertain(
                    "State file is unreadable; verify prior orders and inventory before archiving it"
                ) from exc
            if (
                not isinstance(data, dict)
                or data.get("unknown")
                or data.get("records")
                or data.get("expected")
            ):
                raise Uncertain(
                    "Unresolved previous session in state file. Reconcile in RIT before restarting; do not delete this file to bypass an outstanding order."
                )

    def preflight(self):
        case = self.api.case()
        position = int(self.api.security(self.c.ticker)["position"])
        orders = self.api.orders(self.c.ticker)
        reason = None
        try:
            self.check_saved_state()
        except Uncertain as exc:
            reason = str(exc)
        clear = position == 0 and not orders and reason is None
        self.log(
            "preflight_clear" if clear else "preflight_blocked",
            status=case["status"],
            tick=case["tick"],
            position=position,
            open_orders=len(orders),
            reason=reason,
        )
        return clear

    def initialize(self):
        sec = self.api.security(self.c.ticker)
        self.c.size = min(self.c.size, int(sec.get("max_trade_size", 5000)), 5000)
        if self.c.size <= 0:
            raise RuntimeError("Invalid server order maximum")
        self.expected = int(sec["position"])
        server_rate = sec.get("api_orders_per_second")
        if server_rate and hasattr(self.api, "gap"):
            self.api.gap = max(self.api.gap, 1 / min(5, float(server_rate)))
        if sec.get("fee") is not None:
            self.c.fee = float(sec["fee"])
        if sec.get("limit_order_rebate") is not None:
            self.c.rebate = float(sec["limit_order_rebate"])
        if self.live:
            self.check_saved_state()
        if self.api.orders(self.c.ticker):
            raise Uncertain(
                "Existing ALGO orders: cancel and verify them before starting this bot."
            )
        self.history.clear()
        self.markouts.clear()
        self.ladder_blocks.clear()
        self.last_fill = time.monotonic()
        self.last_history = 0
        self.round_number += 1
        self.save()
        self.log(
            "initialized",
            position=self.expected,
            tick=self.last_tick,
            round=self.round_number,
            config=vars(self.c),
        )

    def order_status(self, oid, rec):
        if rec["status"] in TERMINAL:
            return dict(rec, quantity_filled=rec["filled"])
        try:
            result = self.api.call("GET", "/orders/" + str(oid))
        except requests.HTTPError as e:
            if e.response is None or e.response.status_code != 404:
                raise
            result = None
            # Some RIT builds expose a newly submitted/finished order in lists
            # before the single-order endpoint. Absence never proves a fill.
            for status in ("OPEN", "TRANSACTED", "CANCELLED"):
                rows = self.api.call("GET", "/orders", status=status)
                result = next(
                    (
                        o
                        for o in rows
                        if str(o.get("order_id")) == str(oid)
                        and o.get("ticker") == self.c.ticker
                    ),
                    None,
                )
                if result is not None:
                    break
            if result is None:
                since = rec.setdefault("missing_since", time.monotonic())
                self.save()
                if time.monotonic() - since > self.c.missing_timeout:
                    raise Uncertain(
                        f"Order {oid} still missing from status and lists; replacement blocked"
                    )
                if time.monotonic() - rec["created"] <= self.c.visibility_grace:
                    return dict(
                        rec,
                        order_id=oid,
                        ticker=self.c.ticker,
                        quantity_filled=rec["filled"],
                        provisional=True,
                    )
                raise VisibilityPending(
                    f"Order {oid} awaiting visibility; reservation retained"
                )
        rec.pop("missing_since", None)
        rec["seen"] = True
        if (
            result.get("action") != rec["action"]
            or int(result["quantity"]) != rec["quantity"]
        ):
            raise Uncertain("Order record does not match submitted reservation")
        return result

    def reconcile(self):
        if self.unknown:
            raise Uncertain("Unknown POST outcome: further submissions blocked")
        for oid, rec in list(self.records.items()):
            o = self.order_status(oid, rec)
            if o.get("status") not in TERMINAL | {"OPEN"}:
                raise Uncertain("Unknown order status")
            filled = int(o["quantity_filled"])
            if not rec["filled"] <= filled <= rec["quantity"]:
                raise Uncertain("Invalid cumulative fill")
            delta = filled - rec["filled"]
            self.expected += delta * (1 if rec["action"] == "BUY" else -1)
            previous_status = rec["status"]
            rec["filled"] = filled
            rec["status"] = o["status"]
            self.save()
            if delta:
                self.last_fill = time.monotonic()
                elapsed = self.last_fill - rec["created"]
                if rec["price"] is not None:
                    for horizon in (1, 5):
                        self.markouts.append(
                            (
                                self.last_fill,
                                horizon,
                                oid,
                                rec["action"],
                                delta,
                                rec["price"],
                            )
                        )
                self.log(
                    "fill",
                    order_id=oid,
                    action=rec["action"],
                    quantity=delta,
                    cumulative=filled,
                    position=self.expected,
                    limit_price=rec["price"],
                    level=rec.get("level", 0),
                    seconds_to_observed_fill=round(elapsed, 3),
                    displayed_depth_at_submit=rec.get("displayed_depth_at_submit"),
                )
            if rec["status"] in TERMINAL and previous_status not in TERMINAL:
                self.log(
                    "order_terminal", order_id=oid, status=rec["status"], filled=filled
                )
        actual = int(self.api.security(self.c.ticker)["position"])
        if actual != self.expected:
            raise VisibilityPending(
                f"Position not synchronized: expected {self.expected}, observed {actual}; submissions blocked"
            )
        opened = self.api.orders(self.c.ticker)
        if any(str(o["order_id"]) not in self.records for o in opened):
            raise Uncertain(
                "External order detected; do not trade manually alongside this bot"
            )
        # A terminal order must also disappear from the open-order snapshot.
        openids = {str(o["order_id"]) for o in opened}
        for oid in list(self.records):
            if self.records[oid]["status"] in TERMINAL and oid not in openids:
                del self.records[oid]
        self.save()
        # Unpublished known submissions still consume their full unfilled capacity.
        # A later observed fill/position mismatch blocks writes until reconciled.
        opened.extend(
            dict(rec, order_id=oid, ticker=self.c.ticker, quantity_filled=rec["filled"])
            for oid, rec in self.records.items()
            if rec["status"] == "OPEN" and oid not in openids
        )
        return actual, opened

    def cancel(self, order_ids=None):
        selected = set(self.records) if order_ids is None else set(order_ids)
        now = time.monotonic()
        for oid, rec in list(self.records.items()):
            if (
                oid not in selected
                or rec["status"] in TERMINAL
                or rec["filled"] == rec["quantity"]
            ):
                continue
            last_cancel = rec.get("cancel_requested")
            if last_cancel is not None and now - last_cancel < self.c.cancel_retry:
                continue
            # The order may have filled since reconciliation. Confirm that it
            # still exists before spending a write on its cancellation.
            detail = self.order_status(oid, rec)
            if detail.get("provisional") or detail["status"] in TERMINAL:
                continue
            # Let a newly submitted flattening order settle before attempting
            # to cancel a residual. Its entire unfilled quantity stays reserved.
            if rec["price"] is None and now - rec["created"] < self.c.visibility_grace:
                continue
            current_case = self.api.case()
            current_tick = int(current_case["tick"])
            if (
                current_case["status"] != "ACTIVE"
                or current_tick >= self.c.end
                or (self.last_tick is not None and current_tick < self.last_tick)
            ):
                raise VisibilityPending(
                    "Cancellation deferred at case boundary; reservation retained"
                )
            rec["cancel_requested"] = now
            self.save()
            try:
                self.api.call("DELETE", "/orders/" + oid)
            except requests.HTTPError as e:
                if e.response is None or e.response.status_code != 404:
                    raise
                self.log(
                    "cancel_pending", order_id=oid, error="Awaiting order visibility"
                )
        position, opened = self.reconcile()
        if any(oid in self.records for oid in selected):
            raise CancellationPending((position, opened))
        return position, opened

    def submit(self, action, quantity, price=None, reduce_only=False, level=None):
        if not self.live:
            raise RuntimeError("Dry-run write blocked")

        def blocked(reason):
            if level is not None:
                self.ladder_blocks[action, level] = reason

        position, opened = self.reconcile()
        same_side = [o for o in opened if o["action"] == action]
        if any(
            rec["action"] == action and rec.get("cancel_requested") is not None
            for rec in self.records.values()
        ):
            return blocked("cancel_pending")
        if level is None:
            if same_side:
                return blocked("same_side_open")
        elif (
            not self.c.two_levels
            or self.closing
            or price is None
            or level not in (0, 1)
        ):
            return blocked("ladder_disabled")
        elif len(same_side) >= 2 or any(
            self.records[str(o["order_id"])].get("level", 0) == level
            or o["price"] is None
            or float(o["price"]) == price
            for o in same_side
        ):
            return blocked("duplicate_level_or_price")
        if level == 1 and not any(
            self.records[str(o["order_id"])].get("level", 0) == 0
            and remaining(o) > 0
            and (
                price < float(o["price"])
                if action == "BUY"
                else price > float(o["price"])
            )
            for o in same_side
        ):
            return blocked("awaiting_working_primary")
        if level == 0 and any(
            (
                price <= float(o["price"])
                if action == "BUY"
                else price >= float(o["price"])
            )
            for o in same_side
        ):
            return blocked("outer_quote_in_way")
        reserved_side = sum(remaining(o) for o in same_side)
        case = self.api.case()
        case_tick = int(case["tick"])
        if (
            case["status"] != "ACTIVE"
            or case_tick >= self.c.end
            or (self.last_tick is not None and case_tick < self.last_tick)
        ):
            return blocked("case_boundary")
        entry_tick, flatten_tick = self.timing(position, opened)
        qty = min(int(quantity), self.c.size - reserved_side, 5000)
        caps = capacity(
            position,
            opened,
            min(self.c.limit, self.c.soft) if level is not None else self.c.limit,
        )
        qty = min(qty, caps[0 if action == "BUY" else 1])
        if (
            self.closing
            or reduce_only
            or int(case["tick"]) >= entry_tick
            or price is None
        ):
            if (action == "BUY" and position >= 0) or (
                action == "SELL" and position <= 0
            ):
                return blocked("reducing_only")
            reserved = sum(remaining(o) for o in opened if o["action"] == action)
            qty = min(qty, max(0, abs(position) - reserved))
        if qty <= 0:
            return blocked("no_capacity")
        quote_depth = None
        if price is not None:
            book = self.api.book(self.c.ticker)
            quote_depth = displayed_depth(book, action, price, self.records)
            opposite = book["asks"] if action == "BUY" else book["bids"]
            prices = [float(o["price"]) for o in opposite if remaining(o)]
            if not prices:
                return blocked("no_opposite_book")
            touch = min(prices) if action == "BUY" else max(prices)
            if (action == "BUY" and price >= touch) or (
                action == "SELL" and price <= touch
            ):
                return blocked("would_cross_market")
            if any(
                o["action"] != action
                and (
                    o["price"] is None
                    or (action == "BUY" and price >= float(o["price"]))
                    or (action == "SELL" and price <= float(o["price"]))
                )
                for o in opened
            ):
                return blocked("would_cross_own_order")
        # Book/status calls may cross a closeout boundary; recheck before writing.
        latest = self.api.case()
        if (
            latest["status"] != "ACTIVE"
            or not case_tick <= int(latest["tick"]) < self.c.end
        ):
            return blocked("case_boundary")
        if level is not None and int(latest["tick"]) >= min(
            entry_tick, self.c.taper_start
        ):
            return blocked("outside_ladder_window")
        if price is not None and int(latest["tick"]) >= flatten_tick:
            return blocked("closeout")
        if self.closing or int(latest["tick"]) >= entry_tick:
            if (action == "BUY" and position >= 0) or (
                action == "SELL" and position <= 0
            ):
                return blocked("reducing_only")
            reserved = sum(remaining(o) for o in opened if o["action"] == action)
            qty = min(qty, max(0, abs(position) - reserved))
        if int(latest["tick"]) >= max(self.c.taper_start, self.c.stop_entry - 15):
            if (action == "SELL" and position > 0) or (
                action == "BUY" and position < 0
            ):
                qty = min(qty, abs(position))
        if qty <= 0:
            return blocked("no_capacity")
        if price is not None:
            hypothetical = list(opened) + [
                dict(action=action, quantity=qty, quantity_filled=0)
            ]
            candidate_entry, _ = self.timing(position, hypothetical)
            increasing = (
                (action == "BUY" and position >= 0)
                or (action == "SELL" and position <= 0)
                or qty + reserved_side > abs(position)
            )
            if increasing and int(latest["tick"]) >= candidate_entry:
                return blocked("insufficient_closeout_time")
        params = dict(
            ticker=self.c.ticker,
            action=action,
            quantity=qty,
            type="MARKET" if price is None else "LIMIT",
        )
        if price is not None:
            params["price"] = price
        # Persist BEFORE network transmission, including the unknown-response case.
        self.unknown = True
        self.save()
        try:
            result = self.api.call("POST", "/orders", **params)
        except requests.HTTPError as e:
            # An explicit rejection is distinct from a lost/ambiguous response.
            if e.response is not None and e.response.status_code in (
                400,
                401,
                403,
                429,
            ):
                self.unknown = False
                self.save()
                if e.response.status_code == 429:
                    raise RateLimited(
                        "Submission rejected by rate limit; pacing reduced"
                    ) from e
            raise
        if "order_id" not in result:
            raise Uncertain("POST missing order id")
        oid = str(result["order_id"])
        self.records[oid] = dict(
            action=action,
            quantity=qty,
            filled=0,
            status="OPEN",
            price=price,
            created=time.monotonic(),
            displayed_depth_at_submit=quote_depth,
            level=level if level is not None else 0,
        )
        self.unknown = False
        self.save()
        self.log(
            "order",
            order_id=oid,
            displayed_depth_at_submit=quote_depth,
            level=level,
            **params,
        )

    def observe_book(self, book):
        prices = best_prices(book)
        now = time.monotonic()
        if prices is not None and now - self.last_history >= 0.2:
            self.history.append(sum(prices) / 2)
            self.last_history = now
            mid = sum(prices) / 2
            for _ in range(len(self.markouts)):
                observed, horizon, oid, action, qty, limit = self.markouts.popleft()
                elapsed = now - observed
                if elapsed < horizon:
                    self.markouts.append((observed, horizon, oid, action, qty, limit))
                elif elapsed <= horizon + 1:
                    self.log(
                        "limit_markout",
                        order_id=oid,
                        action=action,
                        quantity=qty,
                        horizon_seconds=horizon,
                        observed_seconds=round(elapsed, 3),
                        limit_price=limit,
                        midpoint=mid,
                        signed_mid_minus_limit=round(
                            (mid - limit) * (1 if action == "BUY" else -1), 6
                        ),
                    )

    def maintain_ladder(self, target, opened, position, tick, entry_tick, book=None):
        active = tick < min(entry_tick, self.c.taper_start)
        self.ladder_blocks.clear()
        if (
            active
            and position == 0
            and book is not None
            and time.monotonic() - self.last_fill >= 5
        ):
            target = queue_adjusted(target, book)
        desired = {}
        for side, (price, qty) in target.items():
            second_price = round(price + (-0.01 if side == "BUY" else 0.01), 2)
            second = qty // 4 if active and qty >= 1000 and second_price > 0 else 0
            desired[side, 0] = (price, qty - second)
            if second:
                desired[side, 1] = (second_price, second)
        current = {}
        now = time.monotonic()
        blocked_sides = set()
        cancel_id = None
        side_open = {
            side: sum(remaining(o) for o in opened if o["action"] == side)
            for side in ("BUY", "SELL")
        }
        for order in opened:
            oid = str(order["order_id"])
            rec = self.records[oid]
            side = order["action"]
            key = side, rec.get("level", 0)
            current[key] = order
            wanted = desired.get(key)
            if rec.get("cancel_requested") is not None:
                blocked_sides.add(side)
                continue
            excess = (
                wanted is not None
                and side_open[side] > target[side][1]
                and remaining(order) > wanted[1]
            )
            old_price = order["price"]
            reprice = old_price is None or (
                wanted is not None
                and abs(float(old_price) - wanted[0]) >= 0.009
                and now - rec["created"]
                >= max(self.c.min_quote_life, 1.5 if key[1] else 0)
            )
            if wanted is None or excess or reprice:
                blocked_sides.add(side)
                if cancel_id is None:
                    cancel_id = oid
        if cancel_id is not None:
            try:
                self.cancel([cancel_id])
            except CancellationPending as exc:
                self.wait_for_visibility(exc)
        sides = ("SELL", "BUY") if position > 0 else ("BUY", "SELL")
        for level in (0, 1):
            for side in sides:
                key = side, level
                if key not in desired or key in current:
                    continue
                if side in blocked_sides:
                    self.ladder_blocks[key] = "same_side_repricing"
                    continue
                price, qty = desired[key]
                self.submit(side, qty, price, level=level if active else None)
        if now - self.last_status >= 3:
            self.log(
                "status",
                tick=tick,
                position=self.expected,
                quotes=target,
                levels=[
                    dict(side=side, level=level, price=quote[0], quantity=quote[1])
                    for (side, level), quote in desired.items()
                ],
                tracked_levels=[
                    dict(
                        side=r["action"],
                        level=r.get("level", 0),
                        price=r["price"],
                        remaining=r["quantity"] - r["filled"],
                        cancelling=r.get("cancel_requested") is not None,
                    )
                    for r in self.records.values()
                    if r["status"] == "OPEN"
                ],
                level_blocks=[
                    dict(side=side, level=level, reason=reason)
                    for (side, level), reason in self.ladder_blocks.items()
                ],
                working_orders=len(self.records),
                round=self.round_number,
            )
            self.last_status = now

    def step(self, tick):
        if not self.live:
            p = int(self.api.security(self.c.ticker)["position"])
            b = self.api.book(self.c.ticker)
            self.observe_book(b)
            self.log(
                "dry_run",
                tick=tick,
                position=p,
                quotes=quotes(b, p, self.history, self.c, tick),
            )
            return
        p, opened = self.reconcile()
        entry_tick, flatten_tick = self.timing(p, opened)
        if self.closing or tick >= flatten_tick:
            p, _ = self.cancel()
            if p:
                self.submit("SELL" if p > 0 else "BUY", abs(p))
            return
        cancelling = [
            oid
            for oid, r in self.records.items()
            if r.get("cancel_requested") is not None
        ]
        if cancelling:
            try:
                p, opened = self.cancel(cancelling)
            except CancellationPending as exc:
                self.wait_for_visibility(exc)
                p, opened = exc.snapshot
            entry_tick, flatten_tick = self.timing(p, opened)
            if tick >= flatten_tick:
                return
        book = self.api.book(self.c.ticker)
        for side in ("bids", "asks"):
            book[side] = [
                o for o in book[side] if str(o.get("order_id")) not in self.records
            ]
        self.observe_book(book)
        target = quotes(book, p, self.history, self.c, tick)
        queue_side = None
        fill_gap = time.monotonic() - self.last_fill
        aged_exit = self.c.aged_exit and 500 < abs(p) <= self.c.size and fill_gap >= 10
        residual_queue = self.c.adaptive_queue and abs(p) <= 500 and fill_gap >= 5
        if (aged_exit or residual_queue) and tick < min(entry_tick, self.c.taper_start):
            adjusted = queue_adjusted(target, book, p, aged_exit=aged_exit)
            queue_side = next(
                (side for side in adjusted if adjusted[side] != target[side]), None
            )
            target = adjusted
        if tick >= entry_tick:
            target = {
                side: (price, min(qty, abs(p)))
                for side, (price, qty) in target.items()
                if (side == "SELL" and p > 0) or (side == "BUY" and p < 0)
            }
        if self.c.two_levels:
            self.maintain_ladder(target, opened, p, tick, entry_tick, book)
            return
        current = {o["action"]: o for o in opened}
        now = time.monotonic()
        repricing = set()
        cancel_id = None
        for side, order in current.items():
            oid = str(order["order_id"])
            rec = self.records[oid]
            if rec.get("cancel_requested") is not None:
                continue
            wanted = target.get(side)
            # Exposure reduction can cancel promptly; ordinary repricing waits.
            # Size targets vary with every fill. Do not lose queue priority just
            # to trim a small amount, unless closeout makes it necessary.
            remove = wanted is None or (
                (
                    tick >= entry_tick
                    or (
                        tick >= max(self.c.taper_start, self.c.stop_entry - 15)
                        and ((side == "BUY" and p < 0) or (side == "SELL" and p > 0))
                    )
                )
                and remaining(order) > abs(p)
            )
            if wanted is not None and remaining(order) > max(
                wanted[1] * 1.5, wanted[1] + 500
            ):
                remove = True
            if p and side == queue_side and remaining(order) > abs(p):
                remove = True
            moved = (
                wanted is not None and abs(float(order["price"]) - wanted[0]) >= 0.019
            )
            prices = best_prices(book)
            behind = prices is not None and (
                (side == "BUY" and float(order["price"]) < prices[0] - 0.009)
                or (side == "SELL" and float(order["price"]) > prices[1] + 0.009)
            )
            improve = wanted is not None and (
                (side == "BUY" and wanted[0] >= float(order["price"]) + 0.009)
                or (side == "SELL" and wanted[0] <= float(order["price"]) - 0.009)
            )
            stale = (
                (behind or side == queue_side)
                and improve
                and now - rec["created"] >= max(1.5, self.c.min_quote_life)
            )
            adverse = wanted is not None and (
                (side == "BUY" and wanted[0] <= float(order["price"]) - 0.009)
                or (side == "SELL" and wanted[0] >= float(order["price"]) + 0.009)
            )
            if (
                remove
                or stale
                or (
                    (moved or adverse) and now - rec["created"] >= self.c.min_quote_life
                )
            ):
                if stale and side == queue_side:
                    self.log(
                        "queue_reprice",
                        tick=tick,
                        action=side,
                        order_id=oid,
                        previous_price=float(order["price"]),
                        target_price=wanted[0],
                    )
                repricing.add(side)
                if cancel_id is None:
                    cancel_id = oid
        if cancel_id is not None:
            try:
                new_position, _ = self.cancel([cancel_id])
            except CancellationPending as exc:
                self.wait_for_visibility(exc)
                new_position, _ = exc.snapshot
            if new_position != p:
                return  # Rebuild both targets after fills during cancellation.
        sides = ["SELL", "BUY"] if p > 0 else ["BUY", "SELL"]
        for side in sides:
            if side in target and side not in current and side not in repricing:
                if p and side == queue_side:
                    self.submit(
                        side, target[side][1], target[side][0], reduce_only=True
                    )
                else:
                    self.submit(side, target[side][1], target[side][0])
        if time.monotonic() - self.last_status >= 3:
            self.log(
                "status",
                tick=tick,
                position=self.expected,
                quotes=target,
                queue_side=queue_side,
                aged_exit=aged_exit and queue_side is not None,
                external_bbo=best_prices(book),
                working_orders=len(self.records),
                passive_pair_rebate=2 * (self.c.rebate - self.c.fee),
                stop_entry_tick=entry_tick,
                flatten_tick=flatten_tick,
                round=self.round_number,
            )
            self.last_status = time.monotonic()

    def wait_for_visibility(self, error):
        now = time.monotonic()
        message = str(error)
        if message != self.last_wait_message or now - self.last_wait >= 2:
            if isinstance(error, CancellationPending):
                self.log("cancel_wait", detail=message)
            else:
                self.log("visibility_wait", error=message)
            self.last_wait = now
            self.last_wait_message = message
        # During normal publication lag, retain the quote and its reservation.
        # Closing/late-case cancellation is retried by step(), without replaying POST.

    def run(self):
        self.log(
            "start",
            version="3.8-primary-refill",
            live=self.live,
            repeat=self.c.repeat,
        )
        signal.signal(signal.SIGINT, self.request_stop)
        while True:
            try:
                if self.stop_requested and self.unknown:
                    self.log(
                        "shutdown_unresolved", reason="user_interrupt", unknown=True
                    )
                    return
                case = self.api.case()
                tick = int(case["tick"])
                if case["status"] != "ACTIVE" or tick >= self.c.end:
                    if (
                        self.started
                        and self.last_tick is not None
                        and tick < self.last_tick
                    ):
                        self.reset_seen = True
                    if not self.inactive_logged:
                        self.log(
                            "waiting_for_case",
                            status=case["status"],
                            tick=tick,
                            expected_position=self.expected,
                            unknown=self.unknown,
                        )
                        self.inactive_logged = True
                    if self.stop_requested:
                        self.log(
                            "shutdown",
                            reason="user_interrupt_case_inactive",
                            expected_position=self.expected,
                            unresolved_orders=len(self.records),
                            unknown=self.unknown,
                        )
                        return
                    # Read-only reconciliation preserves terminal evidence before
                    # the simulator clears its order history for the next round.
                    if self.started and self.live and not self.reset_seen:
                        self.reconcile()
                    if self.stop_requested or (self.started and not self.c.repeat):
                        self.log(
                            "shutdown",
                            reason=(
                                "user_interrupt"
                                if self.stop_requested
                                else "case_inactive"
                            ),
                            expected_position=self.expected,
                            unresolved_orders=len(self.records),
                            unknown=self.unknown,
                        )
                        return
                    time.sleep(0.25)
                    continue
                self.inactive_logged = False
                if (
                    self.started
                    and self.last_tick is not None
                    and (self.reset_seen or tick < self.last_tick)
                ):
                    if self.unknown or self.records or self.expected:
                        raise RoundResetBlocked(
                            "Round reset with unresolved state; verify inventory/orders before restart"
                        )
                    # Both snapshots must confirm a clean account. Never infer
                    # the outcome of an ambiguous POST from a simulator reset.
                    for _ in range(2):
                        if int(
                            self.api.security(self.c.ticker)["position"]
                        ) != 0 or self.api.orders(self.c.ticker):
                            raise RoundResetBlocked("New round is not flat and clear")
                    if not self.c.repeat:
                        return
                    self.reset_seen = False
                    self.started = False
                    self.closing = self.stop_requested
                    self.close_reason = (
                        "user_interrupt" if self.stop_requested else None
                    )
                    self.resume_at = 0
                    self.log("round_reset", previous_tick=self.last_tick, tick=tick)
                self.last_tick = tick
                if self.stop_requested and not self.started:
                    return
                if not self.started:
                    self.initialize()
                    self.started = True
                if time.monotonic() < self.resume_at:
                    time.sleep(0.1)
                    continue
                self.step(tick)
                if (
                    self.closing
                    and not self.expected
                    and not self.records
                    and not self.unknown
                ):
                    self.log(
                        "shutdown_flat" if self.stop_requested else "recovery_flat",
                        reason=self.close_reason,
                        tick=tick,
                    )
                    if self.stop_requested:
                        return
                    self.closing = False
                    self.close_reason = None
                    self.resume_at = time.monotonic() + self.c.recovery_cooldown
                    self.history.clear()
                    self.log(
                        "resume_after_recovery",
                        tick=tick,
                        delay=self.c.recovery_cooldown,
                    )
            except RoundResetBlocked as e:
                self.log(
                    "shutdown_unresolved",
                    reason=str(e),
                    expected_position=self.expected,
                    unresolved_orders=len(self.records),
                    unknown=self.unknown,
                )
                return
            except CancellationPending as e:
                self.wait_for_visibility(e)
            except VisibilityPending as e:
                self.wait_for_visibility(e)
                time.sleep(0.2)
            except requests.RequestException as e:
                self.log("api_wait", error=str(e), unknown_submission=self.unknown)
                if self.unknown:
                    self.closing = True
                    self.close_reason = "ambiguous_submission"
                time.sleep(0.4)
            except RateLimited as e:
                self.log("rate_limit", error=str(e))
            except (RuntimeError, ValueError, KeyError, StopIteration) as e:
                self.log("error", error=str(e), tick=self.last_tick)
                if not self.started:
                    # Do not spin forever or trade around an unresolved state file.
                    return
                if self.live:
                    self.closing = True
                    self.close_reason = str(e)
                    try:
                        self.cancel()
                    except Exception as ce:
                        self.log("recovery_wait", error=str(ce))
                time.sleep(0.4)
            time.sleep(self.c.interval if self.live else 1)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--live", action="store_true")
    p.add_argument(
        "--preflight",
        action="store_true",
        help="Check RIT and saved state once without sending orders",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Exit after one case instead of waiting for the next round",
    )
    p.add_argument(
        "--adaptive-queue",
        action="store_true",
        help="Practice: improve one side of a heavily queued three-tick spread after a fill drought",
    )
    p.add_argument("--stop-entry", type=int, default=290)
    p.add_argument(
        "--aged-exit",
        action="store_true",
        help="Practice: improve the inventory-reducing quote after a 10-second fill gap",
    )
    p.add_argument(
        "--two-levels",
        action="store_true",
        help="Practice: split each quote across two adjacent price levels",
    )
    p.add_argument("--flatten", type=int, default=294)
    p.add_argument("--confirmed", action="store_true")
    p.add_argument("--base-url", default="http://localhost:9999/v1")
    p.add_argument("--size", type=int, default=4000)
    p.add_argument("--rate", type=float, default=5)
    p.add_argument("--log", default="algo2_events.jsonl")
    p.add_argument("--state", default="algo2_state.json")
    a = p.parse_args()
    if a.live and not a.confirmed:
        p.error("--live requires --confirmed")
    if a.two_levels and (a.adaptive_queue or a.aged_exit):
        p.error(
            "--two-levels must be tested separately from --adaptive-queue and --aged-exit"
        )
    if not 1 <= a.size <= 5000 or not 0 < a.rate <= 5:
        p.error("Invalid size or rate")
    if not 0 < a.stop_entry < a.flatten < 300:
        p.error("Require 0 < stop-entry < flatten < 300")
    key = os.environ.get("RIT_API_KEY", "")
    if not key:
        p.error("Set RIT_API_KEY to your RIT client API key")
    bot = Bot(
        API(a.base_url, key, a.rate),
        Config(
            size=a.size,
            rate=a.rate,
            repeat=not a.once,
            adaptive_queue=a.adaptive_queue,
            aged_exit=a.aged_exit,
            two_levels=a.two_levels,
            stop_entry=a.stop_entry,
            flatten=a.flatten,
        ),
        a.live,
        a.state,
        a.log,
    )
    if a.preflight:
        try:
            clear = bot.preflight()
        except (requests.RequestException, ValueError, KeyError, StopIteration) as exc:
            bot.log("preflight_blocked", reason=str(exc))
            clear = False
        raise SystemExit(0 if clear else 1)
    bot.run()


if __name__ == "__main__":
    main()
