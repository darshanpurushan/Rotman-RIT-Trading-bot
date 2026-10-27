import unittest
from unittest.mock import patch

import algo3_router as r
import test_router as fixtures
from test_router import MirroredExchange


class LateTenderTests(unittest.TestCase):
    def tender(self, action="BUY", quantity=71000, price=None):
        return dict(
            tender_id=833,
            ticker=r.MAIN,
            action=action,
            quantity=quantity,
            price=price if price is not None else (19.8 if action == "BUY" else 20.2),
            expires=290,
            is_fixed_bid=True,
        )

    def evaluate(
        self, tick=260, action="BUY", quantity=71000, price=None, book=None, speed=0.6
    ):
        return r.should_accept(
            self.tender(action, quantity, price),
            book or MirroredExchange().books(),
            dict.fromkeys(r.MARKETS, 0),
            tick,
            speed,
        )

    def test_full_depth_profitable_fifth_tender_is_eligible_at_260_both_sides(self):
        for action in ("BUY", "SELL"):
            self.assertTrue(self.evaluate(action=action)[0])

    def test_tender_identifier_does_not_change_admission(self):
        outcomes = []
        for tid in (33, 233, 433, 633, 833):
            tender = self.tender()
            tender["tender_id"] = tid
            outcomes.append(
                r.should_accept(
                    tender, MirroredExchange().books(), dict.fromkeys(r.MARKETS, 0), 260
                )
            )
        self.assertTrue(all(value == outcomes[0] for value in outcomes))
        self.assertTrue(outcomes[0][0])

    def test_more_time_does_not_reject_same_profitable_offer(self):
        for tick in (240, 250, 255, 260):
            self.assertTrue(self.evaluate(tick=tick)[0])

    def test_smaller_offer_can_fit_when_larger_one_cannot(self):
        self.assertFalse(self.evaluate(tick=270)[0])
        self.assertTrue(self.evaluate(tick=270, quantity=10000)[0])

    def test_hard_entry_boundary_precedes_urgent_market_closeout(self):
        self.assertFalse(self.evaluate(tick=275, quantity=1000)[0])

    def test_slow_execution_rejects_large_late_offer(self):
        self.assertFalse(self.evaluate(speed=2)[0])

    def test_missing_depth_cannot_be_replaced_by_late_refill_credit(self):
        book = MirroredExchange().books()
        for venue in book:
            book[venue]["bids"][0]["quantity"] = 20000
        profile = r.LiquidityEstimator().profile(False, 8)
        profile.update(
            mature=True,
            refill_capacity=1000000,
            near_refill_capacity=1000000,
            recent_execution_capacity=1000000,
        )
        ok, reason, _ = r.should_accept(
            self.tender(), book, dict.fromkeys(r.MARKETS, 0), 260, profile=profile
        )
        self.assertFalse(ok)
        self.assertIn("full discounted displayed", reason)

    def test_late_stress_rejects_thin_profit_margin(self):
        self.assertTrue(self.evaluate(tick=100, quantity=10000, price=19.96)[0])
        self.assertFalse(self.evaluate(tick=260, quantity=10000, price=19.96)[0])

    def test_fragmentation_increases_execution_budget(self):
        tender = self.tender(quantity=10000)
        book = {
            r.MAIN: {
                "bids": [dict(price=20 - i * 0.01, quantity=1000) for i in range(10)]
            },
            r.ALT: {"bids": []},
        }
        self.assertGreater(
            r.tender_execution_budget(tender, book, 260),
            r.tender_execution_budget(tender, MirroredExchange().books(), 260),
        )

    def test_late_hedge_uses_active_limits_without_passive_waiting(self):
        api = MirroredExchange(tick=250)
        with patch.object(r, "log"):
            r.begin_hedge_plan(api, self.tender(), api.books(), 250)
            state = r.unwind_schedule(api, 71000, False)
            choice = r.choose_unwind_order(api, api.books(), False, 71000, 250, state)
        self.assertFalse(choice["passive"])
        self.assertIsNotNone(choice["price"])
        self.assertEqual(choice["rest"], 0.1)

    def test_run_actually_accepts_and_flattens_after_old_cutoff(self):
        for action in ("BUY", "SELL"):
            api = MirroredExchange(action=action, tick=260)
            logs = fixtures.InventoryRegressionTests().run_exchange(api)
            self.assertTrue(api.accepted)
            self.assertEqual(api.position, 0)
            children = [
                p
                for method, path, p in api.writes
                if method == "POST" and path == "/orders"
            ]
            self.assertEqual(sum(p["quantity"] for p in children), 71000)
            self.assertTrue(all(p["type"] == "LIMIT" for p in children))
            self.assertFalse(any(c.args[0] == "recovery" for c in logs.call_args_list))

    def test_preflight_blocks_offer_when_slow_reads_consume_time(self):
        class SlowPreflight(MirroredExchange):
            case_reads = 0

            def get(self, path, params=None):
                if path == "/case":
                    self.case_reads += 1
                    if self.case_reads >= 3:
                        self.tick = 275
                    if self.case_reads >= 5:
                        return dict(status="STOPPED", tick=300)
                return super().get(path, params)

        api = SlowPreflight(tick=260)
        logs = fixtures.InventoryRegressionTests().run_exchange(api)
        self.assertFalse(api.accepted)
        self.assertFalse(api.writes)
        self.assertTrue(
            any(c.args[0] == "tender_preflight_blocked" for c in logs.call_args_list)
        )
