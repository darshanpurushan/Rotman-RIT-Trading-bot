import unittest
from unittest.mock import patch

import algo3_router as r


class StrategyTests(unittest.TestCase):
    def books(self):
        return {
            t: {
                "asks": [{"price": 20, "quantity": 100000}],
                "bids": [{"price": 19.98, "quantity": 100000}],
            }
            for t in r.MARKETS
        }

    def tender(self, action, price, qty=10000):
        return dict(
            tender_id=1, ticker=r.MAIN, action=action, price=price, quantity=qty
        )

    def test_buy_tender_sells_hedge(self):
        ok, _, profit = r.should_accept(
            self.tender("BUY", 19.9), self.books(), dict.fromkeys(r.MARKETS, 0), 10
        )
        self.assertTrue(ok)
        self.assertAlmostEqual(profit, 725)

    def test_sell_tender_buys_hedge(self):
        ok, _, profit = r.should_accept(
            self.tender("SELL", 20.1), self.books(), dict.fromkeys(r.MARKETS, 0), 10
        )
        self.assertTrue(ok)
        self.assertAlmostEqual(profit, 925)

    def test_reject_loss_and_limit(self):
        for t in [
            self.tender("BUY", 20.1),
            self.tender("SELL", 19.9),
            self.tender("BUY", 10, 100001),
        ]:
            self.assertFalse(
                r.should_accept(t, self.books(), dict.fromkeys(r.MARKETS, 0), 10)[0]
            )

    def test_nbbo_before_fee(self):
        b = self.books()
        b[r.MAIN]["asks"][0]["price"] = 19.999
        self.assertEqual(r.aggregate(b, True)[0][2], r.MAIN)

    def test_equal_price_lower_fee(self):
        self.assertEqual(r.aggregate(self.books(), True)[0][2], r.ALT)
        self.assertEqual(r.aggregate(self.books(), False)[0][2], r.ALT)

    def test_partial_book_and_depth(self):
        b = self.books()
        for t in b:
            b[t]["asks"][0]["quantity_filled"] = 99900
        self.assertEqual(r.walk(r.aggregate(b, True), 300, True)[0], 200)
        self.assertFalse(
            r.should_accept(
                self.tender("SELL", 21), b, dict.fromkeys(r.MARKETS, 0), 10
            )[0]
        )

    def test_late_case(self):
        self.assertTrue(
            r.should_accept(
                self.tender("SELL", 21), self.books(), dict.fromkeys(r.MARKETS, 0), 257
            )[0]
        )
        self.assertFalse(
            r.should_accept(
                self.tender("SELL", 21), self.books(), dict.fromkeys(r.MARKETS, 0), 295
            )[0]
        )

    def test_partial_depth_stressed_tail(self):
        b = self.books()
        for t in b:
            b[t]["asks"][0]["quantity"] = 6000
        ok, _, edge = r.should_accept(
            self.tender("SELL", 20.2), b, dict.fromkeys(r.MARKETS, 0), 10
        )
        self.assertTrue(ok)
        self.assertGreater(edge, 200)
        self.assertFalse(
            r.should_accept(
                self.tender("SELL", 20.01), b, dict.fromkeys(r.MARKETS, 0), 10
            )[0]
        )

    def test_competition_depth_floor(self):
        b = self.books()
        for t in b:
            b[t]["asks"][0]["quantity"] = 3000
        self.assertFalse(
            r.should_accept(
                self.tender("SELL", 30), b, dict.fromkeys(r.MARKETS, 0), 10
            )[0]
        )

    def test_slow_api_rejects_late_tender(self):
        self.assertFalse(
            r.should_accept(
                self.tender("SELL", 21, 71000),
                self.books(),
                dict.fromkeys(r.MARKETS, 0),
                257,
                child_seconds=3,
            )[0]
        )

    def test_passive_cancels_and_reconciles(self):
        class Fake:
            def __init__(self):
                self.cancelled = False

            def request(self, method, path, params=None):
                if method == "DELETE":
                    self.cancelled = True
                return {"order_id": 1}

            def get(self, path):
                return {
                    "status": "CANCELLED" if self.cancelled else "OPEN",
                    "quantity_filled": 50,
                }

        a = Fake()
        with patch.object(r.time, "sleep") as sleep, patch.object(r, "log"):
            self.assertEqual(r.execute_child(a, r.ALT, "BUY", 100, 20, 3), 50)
            sleep.assert_any_call(3)
        self.assertTrue(a.cancelled)

    def test_cross_venue_gross(self):
        self.assertTrue(r.within_limits({r.MAIN: 100000, r.ALT: 100000}))
        self.assertFalse(r.within_limits({r.MAIN: 100001, r.ALT: 100001}))

    def test_partial_order_cancel(self):
        class Fake:
            def __init__(self):
                self.calls = []
                self.n = 0

            def request(self, method, path, params=None):
                self.calls.append((method, path))
                return {"order_id": 1}

            def get(self, path):
                self.n += 1
                return {
                    "status": "OPEN" if self.n == 1 else "CANCELLED",
                    "quantity_filled": 123,
                }

        a = Fake()
        with patch.object(r.time, "sleep"), patch.object(r, "log"):
            self.assertEqual(r.execute_child(a, r.MAIN, "BUY", 1000, 20), 123)
        self.assertIn(("DELETE", "/orders/1"), a.calls)

    def test_ambiguous_write_not_retried(self):
        a = r.API(True)
        with (
            patch.object(a, "positions", return_value=dict.fromkeys(r.MARKETS, 100)),
            patch.object(a, "get", return_value=[]),
            patch.object(a.s, "request", side_effect=r.requests.Timeout) as req,
        ):
            with self.assertRaises(r.requests.Timeout):
                a.request(
                    "POST", "/orders", dict(ticker=r.MAIN, action="SELL", quantity=100)
                )
            self.assertEqual(req.call_count, 1)
            self.assertEqual(req.call_args.args[0], "POST")

    def test_dry_run_blocks_writes(self):
        with self.assertRaises(RuntimeError):
            r.API(False).request("POST", "/orders", {})

    def test_pending_tender_reconsidered_then_accepted_once(self):
        tender = self.tender("SELL", 19.9)
        tender["expires"] = 40

        class Fake:
            latency = 0.05
            child_seconds = 0.6

            def __init__(self, *args):
                self.samples = 0
                self.accepted = False
                self.writes = []

            def positions(self):
                return dict.fromkeys(r.MARKETS, 0)

            def get(self, path, params=None):
                if path == "/case":
                    return {
                        "status": "STOPPED" if self.accepted else "ACTIVE",
                        "tick": 10,
                    }
                if path == "/orders":
                    return []
                if path == "/tenders":
                    return [tender]
                raise AssertionError(path)

            def books(self):
                self.samples += 1
                price = 20 if self.samples == 1 else 19.6
                return {
                    t: {"asks": [{"price": price, "quantity": 100000}]}
                    for t in r.MARKETS
                }

            def request(self, method, path, params=None):
                self.writes.append((method, path))
                self.accepted = True

        api = Fake()
        with (
            patch.object(r, "API", return_value=api),
            patch.object(r, "API_KEY", "offline"),
            patch.object(r, "RECHECK_SECS", 0),
            patch.object(r.time, "sleep"),
            patch.object(
                r, "wait_positions", return_value={r.MAIN: -10000, r.ALT: -10000}
            ),
            patch.object(r, "cover") as cover,
            patch.object(r, "log"),
        ):
            r.run(live=True, confirmed=True)
        self.assertEqual(api.writes, [("POST", "/tenders/1")])
        self.assertEqual(api.samples, 2)
        cover.assert_called_once()


class MirroredExchange:
    """Offline stateful RIT fixture: either venue changes BOTH position fields."""

    latency = 0.05
    child_seconds = 0.6

    def __init__(self, action="BUY", initial=0, tick=124, partial=False, empty=False):
        self.position = initial
        self.initial = initial
        self.tick = tick
        self.action = action
        self.accepted = False
        self.orders = {}
        self.writes = []
        self.partial = partial
        self.empty = empty

    def positions(self):
        return dict.fromkeys(r.MARKETS, self.position)

    def books(self):
        return {
            t: {
                "asks": [] if self.empty else [{"price": 20, "quantity": 100000}],
                "bids": [] if self.empty else [{"price": 19.98, "quantity": 100000}],
            }
            for t in r.MARKETS
        }

    def get(self, path, params=None):
        if path == "/case":
            done = (self.accepted or self.initial != 0) and self.position == 0
            return {"status": "STOPPED" if done else "ACTIVE", "tick": self.tick}
        if path == "/orders":
            return [o for o in self.orders.values() if o["status"] == "OPEN"]
        if path.startswith("/orders/"):
            return self.orders[int(path.rsplit("/", 1)[1])]
        if path == "/tenders":
            return (
                []
                if self.accepted
                else [
                    dict(
                        tender_id=215,
                        ticker=r.MAIN,
                        action=self.action,
                        quantity=71000,
                        price=19.8 if self.action == "BUY" else 20.2,
                        expires=self.tick + 30,
                        is_fixed_bid=True,
                    )
                ]
            )
        raise AssertionError(path)

    def request(self, method, path, params=None):
        self.writes.append((method, path, params))
        if method == "POST" and path.startswith("/tenders/"):
            self.accepted = True
            self.position += 71000 if self.action == "BUY" else -71000
            return {}
        if method == "POST" and path == "/orders":
            oid = len(self.orders) + 1
            qty = params["quantity"]
            filled = max(1, qty // 2) if self.partial else qty
            self.position += filled if params["action"] == "BUY" else -filled
            self.orders[oid] = dict(
                order_id=oid,
                ticker=params["ticker"],
                quantity=qty,
                quantity_filled=filled,
                status="OPEN" if filled < qty else "TRANSACTED",
            )
            return {"order_id": oid}
        if method == "DELETE":
            self.orders[int(path.rsplit("/", 1)[1])]["status"] = "CANCELLED"
            return {}
        raise AssertionError((method, path))


class InventoryRegressionTests(unittest.TestCase):
    def run_exchange(self, api, reconcile_failure=False):
        original = r.wait_positions
        failures = [reconcile_failure]

        def wait(*args, **kwargs):
            if failures[0]:
                failures[0] = False
                raise RuntimeError("temporary reconciliation failure after tender")
            return original(*args, **kwargs)

        with (
            patch.object(r, "API", return_value=api),
            patch.object(r, "API_KEY", "offline"),
            patch.object(r.time, "sleep"),
            patch.object(r, "log") as logs,
            patch.object(r, "wait_positions", side_effect=wait),
        ):
            r.run(True, True)
        return logs

    def test_reported_71000_is_hedged_once_not_twice(self):
        a = MirroredExchange()
        self.run_exchange(a)
        orders = [p for m, path, p in a.writes if path == "/orders" and m == "POST"]
        self.assertEqual(sum(p["quantity"] for p in orders), 71000)
        self.assertTrue(all(p["action"] == "SELL" for p in orders))
        self.assertEqual(a.positions(), dict.fromkeys(r.MARKETS, 0))

    def test_short_tender_without_owned_shares(self):
        a = MirroredExchange(action="SELL")
        self.run_exchange(a)
        orders = [p for m, path, p in a.writes if path == "/orders" and m == "POST"]
        self.assertEqual(sum(p["quantity"] for p in orders), 71000)
        self.assertTrue(all(p["action"] == "BUY" for p in orders))
        self.assertEqual(a.position, 0)

    def test_partial_fills_close_both_mirrored_fields(self):
        a = MirroredExchange(partial=True)
        self.run_exchange(a)
        self.assertEqual(sum(o["quantity_filled"] for o in a.orders.values()), 71000)
        self.assertEqual(a.position, 0)
        self.assertFalse(a.get("/orders"))

    def test_urgent_liquidation_even_without_displayed_quotes(self):
        a = MirroredExchange(initial=71000, tick=280, empty=True)
        self.run_exchange(a)
        orders = [p for m, path, p in a.writes if path == "/orders" and m == "POST"]
        self.assertEqual([p["quantity"] for p in orders], [25000, 25000, 21000])
        self.assertTrue(all(p["type"] == "MARKET" for p in orders))
        self.assertFalse(a.accepted)
        self.assertEqual(a.position, 0)

    def test_urgent_short_cover(self):
        a = MirroredExchange(initial=-71000, tick=280)
        self.run_exchange(a)
        orders = [p for m, path, p in a.writes if path == "/orders" and m == "POST"]
        self.assertTrue(all(p["action"] == "BUY" for p in orders))
        self.assertEqual(a.position, 0)

    def test_urgent_book_read_failure_still_closes_inventory(self):
        a = MirroredExchange(initial=71000, tick=280)
        with patch.object(
            a, "books", side_effect=r.requests.Timeout("book unavailable")
        ):
            self.run_exchange(a)
        self.assertEqual(a.position, 0)

    def test_flat_inventory_with_resting_order_is_cancelled(self):
        class Resting(MirroredExchange):
            def get(self, path, params=None):
                if path == "/case":
                    return {
                        "status": "ACTIVE" if self.get("/orders") else "STOPPED",
                        "tick": 280,
                    }
                return super().get(path, params)

        a = Resting(tick=280)
        a.orders[1] = dict(order_id=1, ticker=r.ALT, status="OPEN", quantity_filled=0)
        self.run_exchange(a)
        self.assertFalse(a.get("/orders"))
        self.assertFalse(any(method == "POST" for method, _, _ in a.writes))

    def test_reconciliation_error_enters_recovery_and_flattens(self):
        a = MirroredExchange()
        logs = self.run_exchange(a, reconcile_failure=True)
        self.assertEqual(a.position, 0)
        self.assertTrue(any(c.args[0] == "recovery" for c in logs.call_args_list))
        self.assertEqual(sum(1 for m, p, _ in a.writes if p.startswith("/tenders/")), 1)

    def test_endgame_rejects_new_tenders(self):
        t = dict(ticker=r.MAIN, action="BUY", quantity=1000, price=19, expires=290)
        self.assertFalse(
            r.should_accept(
                t, MirroredExchange().books(), dict.fromkeys(r.MARKETS, 0), 260
            )[0]
        )

    def test_never_net_opposite_unverified_fields_to_zero(self):
        with self.assertRaises(RuntimeError):
            r.position_value({r.MAIN: 71000, r.ALT: -71000})


def missing_error():
    response = r.requests.Response()
    response.status_code = 404
    return r.requests.HTTPError("missing order detail", response=response)


class MissingDetailExchange(MirroredExchange):
    def get(self, path, params=None):
        if path.startswith("/orders/"):
            raise missing_error()
        return super().get(path, params)


class MissingOrderTests(unittest.TestCase):
    def test_missing_detail_full_fill_reconciles_without_recovery(self):
        a = MissingDetailExchange()
        logs = InventoryRegressionTests().run_exchange(a)
        self.assertEqual(a.position, 0)
        self.assertEqual(sum(o["quantity_filled"] for o in a.orders.values()), 71000)
        self.assertFalse(any(c.args[0] == "recovery" for c in logs.call_args_list))

    def test_missing_detail_partial_fill_cancels_then_reconciles(self):
        a = MissingDetailExchange(partial=True)
        logs = InventoryRegressionTests().run_exchange(a)
        self.assertEqual(a.position, 0)
        self.assertFalse(a.get("/orders"))
        self.assertEqual(sum(o["quantity_filled"] for o in a.orders.values()), 71000)
        self.assertFalse(any(c.args[0] == "recovery" for c in logs.call_args_list))

    def test_missing_detail_zero_delta_is_not_assumed_filled(self):
        a = MissingDetailExchange()
        with patch.object(r.time, "sleep"), patch.object(r, "log"):
            with self.assertRaisesRegex(RuntimeError, "Unresolved missing order"):
                r.reconcile_missing_order(a, 99, a.positions(), "SELL", 5000)
        self.assertFalse(a.writes)

    def test_missing_detail_excess_delta_is_not_accepted(self):
        a = MissingDetailExchange(initial=-6000)
        with patch.object(r.time, "sleep"), patch.object(r, "log"):
            with self.assertRaises(RuntimeError):
                r.reconcile_missing_order(
                    a, 99, dict.fromkeys(r.MARKETS, 0), "SELL", 5000
                )

    def test_missing_detail_concurrent_order_blocks_attribution(self):
        a = MissingDetailExchange(initial=-100)
        a.orders[2] = dict(order_id=2, ticker=r.MAIN, status="OPEN")
        with self.assertRaisesRegex(RuntimeError, "concurrent"):
            r.reconcile_missing_order(a, 99, dict.fromkeys(r.MARKETS, 0), "SELL", 5000)

    def test_missing_order_does_not_disable_later_tender(self):
        class TwoTenders(MissingDetailExchange):
            count = 0

            def get(self, path, params=None):
                if path == "/case":
                    if self.count == 1 and self.position == 0:
                        self.tick = 198
                    return {
                        "status": (
                            "STOPPED"
                            if self.count == 2 and self.position == 0
                            else "ACTIVE"
                        ),
                        "tick": self.tick,
                    }
                if path == "/tenders":
                    return [
                        dict(
                            tender_id=215 if self.count == 0 else 615,
                            ticker=r.MAIN,
                            action="BUY",
                            quantity=71000,
                            price=19.8,
                            expires=self.tick + 30,
                        )
                    ]
                return super().get(path, params)

            def request(self, method, path, params=None):
                if method == "POST" and path.startswith("/tenders/"):
                    self.count += 1
                return super().request(method, path, params)

        a = TwoTenders()
        logs = InventoryRegressionTests().run_exchange(a)
        self.assertEqual(a.count, 2)
        self.assertEqual(a.position, 0)
        self.assertFalse(any(c.args[0] == "recovery" for c in logs.call_args_list))

    def test_flat_logging_throttled_without_skipping_checks(self):
        a = MirroredExchange()
        with (
            patch.object(r.time, "monotonic", side_effect=[10, 11, 16]),
            patch.object(r, "log") as logs,
        ):
            for _ in range(3):
                r.log_flat(a, a.positions())
        self.assertEqual(logs.call_count, 2)


class DepthAndTimingTests(unittest.TestCase):
    def test_same_price_orders_combined_per_venue(self):
        books = {
            r.MAIN: {
                "asks": [
                    {"price": 11.23, "quantity": 100},
                    {"price": 11.23, "quantity": 4900},
                    {"price": 11.24, "quantity": 8000},
                ]
            },
            r.ALT: {"asks": [{"price": 11.23, "quantity": 2000}]},
        }
        self.assertEqual(
            r.aggregate(books, True),
            [(11.23, 2000, r.ALT), (11.23, 5000, r.MAIN), (11.24, 8000, r.MAIN)],
        )

    def test_haircut_applies_to_combined_depth(self):
        books = {r.MAIN: {"bids": [{"price": 10, "quantity": 1} for _ in range(10)]}}
        self.assertEqual(r.aggregate(books, False, 0.6), [(10, 6, r.MAIN)])

    def test_expiry_and_latency_guard(self):
        self.assertFalse(r.submission_ready({"expires": 237}, 235, 0.05)[0])
        self.assertTrue(r.submission_ready({"expires": 237}, 234, 0.05)[0])
        self.assertFalse(r.submission_ready({"expires": 237}, 234, 1.1)[0])

    def test_logged_accept_matches_expiry_block(self):
        class Expiring(MirroredExchange):
            checks = 0

            def get(self, path, params=None):
                if path == "/case":
                    self.checks += 1
                    return {
                        "status": "ACTIVE" if self.checks <= 2 else "STOPPED",
                        "tick": 124,
                    }
                out = super().get(path, params)
                if path == "/tenders":
                    out[0]["expires"] = 126
                return out

        a = Expiring()
        logs = InventoryRegressionTests().run_exchange(a)
        decisions = [c.kwargs for c in logs.call_args_list if c.args[0] == "decision"]
        self.assertFalse(decisions[0]["accept"])
        self.assertTrue(decisions[0]["profitable_and_eligible"])
        self.assertIn("expiry", decisions[0]["reason"])
        self.assertFalse(a.writes)

    def test_completed_hedge_records_full_cycle_speed(self):
        a = MirroredExchange()
        logs = InventoryRegressionTests().run_exchange(a)
        self.assertGreater(a.hedge_seconds_per_share, 0)
        summary = [
            c.kwargs for c in logs.call_args_list if c.args[0] == "hedge_summary"
        ]
        self.assertEqual(summary[0]["shares"], 71000)
        self.assertEqual(summary[0]["underlying_position"], 0)


class AutoStartTests(unittest.TestCase):
    def test_waits_before_start_then_exits_after_one_round(self):
        class Waiting(MirroredExchange):
            states = iter(["STOPPED", "STOPPED", "ACTIVE", "STOPPED"])

            def get(self, path, params=None):
                if path == "/case":
                    return {"status": next(self.states), "tick": 0}
                if path == "/tenders":
                    return []
                return super().get(path, params)

        a = Waiting()
        logs = InventoryRegressionTests().run_exchange(a)
        events = [c.args[0] for c in logs.call_args_list]
        self.assertIn("waiting_for_case", events)
        self.assertEqual(events.count("case_started"), 1)
        self.assertEqual(events[-1], "case_inactive")
        self.assertFalse(a.writes)

    def test_connection_failure_before_start_does_not_trigger_recovery(self):
        class Waiting(MirroredExchange):
            calls = 0

            def get(self, path, params=None):
                if path == "/case":
                    self.calls += 1
                    if self.calls == 1:
                        raise r.requests.ConnectionError("RIT not yet open")
                    return {
                        "status": "ACTIVE" if self.calls == 2 else "STOPPED",
                        "tick": 0,
                    }
                if path == "/tenders":
                    return []
                return super().get(path, params)

        logs = InventoryRegressionTests().run_exchange(Waiting())
        self.assertFalse(any(c.args[0] == "recovery" for c in logs.call_args_list))
        self.assertTrue(any(c.args[0] == "case_started" for c in logs.call_args_list))

    def test_cancel_wait_before_round(self):
        a = MirroredExchange()
        with patch.object(a, "get", side_effect=KeyboardInterrupt):
            logs = InventoryRegressionTests().run_exchange(a)
        self.assertTrue(
            any(c.args[0] == "waiting_cancelled" for c in logs.call_args_list)
        )
        self.assertFalse(a.writes)


class ExposureGuardTests(unittest.TestCase):
    def test_flat_both_sides_blocked(self):
        for action in ("BUY", "SELL"):
            with self.assertRaises(RuntimeError):
                r.validate_reducing_order(
                    dict(ticker=r.MAIN, action=action, quantity=1),
                    dict.fromkeys(r.MARKETS, 0),
                )

    def test_long_short_reductions_allowed(self):
        for position, action in [(100, "SELL"), (-100, "BUY")]:
            for ticker in r.MARKETS:
                r.validate_reducing_order(
                    dict(ticker=ticker, action=action, quantity=100),
                    dict.fromkeys(r.MARKETS, position),
                )

    def test_wrong_direction_and_crossing_zero(self):
        for position, action, qty in [
            (100, "BUY", 1),
            (-100, "SELL", 1),
            (100, "SELL", 101),
            (-100, "BUY", 101),
        ]:
            with self.assertRaises(RuntimeError):
                r.validate_reducing_order(
                    dict(ticker=r.MAIN, action=action, quantity=qty),
                    dict.fromkeys(r.MARKETS, position),
                )

    def test_invalid_sizes(self):
        for qty in (0, -1, 1.5, 25001, float("nan"), True):
            with self.assertRaises(RuntimeError):
                r.validate_reducing_order(
                    dict(ticker=r.MAIN, action="SELL", quantity=qty),
                    dict.fromkeys(r.MARKETS, 100000),
                )

    def test_live_boundary_blocks_before_write(self):
        a = r.API(True)
        with (
            patch.object(a, "positions", return_value=dict.fromkeys(r.MARKETS, 0)),
            patch.object(a.s, "request") as req,
        ):
            with self.assertRaises(RuntimeError):
                a.request(
                    "POST", "/orders", dict(ticker=r.ALT, action="BUY", quantity=100)
                )
        req.assert_not_called()

    def test_open_order_blocks_second_write(self):
        a = r.API(True)
        with (
            patch.object(a, "positions", return_value=dict.fromkeys(r.MARKETS, 100)),
            patch.object(a, "get", return_value=[dict(ticker=r.MAIN)]),
            patch.object(a.s, "request") as req,
        ):
            with self.assertRaises(RuntimeError):
                a.request(
                    "POST", "/orders", dict(ticker=r.ALT, action="SELL", quantity=100)
                )
        req.assert_not_called()


class AdaptiveLiquidityTests(unittest.TestCase):
    def books(self, quantity=6000, price=10):
        return {
            t: dict(
                asks=[dict(price=price, quantity=quantity)],
                bids=[dict(price=price - 0.01, quantity=quantity)],
            )
            for t in r.MARKETS
        }

    def profile(self, quantities):
        a = r.LiquidityEstimator()
        for i, q in enumerate(quantities):
            a.observe(self.books(q), float(i))
        with patch.object(r.time, "monotonic", return_value=len(quantities) - 1):
            return a.profile(True, 20)

    def test_stable_quotes_more_usable_than_depleting_quotes(self):
        stable = self.profile([6000] * 4)
        depleted = self.profile([48000, 24000, 12000, 6000])
        self.assertGreater(stable["depth_fraction"], depleted["depth_fraction"])
        self.assertEqual(depleted["refill_capacity"], 0)

    def test_replenishment_requires_history_and_common_prices(self):
        growing = self.profile([3000, 4000, 5000, 6000])
        self.assertGreater(growing["refill_capacity"], 0)
        a = r.LiquidityEstimator()
        for i in range(4):
            a.observe(self.books(6000, 10 + i * 0.10), float(i))
        with patch.object(r.time, "monotonic", return_value=3):
            p = a.profile(True, 20)
        self.assertEqual(p["refill_capacity"], 0)
        self.assertGreater(p["tail_stress"], growing["tail_stress"])

    def test_stale_history_does_not_credit_refill(self):
        a = r.LiquidityEstimator()
        for i, q in enumerate([3000, 4000, 5000, 6000]):
            a.observe(self.books(q), float(i))
        with patch.object(r.time, "monotonic", return_value=10):
            p = a.profile(True, 20)
        self.assertFalse(p["mature"])
        self.assertEqual(p["refill_capacity"], 0)

    def test_actual_partial_fills_reduce_confidence(self):
        a = r.LiquidityEstimator()
        before = a.profile(True, 20)
        a.record_fill(100, 1000)
        after = a.profile(True, 20)
        self.assertLess(after["depth_fraction"], before["depth_fraction"])
        self.assertGreater(after["tail_stress"], before["tail_stress"])

    def test_growing_liquidity_can_support_tender_rejected_by_cold_model(self):
        b = self.books(3000)
        t = dict(action="SELL", quantity=10000, price=10.20)
        cold = r.LiquidityEstimator().profile(True, 20)
        edge, _ = r.estimate_unwind(t, b, cold)
        self.assertIsNone(edge)
        p = self.profile([1500, 2000, 2500, 3000])
        edge, details = r.estimate_unwind(t, b, p)
        self.assertIsNotNone(edge)
        self.assertGreater(edge, 0)
        self.assertLessEqual(details["credited_refill_shares"], 3000)

    def test_small_positive_profit_is_accepted(self):
        b = self.books(20000)
        t = dict(action="SELL", ticker=r.MAIN, quantity=10000, price=10.01)
        ok, _, edge = r.should_accept(t, b, dict.fromkeys(r.MARKETS, 0), 10)
        self.assertTrue(ok)
        self.assertAlmostEqual(edge, 25)
        t["price"] = 10.006
        self.assertFalse(r.should_accept(t, b, dict.fromkeys(r.MARKETS, 0), 10)[0])

    def test_no_book_cannot_be_replaced_by_refill_credit(self):
        p = self.profile([1000, 2000, 3000, 4000])
        p["refill_capacity"] = 1000000
        self.assertIsNone(
            r.estimate_unwind(dict(action="SELL", quantity=10000, price=20), {}, p)[0]
        )


class TimingAndVisibilityRegressionTests(unittest.TestCase):
    def test_tiny_cleanup_cannot_poison_tender_budget(self):
        class Fake:
            pass

        a = Fake()
        r.record_hedge_timing(a, 72000, 14.142)
        before = a.hedge_seconds_per_share
        r.record_hedge_timing(a, 300, 1.327)
        self.assertEqual(a.hedge_seconds_per_share, before)
        budget = r.execution_budget(
            71000, max(0.6, r.CHILD_SIZE * a.hedge_seconds_per_share)
        )
        self.assertLess(budget, 35)

    def test_tiny_first_hedge_does_not_calibrate(self):
        class Fake:
            pass

        a = Fake()
        r.record_hedge_timing(a, 600, 0.68)
        self.assertFalse(hasattr(a, "hedge_seconds_per_share"))

    def test_old_slow_hedge_expires(self):
        class Fake:
            pass

        a = Fake()
        for secs in [60, 12, 11, 10]:
            r.record_hedge_timing(a, 80000, secs)
        self.assertEqual(a.hedge_seconds_per_share, 12 / 80000)

    def test_known_child_reappears_then_clears_without_new_post(self):
        class Fake:
            reads = 0

            def get(self, path, params=None):
                self.reads += 1
                return [dict(order_id=99, ticker=r.MAIN)] if self.reads == 2 else []

        a = Fake()
        after = dict.fromkeys(r.MARKETS, -100)
        with (
            patch.object(r, "stable_positions", return_value=after),
            patch.object(r, "cancel_order"),
            patch.object(r.time, "sleep"),
            patch.object(r, "log"),
        ):
            self.assertEqual(
                r.reconcile_missing_order(
                    a, 99, dict.fromkeys(r.MARKETS, 0), "SELL", 100
                ),
                100,
            )
        self.assertEqual(a.reads, 4)

    def test_known_child_never_clears_still_blocks(self):
        class Fake:
            def get(self, path, params=None):
                return [dict(order_id=99, ticker=r.MAIN)]

        with (
            patch.object(
                r, "stable_positions", return_value=dict.fromkeys(r.MARKETS, -100)
            ),
            patch.object(r, "cancel_order"),
            patch.object(r.time, "sleep"),
            patch.object(r, "log"),
        ):
            with self.assertRaisesRegex(RuntimeError, "bounded"):
                r.reconcile_missing_order(
                    Fake(), 99, dict.fromkeys(r.MARKETS, 0), "SELL", 100
                )


class PendingReservationTests(unittest.TestCase):
    def fake(self, position=-39700, detail=None):
        class Fake:
            pending_order = dict(
                order_id=825,
                before=dict.fromkeys(r.MARKETS, -39700),
                action="BUY",
                quantity=25000,
            )

            def positions(self):
                return dict.fromkeys(r.MARKETS, position)

            def get(self, path, params=None):
                if path == "/orders":
                    return []
                if detail is not None:
                    return detail
                response = r.requests.Response()
                response.status_code = 404
                raise r.requests.HTTPError(response=response)

        return Fake()

    def test_unchanged_inventory_retains_reservation(self):
        a = self.fake()
        with patch.object(r.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "reservation retained"):
                r.resolve_pending_order(a)
        self.assertIsNotNone(a.pending_order)

    def test_partial_missing_fill_retains_reservation(self):
        a = self.fake(-30000)
        with patch.object(r.time, "sleep"):
            with self.assertRaises(RuntimeError):
                r.resolve_pending_order(a)
        self.assertIsNotNone(a.pending_order)

    def test_late_full_fill_resolves_then_oversized_replacement_blocked(self):
        a = self.fake(-14700)
        with patch.object(r.time, "sleep"), patch.object(r, "log"):
            r.resolve_pending_order(a)
        self.assertIsNone(a.pending_order)
        with self.assertRaises(RuntimeError):
            r.validate_reducing_order(
                dict(ticker=r.ALT, action="BUY", quantity=25000), a.positions()
            )

    def test_terminal_cancelled_zero_fill_can_release(self):
        a = self.fake(detail=dict(status="CANCELLED", quantity_filled=0))
        with patch.object(r.time, "sleep"), patch.object(r, "log"):
            r.resolve_pending_order(a)
        self.assertIsNone(a.pending_order)

    def test_terminal_record_waits_for_inventory(self):
        a = self.fake(detail=dict(status="TRANSACTED", quantity_filled=25000))
        with patch.object(r.time, "sleep"):
            with self.assertRaises(RuntimeError):
                r.resolve_pending_order(a)

    def test_recovery_does_not_cover_while_fill_unresolved(self):
        a = self.fake()
        with patch.object(r.time, "sleep"), patch.object(r, "cover") as cover:
            with self.assertRaises(RuntimeError):
                r.recovery_step(a)
        cover.assert_not_called()

    def test_unknown_submission_id_blocks_new_write(self):
        a = r.API(True)
        a.pending_order = dict(order_id=None)
        with patch.object(a.s, "request") as req:
            with self.assertRaises(RuntimeError):
                a.request(
                    "POST", "/orders", dict(ticker=r.ALT, action="BUY", quantity=100)
                )
        req.assert_not_called()


class LoggingTests(unittest.TestCase):
    def test_console_and_file_share_one_serialized_event(self):
        with (
            patch.object(r, "LOG") as logfile,
            patch("builtins.print") as console,
            patch.object(r.json, "dumps", wraps=r.json.dumps) as encode,
        ):
            r.log("test_event", quantity=123)
        encode.assert_called_once()
        line = console.call_args.args[0]
        console.assert_called_once_with(line, flush=True)
        logfile.open.return_value.__enter__.return_value.write.assert_called_once_with(
            line + "\n"
        )


if __name__ == "__main__":
    unittest.main()
