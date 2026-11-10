import unittest
from unittest.mock import patch

import algo3_router as router


class TenderNumberTests(unittest.TestCase):
    def tender(self, **updates):
        tender = dict(tender_id=1, ticker=router.MAIN, action="SELL",
                      quantity=1000, price=21., expires=200)
        tender.update(updates)
        return tender

    def evaluate(self, tender):
        books = {ticker: {"asks": [dict(price=20., quantity=10000)],
                          "bids": [dict(price=19.98, quantity=10000)]}
                 for ticker in router.MARKETS}
        return router.should_accept(tender, books, dict.fromkeys(router.MARKETS, 0), 10)

    def test_invalid_prices_are_rejected_before_estimation(self):
        for price in (float("inf"), float("-inf"), float("nan"), 0., -1., True, "21"):
            with self.subTest(price=price), patch.object(router, "estimate_tender", return_value=(100., {})) as estimate:
                accepted, _, edge = self.evaluate(self.tender(price=price))
                self.assertFalse(accepted)
                self.assertIsNone(edge)
                estimate.assert_not_called()

    def test_invalid_quantities_are_rejected_before_budgeting(self):
        for quantity in (1000.5, float("nan"), float("inf"), True, "1000", None):
            with self.subTest(quantity=quantity), patch.object(router, "tender_execution_budget", return_value=12.) as budget:
                self.assertFalse(self.evaluate(self.tender(quantity=quantity))[0])
                budget.assert_not_called()

    def test_valid_profitable_tender_remains_eligible(self):
        self.assertTrue(self.evaluate(self.tender())[0])
