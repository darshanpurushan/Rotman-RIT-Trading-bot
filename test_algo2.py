import json
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

import requests
from algo2_market_maker import (
    Bot,
    Config,
    Uncertain,
    VisibilityPending,
    capacity,
    quotes,
)


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
    return {
        "bids": [order(100001, price=10)],
        "asks": [order(100002, "SELL", price=10.05)],
    }


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

    def test_unchanged_reconciliation_does_not_replace_state(self):
        with patch("algo2_market_maker.os.replace") as replace:
            self.bot.reconcile()
            self.bot.reconcile()
        replace.assert_not_called()

    def test_changed_state_is_persisted(self):
        self.bot.unknown = True
        self.bot.save()
        data = json.loads(self.bot.path.read_text())
        self.assertTrue(data["unknown"])
        self.bot.unknown = False
        self.bot.save()
        self.assertFalse(json.loads(self.bot.path.read_text())["unknown"])

    def test_failed_state_replace_is_retried(self):
        self.bot.unknown = True
        previous = self.bot._saved_state
        with patch(
            "algo2_market_maker.os.replace", side_effect=OSError("disk failure")
        ):
            with self.assertRaises(OSError):
                self.bot.save()
        self.assertEqual(self.bot._saved_state, previous)
        self.bot.save()
        self.assertTrue(json.loads(self.bot.path.read_text())["unknown"])

    def test_removed_state_file_is_recreated(self):
        self.bot.path.unlink()
        self.bot.save()
        self.assertTrue(self.bot.path.exists())

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
        self.bot.submit("BUY", 4000, 10)
        with patch(
            "algo2_market_maker.time.monotonic",
            return_value=self.bot.records["1"]["created"] + 2,
        ):
            with self.assertRaises(VisibilityPending):
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

    def test_publication_lag_keeps_both_quote_reservations(self):
        clock = [100.0]
        deletes = []
        original = self.api.call
        published = {}

        def call(method, path, **kw):
            if method == "POST":
                result = original(method, path, **kw)
                published[result["order_id"]] = clock[0] + 0.3
                return result
            if method == "DELETE":
                deletes.append(path)
            if method == "GET" and path == "/orders":
                return [
                    o.copy()
                    for oid, o in self.api.rows.items()
                    if o["status"] == kw["status"] and clock[0] >= published[oid]
                ]
            if method == "GET" and path.startswith("/orders/"):
                oid = int(path.rsplit("/", 1)[1])
                if clock[0] < published[oid]:
                    response = requests.Response()
                    response.status_code = 404
                    raise requests.HTTPError(response=response)
            return original(method, path, **kw)

        self.api.call = call
        self.api.orders = lambda _: [
            o.copy()
            for oid, o in self.api.rows.items()
            if o["status"] == "OPEN" and clock[0] >= published[oid]
        ]
        with patch("algo2_market_maker.time.monotonic", side_effect=lambda: clock[0]):
            self.bot.step(50)
            self.assertEqual(len(self.api.posts), 2)
            self.assertEqual({o["action"] for o in self.api.posts}, {"BUY", "SELL"})
            position, opened = self.bot.reconcile()
            self.assertEqual(capacity(position, opened, 20000), (16000, 16000))
            clock[0] += 0.1
            self.bot.step(50)
            clock[0] += 0.3
            self.bot.step(51)
        self.assertFalse(deletes)
        self.assertEqual(len(self.api.posts), 2)
        self.assertFalse(self.bot.closing)

    def test_old_competitive_quotes_keep_queue_position(self):
        self.bot.step(50)
        with patch(
            "algo2_market_maker.time.monotonic",
            return_value=max(r["created"] for r in self.bot.records.values()) + 10,
        ):
            self.bot.step(60)
        self.assertEqual(len(self.api.posts), 2)
        self.assertTrue(all(o["status"] == "OPEN" for o in self.api.rows.values()))

    def test_repricing_cancels_only_changed_side(self):
        self.bot.step(50)
        old_sell = next(
            o["order_id"] for o in self.api.rows.values() if o["action"] == "SELL"
        )
        newbook = book()
        newbook["bids"][0]["price"] = 10.03
        self.api.book = lambda _: newbook
        with patch(
            "algo2_market_maker.time.monotonic",
            return_value=max(r["created"] for r in self.bot.records.values()) + 2,
        ):
            self.bot.step(52)
        self.assertEqual(self.api.rows[old_sell]["status"], "OPEN")
        self.assertIn(str(old_sell), self.bot.records)
        self.assertEqual(len(self.bot.records), 1)

    def test_read_timeout_does_not_stop_market_making(self):
        active = {"status": "ACTIVE", "tick": 50}
        later = {"status": "ACTIVE", "tick": 51}
        stopped = {"status": "STOPPED", "tick": 300}
        self.bot.started = True
        with (
            patch.object(
                self.api,
                "case",
                side_effect=[active, requests.Timeout("read timeout"), later, stopped],
            ),
            patch.object(self.bot, "step") as step,
            patch("algo2_market_maker.signal.signal"),
            patch("algo2_market_maker.time.sleep"),
        ):
            self.bot.run()
        self.assertEqual([c.args[0] for c in step.call_args_list], [50, 51])
        self.assertFalse(self.bot.closing)

    def test_ambiguous_post_timeout_still_blocks_orders(self):
        original = self.api.call

        def call(method, path, **kw):
            if method == "POST":
                raise requests.Timeout("lost POST response")
            return original(method, path, **kw)

        self.api.call = call
        with self.assertRaises(requests.Timeout):
            self.bot.submit("BUY", 4000, 10)
        self.assertTrue(self.bot.unknown)
        with self.assertRaises(Uncertain):
            self.bot.submit("SELL", 4000, 10.04)

    def test_visibility_wait_does_not_cancel_orders(self):
        self.bot.submit("BUY", 4000, 10)
        with patch.object(self.bot, "cancel") as cancel:
            self.bot.wait_for_visibility(VisibilityPending("delayed snapshot"))
        cancel.assert_not_called()
        self.assertIn("1", self.bot.records)

    def test_cancel_retry_is_paced(self):
        self.bot.submit("BUY", 4000, 10)
        self.bot.records["1"]["seen"] = True
        self.bot.records["1"]["created"] = 0
        original = self.api.call
        deletes = []

        def call(method, path, **kw):
            if method == "DELETE":
                deletes.append(path)
                return {}
            return original(method, path, **kw)

        self.api.call = call
        with patch("algo2_market_maker.time.monotonic", return_value=100):
            for _ in range(2):
                with self.assertRaises(VisibilityPending):
                    self.bot.cancel()
        self.assertEqual(len(deletes), 1)

    def test_fill_is_logged_once(self):
        self.bot.submit("BUY", 4000, 10)
        self.api.rows[1]["quantity_filled"] = 1000
        self.api.position = 1000
        with patch.object(self.bot, "log") as log:
            self.bot.reconcile()
            self.bot.reconcile()
        self.assertEqual(sum(c.args[0] == "fill" for c in log.call_args_list), 1)

    def test_security_fee_configuration_overrides_defaults(self):
        self.api.security = lambda _: {
            "position": 0,
            "max_trade_size": 5000,
            "fee": 0.02,
            "limit_order_rebate": 0.025,
        }
        self.bot.initialize()
        self.assertEqual(self.bot.c.fee, 0.02)
        self.assertEqual(self.bot.c.rebate, 0.025)

    def test_no_rebate_quotes_cover_passive_fees(self):
        q = quotes(book(), 0, [], Config(fee=0.01, rebate=0), 50)
        self.assertGreater(q["SELL"][0] - q["BUY"][0], 0.02)

    def test_slow_book_read_cannot_cross_entry_deadline(self):
        with patch.object(
            self.api,
            "case",
            side_effect=[
                {"status": "ACTIVE", "tick": 259},
                {"status": "ACTIVE", "tick": 260},
            ],
        ):
            self.bot.submit("BUY", 4000, 10)
        self.assertFalse(self.api.posts)
        self.assertFalse(self.bot.unknown)

    def test_no_post_after_tick_300(self):
        self.bot.expected = self.api.position = 100
        with patch.object(
            self.api,
            "case",
            side_effect=[
                {"status": "ACTIVE", "tick": 299},
                {"status": "ACTIVE", "tick": 300},
            ],
        ):
            self.bot.submit("SELL", 100)
        self.assertFalse(self.api.posts)

    def test_closeout_uses_exact_residual_and_ends_flat(self):
        self.bot.expected = self.api.position = 10000
        self.api.tick = 280
        for residual in (6000, 2000, 0):
            self.bot.step(280)
            oid = max(self.api.rows)
            self.api.rows[oid].update(
                status="TRANSACTED", quantity_filled=self.api.rows[oid]["quantity"]
            )
            self.api.position = residual
        self.bot.step(281)
        self.assertEqual([o["quantity"] for o in self.api.posts], [4000, 4000, 2000])
        self.assertEqual(self.bot.expected, 0)
        self.assertFalse(self.bot.records)
        self.assertFalse(self.bot.unknown)

    def test_cancellation_wait_keeps_working_order_reserved(self):
        self.bot.submit("BUY", 4000, 10)
        self.bot.records["1"]["seen"] = True
        original = self.api.call

        def call(method, path, **kw):
            if method == "DELETE":
                return {}
            return original(method, path, **kw)

        self.api.call = call
        with self.assertRaises(VisibilityPending):
            self.bot.cancel(["1"])
        self.bot.submit("BUY", 4000, 10)
        self.assertEqual(len(self.api.posts), 1)
        self.assertIn("1", self.bot.records)

    def test_terminal_fill(self):
        self.bot.submit("BUY", 4000, 10)
        self.api.rows[1].update(quantity_filled=4000, status="TRANSACTED")
        self.api.position = 4000
        self.bot.reconcile()
        self.assertEqual(self.bot.expected, 4000)
        self.assertFalse(self.bot.records)


if __name__ == "__main__":
    unittest.main()
