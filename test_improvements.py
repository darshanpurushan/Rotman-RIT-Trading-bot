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
