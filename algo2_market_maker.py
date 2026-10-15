"""ALGO2 market maker v2.0. Python 3.10+, pip install requests.
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


class VisibilityPending(Uncertain):
    pass


class RateLimited(RuntimeError):
    pass


@dataclass
class Config:
    ticker: str = "ALGO"
    limit: int = 20000  # 5,000-share buffer below the case limit
    size: int = 4000
    soft: int = 12000
    stop_entry: int = 260
    flatten: int = 275
    end: int = 300
    interval: float = 0.15
    rate: float = 5.0
    visibility_grace: float = 1.5
    missing_timeout: float = 8.0
    min_quote_life: float = 0.75
    cancel_retry: float = 1.0
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
    buy = min(bid + (0.01 if ask - bid >= 0.03 else 0), mid - half - skew, ask - 0.01)
    sell = max(ask - (0.01 if ask - bid >= 0.03 else 0), mid + half - skew, bid + 0.01)
    if trend < -0.04:
        buy -= min(0.04, abs(trend) / 2)
    if trend > 0.04:
        sell += min(0.04, trend / 2)
    buy = math.floor((buy + 1e-8) * 100) / 100
    sell = math.ceil((sell - 1e-8) * 100) / 100
    if buy <= 0 or sell <= buy:
        return {}
    buycap, sellcap = capacity(position, [], cfg.limit)
    floor = min(500, cfg.size)
    bq = min(
        buycap, cfg.size, max(floor, int(cfg.size * (1 - max(0, position) / cfg.soft)))
    )
    sq = min(
        sellcap,
        cfg.size,
        max(floor, int(cfg.size * (1 - max(0, -position) / cfg.soft))),
    )
    if position >= cfg.soft:
        bq = 0
    if position <= -cfg.soft:
        sq = 0
    if tick >= cfg.stop_entry:
        bq = min(bq, -position) if position < 0 else 0
        sq = min(sq, position) if position > 0 else 0
    return {
        side: (price, qty)
        for side, price, qty in [("BUY", buy, bq), ("SELL", sell, sq)]
        if qty > 0
    }


class API:
    def __init__(self, url, key, rate):
        self.url = url.rstrip("/")
        self.s = requests.Session()
        self.s.headers["X-API-Key"] = key
        self.gap = 1 / rate
        self.next_write = 0

    def call(self, method, path, **params):
        if method != "GET":
            time.sleep(max(0, self.next_write - time.monotonic()))
            self.next_write = time.monotonic() + self.gap
        r = self.s.request(method, self.url + path, params=params, timeout=(1, 2))
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
        self._saved_state = None
        self.last_wait = 0.0
        self.last_wait_message = None

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

    def initialize(self):
        sec = self.api.security(self.c.ticker)
        self.c.size = min(self.c.size, int(sec.get("max_trade_size", 5000)), 5000)
        if self.c.size <= 0:
            raise RuntimeError("Invalid server order maximum")
        self.expected = int(sec["position"])
        if sec.get("fee") is not None:
            self.c.fee = float(sec["fee"])
        if sec.get("limit_order_rebate") is not None:
            self.c.rebate = float(sec["limit_order_rebate"])
        if self.live and self.path.exists():
            data = json.loads(self.path.read_text())
            if data.get("unknown") or data.get("records") or data.get("expected"):
                raise Uncertain(
                    "Unresolved previous session in state file. Reconcile in RIT before restarting; do not delete this file to bypass an outstanding order."
                )
        if self.api.orders(self.c.ticker):
            raise Uncertain(
                "Existing ALGO orders: cancel and verify them before starting this bot."
            )
        self.save()
        self.log("initialized", position=self.expected, config=vars(self.c))

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
        provisional = {}
        for oid, rec in list(self.records.items()):
            o = self.order_status(oid, rec)
            if o.get("provisional"):
                provisional[oid] = o
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
                self.log(
                    "fill",
                    order_id=oid,
                    action=rec["action"],
                    quantity=delta,
                    cumulative=filled,
                    position=self.expected,
                    limit_price=rec["price"],
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
        opened.extend(o for oid, o in provisional.items() if oid not in openids)
        return actual, opened

    def cancel(self, order_ids=None):
        selected = set(self.records) if order_ids is None else set(order_ids)
        now = time.monotonic()
        for oid, rec in list(self.records.items()):
            if oid not in selected or rec["status"] in TERMINAL:
                continue
            # Do not fire repeated DELETEs at an unpublished or cancelling order.
            if not rec.get("seen") and now - rec["created"] < self.c.visibility_grace:
                detail = self.order_status(oid, rec)
                if detail.get("provisional") or detail["status"] in TERMINAL:
                    continue
            last_cancel = rec.get("cancel_requested")
            if last_cancel is not None and now - last_cancel < self.c.cancel_retry:
                continue
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
            raise VisibilityPending("Cancellation awaiting terminal confirmation")
        return position, opened

    def submit(self, action, quantity, price=None):
        if not self.live:
            raise RuntimeError("Dry-run write blocked")
        position, opened = self.reconcile()
        if any(o["action"] == action for o in opened):
            return
        case = self.api.case()
        if case["status"] != "ACTIVE" or int(case["tick"]) >= self.c.end:
            return
        qty = min(int(quantity), self.c.size, 5000)
        caps = capacity(position, opened, self.c.limit)
        qty = min(qty, caps[0 if action == "BUY" else 1])
        if self.closing or int(case["tick"]) >= self.c.stop_entry or price is None:
            if (action == "BUY" and position >= 0) or (
                action == "SELL" and position <= 0
            ):
                return
            reserved = sum(remaining(o) for o in opened if o["action"] == action)
            qty = min(qty, max(0, abs(position) - reserved))
        if qty <= 0:
            return
        if price is not None:
            book = self.api.book(self.c.ticker)
            opposite = book["asks"] if action == "BUY" else book["bids"]
            prices = [float(o["price"]) for o in opposite if remaining(o)]
            if not prices:
                return
            touch = min(prices) if action == "BUY" else max(prices)
            if (action == "BUY" and price >= touch) or (
                action == "SELL" and price <= touch
            ):
                return
            if any(
                o["action"] != action
                and (
                    (action == "BUY" and price >= float(o["price"]))
                    or (action == "SELL" and price <= float(o["price"]))
                )
                for o in opened
            ):
                return
        # Book/status calls may cross a closeout boundary; recheck before writing.
        latest = self.api.case()
        if latest["status"] != "ACTIVE" or int(latest["tick"]) >= self.c.end:
            return
        if price is not None and int(latest["tick"]) >= self.c.flatten:
            return
        if self.closing or int(latest["tick"]) >= self.c.stop_entry:
            if (action == "BUY" and position >= 0) or (
                action == "SELL" and position <= 0
            ):
                return
            reserved = sum(remaining(o) for o in opened if o["action"] == action)
            qty = min(qty, max(0, abs(position) - reserved))
        if qty <= 0:
            return
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
        )
        self.unknown = False
        self.save()
        self.log("order", order_id=oid, **params)

    def observe_book(self, book):
        prices = best_prices(book)
        now = time.monotonic()
        if prices is not None and now - self.last_history >= 0.2:
            self.history.append(sum(prices) / 2)
            self.last_history = now

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
        if self.closing or tick >= self.c.flatten:
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
            self.cancel(cancelling)
            return
        book = self.api.book(self.c.ticker)
        for side in ("bids", "asks"):
            book[side] = [
                o for o in book[side] if str(o.get("order_id")) not in self.records
            ]
        self.observe_book(book)
        target = quotes(book, p, self.history, self.c, tick)
        current = {o["action"]: o for o in opened}
        now = time.monotonic()
        for side, order in current.items():
            oid = str(order["order_id"])
            rec = self.records[oid]
            wanted = target.get(side)
            # Exposure reduction can cancel promptly; ordinary repricing waits.
            remove = wanted is None or remaining(order) > wanted[1]
            moved = (
                wanted is not None and abs(float(order["price"]) - wanted[0]) >= 0.019
            )
            if remove or (moved and now - rec["created"] >= self.c.min_quote_life):
                self.cancel([oid])
                return
        sides = ["SELL", "BUY"] if p > 0 else ["BUY", "SELL"]
        for side in sides:
            if side in target and side not in current:
                self.submit(side, target[side][1], target[side][0])
        if time.monotonic() - self.last_status >= 3:
            self.log(
                "status",
                tick=tick,
                position=self.expected,
                quotes=target,
                working_orders=len(self.records),
                passive_pair_rebate=2 * (self.c.rebate - self.c.fee),
            )
            self.last_status = time.monotonic()

    def wait_for_visibility(self, error):
        now = time.monotonic()
        message = str(error)
        if message != self.last_wait_message or now - self.last_wait >= 2:
            self.log("visibility_wait", error=message)
            self.last_wait = now
            self.last_wait_message = message
        # During normal publication lag, retain the quote and its reservation.
        # Closing/late-case cancellation is retried by step(), without replaying POST.

    def run(self):
        self.log("start", version="2.0-publication-grace", live=self.live)
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "closing", True))
        while True:
            try:
                case = self.api.case()
                if case["status"] != "ACTIVE":
                    if self.started:
                        self.log(
                            "case_stopped",
                            expected_position=self.expected,
                            unresolved_orders=len(self.records),
                            unknown=self.unknown,
                        )
                        return
                    if self.closing:
                        return
                    time.sleep(0.25)
                    continue
                if not self.started:
                    self.initialize()
                    self.started = True
                self.step(int(case["tick"]))
                if self.closing and not self.expected and not self.records:
                    self.log("shutdown_flat")
                    return
            except VisibilityPending as e:
                self.wait_for_visibility(e)
                time.sleep(0.2)
            except requests.RequestException as e:
                self.log("api_wait", error=str(e), unknown_submission=self.unknown)
                if self.unknown:
                    self.closing = True
                # A read timeout is not a new trading instruction or a permanent
                # shutdown. Next iteration obtains fresh case/order/position data.
                time.sleep(0.4)
            except RateLimited as e:
                self.log("rate_limit", error=str(e))
            except (
                RuntimeError,
                ValueError,
                KeyError,
                StopIteration,
            ) as e:
                self.log("error", error=str(e))
                if not self.started:
                    if isinstance(e, Uncertain):
                        return
                elif self.live:
                    self.closing = True
                    # Unknown submission remains blocked across restarts. Known orders
                    # may still be cancelled; never infer fills solely from absence.
                    try:
                        self.cancel()
                    except Exception as ce:
                        self.log("recovery_wait", error=str(ce))
                time.sleep(0.4)
            time.sleep(self.c.interval if self.live else 1)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--live", action="store_true")
    p.add_argument("--confirmed", action="store_true")
    p.add_argument("--base-url", default="http://localhost:9999/v1")
    p.add_argument("--size", type=int, default=4000)
    p.add_argument("--rate", type=float, default=5)
    p.add_argument("--log", default="algo2_events.jsonl")
    p.add_argument("--state", default="algo2_state.json")
    a = p.parse_args()
    if a.live and not a.confirmed:
        p.error("--live requires --confirmed")
    if not 1 <= a.size <= 5000 or not 0 < a.rate <= 20:
        p.error("Invalid size or rate")
    key = os.environ.get("RIT_API_KEY", "")
    if not key:
        p.error("Set RIT_API_KEY to your RIT client API key")
    Bot(
        API(a.base_url, key, a.rate),
        Config(size=a.size, rate=a.rate),
        a.live,
        a.state,
        a.log,
    ).run()


if __name__ == "__main__":
    main()
