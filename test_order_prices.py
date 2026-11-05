import unittest
from unittest.mock import Mock

import algo3_router as router


class LimitPriceValidationTests(unittest.TestCase):
    def test_invalid_limit_prices_are_blocked_before_submission(self):
        for price in (None, True, 0, -1, float("nan"), float("inf"),
                      float("-inf"), "20.00"):
            with self.subTest(price=price):
                api = router.API(live=True)
                api.positions = Mock(return_value=dict.fromkeys(router.MARKETS, 100))
                api.s = Mock()
                open_orders = Mock(status_code=200, content=b"[]")
                open_orders.json.return_value = []
                submitted = Mock(status_code=200, content=b"{}")
                submitted.json.return_value = {"order_id": 1}
                api.s.request.side_effect = [open_orders, submitted]
                params = dict(ticker=router.MAIN, action="SELL", quantity=50,
                              type="LIMIT", price=price)
                with self.assertRaisesRegex(RuntimeError, "limit order price"):
                    api.request("POST", "/orders", params)
                api.s.request.assert_not_called()
                self.assertIsNone(getattr(api, "pending_order", None))

    def test_finite_positive_prices_and_market_exits_remain_valid(self):
        positions = dict.fromkeys(router.MARKETS, 100)
        for price in (20, 20.01):
            router.validate_reducing_order(
                dict(ticker=router.MAIN, action="SELL", quantity=50,
                     type="LIMIT", price=price), positions)
        router.validate_reducing_order(
            dict(ticker=router.MAIN, action="SELL", quantity=100, type="MARKET"),
            positions)


if __name__ == "__main__":
    unittest.main()
