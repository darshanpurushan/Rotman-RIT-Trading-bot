import unittest
from unittest.mock import patch

import algo3_router as r


def books(bid=10.0, ask=10.02, quantity=30000):
    return {
        t: {
            "bids": [{"price": bid, "quantity": quantity}],
            "asks": [{"price": ask, "quantity": quantity}],
        }
        for t in r.MARKETS
    }


class NearLiquidityTests(unittest.TestCase):
    def profile(self, distant=False):
        estimator = r.LiquidityEstimator()
        for i in range(5):
            book = books(quantity=30000 if distant else 30000 + i * 10000)
            if distant:
                book[r.MAIN]["asks"].append(dict(price=15, quantity=10000 + i * 10000))
            estimator.observe(book, stamp=float(i))
        with patch.object(r.time, "monotonic", return_value=4):
            return estimator.profile(True, 8)

    def test_only_additions_near_current_execution_price_support_patient_capacity(self):
        self.assertGreater(self.profile()["near_refill_capacity"], 0)
        self.assertEqual(self.profile(distant=True)["near_refill_capacity"], 0)

    def test_stale_history_cannot_support_patient_execution(self):
        estimator = r.LiquidityEstimator()
        for i in range(5):
            estimator.observe(books(quantity=10000 + i * 10000), stamp=float(i))
        with patch.object(r.time, "monotonic", return_value=10):
            self.assertEqual(estimator.profile(True, 8)["near_refill_capacity"], 0)

    def test_adverse_movement_is_directional(self):
        estimator = r.LiquidityEstimator()
        for i in range(5):
            estimator.observe(
                books(bid=10 + i * 0.01, ask=10.02 + i * 0.01), stamp=float(i)
            )
        with patch.object(r.time, "monotonic", return_value=4):
            self.assertGreater(estimator.profile(True, 8)["adverse_per_second"], 0)
            self.assertEqual(estimator.profile(False, 8)["adverse_per_second"], 0)


class PatientAdmissionTests(unittest.TestCase):
    def profile(self, **kwargs):
        result = dict(
            depth_fraction=1.0,
            tail_stress=0.02,
            refill_capacity=0,
            mature=True,
            near_refill_capacity=60000,
            near_price_band=0.03,
            adverse_per_second=0,
        )
        result.update(kwargs)
        return result

    def test_distant_depth_does_not_force_sweep_if_near_refill_is_observed(self):
        book = books(quantity=10000)
        book[r.MAIN]["asks"].append(dict(price=12, quantity=100000))
        tender = dict(action="SELL", quantity=70000, price=10.3)
        # At least 35% must be available now when future capacity is capped at 65%.
        book[r.MAIN]["asks"][0]["quantity"] = 20000
        edge, details = r.estimate_unwind(tender, book, self.profile())
        self.assertGreater(edge, 0)
        self.assertLess(details["sweep_net_edge"], 0)
        self.assertEqual(
            details["execution_model"], "observed_near_price_replenishment"
        )
        self.assertEqual(details["patient_future_shares"], 40000)

    def test_no_recent_near_refill_keeps_loss_rejection(self):
        book = books(quantity=15000)
        book[r.MAIN]["asks"].append(dict(price=12, quantity=100000))
        tender = dict(action="SELL", quantity=70000, price=10.3)
        for profile in [
            self.profile(mature=False),
            self.profile(near_refill_capacity=0),
            self.profile(near_refill_capacity=10000),
        ]:
            edge, details = r.estimate_unwind(tender, book, profile)
            self.assertLess(edge, 0)
            self.assertEqual(details["execution_model"], "sweep")

    def test_patient_long_exit_and_adverse_price_stress(self):
        book = books(quantity=15000)
        book[r.MAIN]["bids"].append(dict(price=8, quantity=100000))
        tender = dict(action="BUY", quantity=70000, price=9.7)
        good, _ = r.estimate_unwind(tender, book, self.profile())
        stressed, _ = r.estimate_unwind(
            tender, book, self.profile(adverse_per_second=0.1)
        )
        self.assertGreater(good, 0)
        self.assertLess(stressed, 0)

    def test_future_capacity_cannot_replace_nearly_entire_book(self):
        tender = dict(action="SELL", quantity=70000, price=10.3)
        _, details = r.estimate_unwind(
            tender, books(quantity=1000), self.profile(near_refill_capacity=1000000)
        )
        self.assertEqual(details["execution_model"], "sweep")


class ExitValueTests(unittest.TestCase):
    def api(self, action="BUY"):
        api = r.API(False)
        api.hedge_plan = dict(
            tender_id=1,
            source="test",
            action=action,
            entry_price=9.8 if action == "BUY" else 10.2,
            profit_bound=0,
            keep_profit=1,
            start_tick=100,
            soft_deadline=130,
        )
        return api

    def test_executable_profit_target_fades_without_ignoring_prices(self):
        api = self.api()
        with patch.object(r, "log"):
            initial = r.update_exit_value(api, books(), 10000, 100)
            target = api.hedge_plan["active_profit_target"]
            self.assertAlmostEqual(target, initial * r.EARLY_PROFIT_RETENTION)
            r.update_exit_value(api, books(bid=9.95, ask=9.97), 10000, 115)
        self.assertLess(api.hedge_plan["active_profit_target"], target)
        self.assertEqual(api.hedge_plan["peak_exit_estimate"], initial)

    def test_insufficient_depth_does_not_claim_full_position_executable_pnl(self):
        api = self.api()
        with patch.object(r, "log"):
            self.assertIsNone(r.update_exit_value(api, books(quantity=100), 10000, 100))
        self.assertEqual(api.hedge_plan["active_profit_target"], 1)

    def test_guard_protects_more_than_bare_break_even_after_profit_deteriorates(self):
        api = self.api()
        with patch.object(r, "log"):
            r.update_exit_value(api, books(), 10000, 100)
            _, price, rest = r.protect_exit(
                api, books(), False, 10000, 101, r.ALT, 9.85
            )
        self.assertGreater(price, 9.90)
        self.assertGreater(rest, 0.1)

    def test_guard_reserves_active_execution_time(self):
        api = self.api()
        with patch.object(r, "log"):
            result = r.protect_exit(api, books(), False, 10000, 128, r.ALT, 9.85)
        self.assertEqual(result, (r.ALT, 9.85, 0.1))

    def test_short_profit_uses_ask_side_and_fees(self):
        api = self.api(action="SELL")
        with patch.object(r, "log"):
            value = r.update_exit_value(api, books(), 10000, 100)
        self.assertAlmostEqual(value, 10000 * (10.2 - 10.02 - 0.005))

    def test_volatile_prices_can_make_passive_wait_uneconomic(self):
        api = self.api()
        api.hedge_plan = None
        state = r.unwind_schedule(api, 10000, False)
        with patch.object(
            api.liquidity,
            "profile",
            return_value=dict(adverse_per_second=0.1, price_noise_per_second=0.1),
        ):
            choice = r.choose_unwind_order(api, books(), False, 10000, 100, state)
        self.assertFalse(choice["passive"])

    def test_favorable_price_move_locks_active_exit(self):
        api = self.api()
        api.hedge_plan = None
        state = r.unwind_schedule(api, 10000, False)
        r.choose_unwind_order(api, books(), False, 10000, 100, state)
        choice = r.choose_unwind_order(
            api, books(bid=10.02, ask=10.04), False, 10000, 101, state
        )
        self.assertFalse(choice["passive"])


class ActiveChildTests(unittest.TestCase):
    def test_tiny_same_venue_quotes_combine_within_one_cent(self):
        api = r.API(False)
        book = {
            r.MAIN: {
                "asks": [dict(price=10, quantity=1), dict(price=10.01, quantity=9999)]
            },
            r.ALT: {"asks": [dict(price=10.02, quantity=10000)]},
        }
        self.assertEqual(
            r.active_exit_child(api, book, True, 10000, 100), (r.MAIN, 10.01, 5000)
        )

    def test_competing_better_quote_prevents_trading_through(self):
        api = r.API(False)
        book = {
            r.MAIN: {
                "asks": [dict(price=10, quantity=1), dict(price=10.02, quantity=9999)]
            },
            r.ALT: {"asks": [dict(price=10.01, quantity=10000)]},
        }
        self.assertEqual(
            r.active_exit_child(api, book, True, 10000, 100), (r.MAIN, 10, 1)
        )

    def test_residual_and_deadline_control_size(self):
        api = r.API(False)
        self.assertEqual(r.active_exit_child(api, books(), False, 123, 100)[2], 123)
        api.child_seconds = 4
        self.assertEqual(
            r.active_exit_child(api, books(quantity=100000), False, 80000, 270)[2],
            10000,
        )

    def test_sell_limit_never_reaches_past_one_cent(self):
        api = r.API(False)
        book = {
            r.MAIN: {
                "bids": [
                    dict(price=10, quantity=1),
                    dict(price=9.99, quantity=999),
                    dict(price=9.98, quantity=20000),
                ]
            },
            r.ALT: {},
        }
        self.assertEqual(
            r.active_exit_child(api, book, False, 10000, 100), (r.MAIN, 9.99, 1000)
        )


class TerminalEvidenceTests(unittest.TestCase):
    def record(self, **updates):
        result = dict(
            order_id=12,
            ticker=r.ALT,
            quantity=5000,
            quantity_filled=5000,
            status="TRANSACTED",
            action="BUY",
        )
        result.update(updates)
        return result

    def test_explicit_terminal_response_survives_missing_detail_endpoint(self):
        api = r.API(False)
        self.assertTrue(r.remember_terminal_order(api, self.record(), 12))
        with patch.object(api, "get", side_effect=AssertionError("Unnecessary read")):
            self.assertEqual(r.get_order_detail(api, 12)["quantity_filled"], 5000)

    def test_cancelled_zero_fill_is_terminal_but_open_zero_fill_is_not(self):
        api = r.API(False)
        self.assertFalse(
            r.remember_terminal_order(
                api, self.record(status="OPEN", quantity_filled=0), 12
            )
        )
        self.assertTrue(
            r.remember_terminal_order(
                api, self.record(status="CANCELLED", quantity_filled=0), 12
            )
        )

    def test_wrong_id_quantity_or_side_cannot_cache_submission_evidence(self):
        api = r.API(False)
        params = dict(ticker=r.ALT, quantity=5000, action="BUY")
        for record in [
            self.record(order_id=13),
            self.record(quantity_filled=6000),
            self.record(quantity=6000, quantity_filled=6000),
            self.record(action="SELL"),
            self.record(quantity_filled=4999),
            {"success": True},
        ]:
            self.assertFalse(r.remember_terminal_order(api, record, 12, params))


class EndToEndImprovementTests(unittest.TestCase):
    def test_books_are_observed_even_without_tenders(self):
        from test_router import InventoryRegressionTests, MirroredExchange

        class IdleExchange(MirroredExchange):
            def __init__(self):
                super().__init__()
                self.case_reads = 0
                self.book_reads = 0
                self.liquidity = r.LiquidityEstimator()

            def get(self, path, params=None):
                if path == "/case":
                    self.case_reads += 1
                    return dict(
                        status="ACTIVE" if self.case_reads <= 10 else "STOPPED",
                        tick=self.case_reads,
                    )
                if path == "/tenders":
                    return []
                return super().get(path, params)

            def books(self):
                self.book_reads += 1
                value = super().books()
                self.liquidity.observe(value)
                return value

        api = IdleExchange()
        clock = iter(i * 0.2 for i in range(1000))
        with patch.object(r.time, "monotonic", side_effect=lambda: next(clock)):
            InventoryRegressionTests().run_exchange(api)
        self.assertGreaterEqual(api.book_reads, 4)
        self.assertFalse(api.writes)
        self.assertGreaterEqual(len(api.liquidity.history), 4)

    def test_terminal_post_record_avoids_missing_order_detail_and_keeps_active_timing(
        self,
    ):
        from test_router import MirroredExchange

        class TerminalExchange(MirroredExchange):
            def request(self, method, path, params=None):
                result = super().request(method, path, params)
                if method == "POST" and path == "/orders":
                    return dict(self.orders[result["order_id"]])
                return result

            def get(self, path, params=None):
                if path.startswith("/orders/"):
                    raise AssertionError(
                        "Terminal POST response already confirms order"
                    )
                return super().get(path, params)

        api = TerminalExchange(initial=-5000)
        with patch.object(r.time, "sleep"), patch.object(r, "log"):
            filled = r.execute_child(api, r.ALT, "BUY", 5000, 19.98, 1, api.positions())
        self.assertEqual(filled, 5000)
        self.assertEqual(api.position, 0)
        self.assertEqual(api.child_seconds, 0.6)

    def test_partial_fills_revalue_only_remaining_inventory(self):
        api = r.API(False)
        tender = dict(tender_id=1, action="BUY", quantity=10000, price=9.8)
        with patch.object(r, "log"):
            r.begin_hedge_plan(api, tender, books(), 100)
            r.credit_hedge_fill(api, r.ALT, "SELL", 4000, 10)
            projected = r.update_exit_value(api, books(), 6000, 101)
        self.assertAlmostEqual(projected, 10000 * (10 - 0.005 - 9.8))
        self.assertAlmostEqual(
            api.hedge_plan["profit_bound"], 4000 * (10 - 0.005 - 9.8)
        )

    def test_replenishment_estimate_still_obeys_position_and_time_limits(self):
        tender = dict(
            tender_id=1,
            ticker=r.MAIN,
            action="SELL",
            quantity=70000,
            price=10.3,
            expires=290,
            is_fixed_bid=True,
        )
        profile = PatientAdmissionTests().profile()
        book = books(quantity=15000)
        book[r.MAIN]["asks"].append(dict(price=12, quantity=100000))
        self.assertTrue(
            r.should_accept(
                tender, book, dict.fromkeys(r.MARKETS, 0), 100, profile=profile
            )[0]
        )
        self.assertFalse(
            r.should_accept(
                tender, book, dict.fromkeys(r.MARKETS, 1), 100, profile=profile
            )[0]
        )
        self.assertFalse(
            r.should_accept(
                tender, book, dict.fromkeys(r.MARKETS, 0), 270, profile=profile
            )[0]
        )
        tender["quantity"] = 110000
        self.assertFalse(
            r.should_accept(
                tender, book, dict.fromkeys(r.MARKETS, 0), 100, profile=profile
            )[0]
        )
