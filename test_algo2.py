import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

import requests
from algo2_market_maker import Bot, Config, Uncertain, capacity, quotes


def order(oid=1, action="BUY", quantity=4000, filled=0, status="OPEN", price=10):
    return dict(
        order_id=oid,
        ticker="ALGO",
        action=action,
        quantity=quantity,
        quantity_filled=filled,
        status=status,
        price=price,
    )


def book():
    return {"bids": [order(price=10)], "asks": [order(2, "SELL", price=10.05)]}


class Fake:
    def __init__(self):
        self.position = 0
        self.rows = {}
        self.posts = []
        self.fail = False
        self.tick = 50

    def security(self, t):
        return {"position": self.position, "max_trade_size": 5000}

    def orders(self, t):
        return [o for o in self.rows.values() if o["status"] == "OPEN"]

    def book(self, t):
        return book()

    def case(self):
        return {"status": "ACTIVE", "tick": self.tick}

    def call(self, method, path, **kw):
        if method == "GET":
            return self.rows[int(path.split("/")[-1])].copy()
        if method == "DELETE":
            self.rows[int(path.split("/")[-1])]["status"] = "CANCELLED"
            return {}
        self.posts.append(kw)
        if self.fail:
            raise RuntimeError("lost response")
        oid = len(self.rows) + 1
        self.rows[oid] = order(
            oid, kw["action"], kw["quantity"], price=kw.get("price", 10)
        )
        return self.rows[oid]


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.api = Fake()
        self.bot = Bot(self.api, Config(), True, root / "state.json", root / "log")
        self.bot.initialize()

    def test_small_configured_size_is_respected_in_quotes(self):
        for size in (1, 100, 499, 500, 4000):
            for position in (-11900, 0, 11900):
                target = quotes(book(), position, [], Config(size=size), 50)
                self.assertTrue(target)
                self.assertTrue(all(0 < qty <= size for _, qty in target.values()))

    def test_history_ignores_fully_filled_levels(self):
        b = book()
        b["bids"].append(order(3, price=30, filled=4000))
        b["asks"].append(order(4, "SELL", price=1, filled=4000))
        with patch("algo2_market_maker.time.monotonic", return_value=10):
            self.bot.observe_book(b)
        self.assertAlmostEqual(self.bot.history[-1], 10.025)

    def test_history_skips_locked_or_empty_book(self):
        self.bot.observe_book({"bids": [], "asks": []})
        b = book()
        b["asks"][0]["price"] = 10
        self.bot.observe_book(b)
        self.assertFalse(self.bot.history)

    def test_dry_run_updates_history_without_orders(self):
        self.bot.live = False
        with patch("algo2_market_maker.time.monotonic", return_value=10):
            self.bot.step(50)
        self.assertAlmostEqual(self.bot.history[-1], 10.025)
        self.assertFalse(self.api.posts)

    def test_capacity_partial(self):
        self.assertEqual(
            capacity(10000, [order(quantity=5000, filled=2000)], 20000), (7000, 30000)
        )

    def test_both_sides_dont_net(self):
        self.assertEqual(
            capacity(0, [order(), order(2, "SELL")], 20000), (16000, 16000)
        )

    def test_quotes_passive(self):
        for pos in range(-20000, 20001, 1000):
            q = quotes(book(), pos, deque(), Config(), 50)
            if "BUY" in q:
                self.assertLess(q["BUY"][0], 10.05)
            if "SELL" in q:
                self.assertGreater(q["SELL"][0], 10)
            for side, (_, size) in q.items():
                self.assertLessEqual(
                    abs(pos + (size if side == "BUY" else -size)), 20000
                )

    def test_inventory_skew(self):
        a = quotes(book(), 0, [], Config(), 50)
        b = quotes(book(), 8000, [], Config(), 50)
        self.assertLess(b["BUY"][1], a["BUY"][1])
        self.assertLessEqual(b["SELL"][0], a["SELL"][0])

    def test_soft_limit(self):
        self.assertNotIn("BUY", quotes(book(), 12000, [], Config(), 50))

    def test_late_flat_no_entry(self):
        self.assertEqual(quotes(book(), 0, [], Config(), 260), {})

    def test_late_reduces_only(self):
        q = quotes(book(), 100, [], Config(), 260)
        self.assertEqual(set(q), {"SELL"})
        self.assertEqual(q["SELL"][1], 100)

    def test_empty_book(self):
        self.assertEqual(quotes({"bids": [], "asks": []}, 0, [], Config(), 50), {})

    def test_partial_fill_reconciled_once(self):
        self.bot.submit("BUY", 4000, 10)
        self.api.rows[1]["quantity_filled"] = 1500
        self.api.position = 1500
        self.bot.reconcile()
        self.bot.reconcile()
        self.assertEqual(self.bot.expected, 1500)
        self.bot.cancel()
        self.assertEqual(self.bot.records, {})

    def test_lag_blocks(self):
        self.bot.submit("BUY", 4000, 10)
        self.api.rows[1]["quantity_filled"] = 1000
        with self.assertRaises(Uncertain):
            self.bot.submit("BUY", 4000, 10)
        self.assertEqual(len(self.api.posts), 1)

    def test_lost_response_persists(self):
        self.api.fail = True
        with self.assertRaises(RuntimeError):
            self.bot.submit("BUY", 4000, 10)
        self.assertTrue(self.bot.unknown)
        with self.assertRaises(Uncertain):
            self.bot.submit("BUY", 4000, 10)
        other = Bot(self.api, Config(), True, self.bot.path, self.bot.logfile)
        with self.assertRaises(Uncertain):
            other.initialize()
        self.assertEqual(len(self.api.posts), 1)

    def test_no_crossing_limit(self):
        self.bot.submit("BUY", 4000, 10.05)
        self.assertFalse(self.api.posts)

    def test_market_cannot_open(self):
        self.bot.submit("BUY", 4000)
        self.assertFalse(self.api.posts)

    def test_closeout_small_remainder(self):
        self.api.position = 100
        self.bot.expected = 100
        self.api.tick = 280
        self.bot.step(280)
        self.assertEqual(self.api.posts[-1]["quantity"], 100)
        self.assertEqual(self.api.posts[-1]["action"], "SELL")

    def test_pending_closeout_not_duplicated(self):
        self.api.position = 100
        self.bot.expected = 100
        self.api.tick = 280
        self.bot.submit("SELL", 100)
        self.bot.submit("SELL", 100)
        self.assertEqual(len(self.api.posts), 1)

    def test_external_order_blocks(self):
        self.api.rows[55] = order(55)
        with self.assertRaises(Uncertain):
            self.bot.submit("BUY", 4000, 10)

    def test_dry_run_no_writes(self):
        self.bot.live = False
        self.bot.step(50)
        self.assertFalse(self.api.posts)

    def test_404_open_list_fallback(self):
        self.bot.submit("BUY", 4000, 10)
        original = self.api.call

        def call(method, path, **kw):
            if method == "GET" and path == "/orders/1":
                r = requests.Response()
                r.status_code = 404
                raise requests.HTTPError(response=r)
            if method == "GET" and path == "/orders":
                return [
                    o for o in self.api.rows.values() if o["status"] == kw["status"]
                ]
            return original(method, path, **kw)

        self.api.call = call
        self.bot.reconcile()
        self.assertEqual(len(self.api.posts), 1)
        self.assertFalse(self.bot.closing)
        self.bot.cancel()
        self.assertFalse(self.bot.records)

    def test_404_terminal_list_reconciles_fill(self):
        self.bot.submit("BUY", 4000, 10)
        self.api.rows[1].update(status="TRANSACTED", quantity_filled=4000)
        self.api.position = 4000

        def call(method, path, **kw):
            if path == "/orders/1":
                r = requests.Response()
                r.status_code = 404
                raise requests.HTTPError(response=r)
            return [o for o in self.api.rows.values() if o["status"] == kw["status"]]

        self.api.call = call
        self.bot.reconcile()
        self.assertEqual(self.bot.expected, 4000)
        self.assertFalse(self.bot.records)

    def test_missing_everywhere_keeps_reservation(self):
        self.bot.submit("BUY", 4000, 10)

        def call(method, path, **kw):
            if path == "/orders":
                return []
            r = requests.Response()
            r.status_code = 404
            raise requests.HTTPError(response=r)

        self.api.call = call
        with self.assertRaises(Uncertain):
            self.bot.submit("BUY", 4000, 10)
        self.assertIn("1", self.bot.records)
        self.assertEqual(len(self.api.posts), 1)

    def test_exact_reported_sequence_does_not_exit(self):
        self.bot.submit("BUY", 4000, 10)
        original = self.api.call

        def call(method, path, **kw):
            if method == "GET" and path == "/orders/1":
                r = requests.Response()
                r.status_code = 404
                raise requests.HTTPError(response=r)
            if method == "GET" and path == "/orders":
                return [
                    o.copy()
                    for o in self.api.rows.values()
                    if o["status"] == kw["status"]
                ]
            return original(method, path, **kw)

        self.api.call = call
        self.bot.step(51)
        self.assertFalse(self.bot.closing)
        self.assertTrue(any(o["action"] == "SELL" for o in self.api.posts))

    def test_terminal_fill(self):
        self.bot.submit("BUY", 4000, 10)
        self.api.rows[1].update(quantity_filled=4000, status="TRANSACTED")
        self.api.position = 4000
        self.bot.reconcile()
        self.assertEqual(self.bot.expected, 4000)
        self.assertFalse(self.bot.records)


if __name__ == "__main__":
    unittest.main()
