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
