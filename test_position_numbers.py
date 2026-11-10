import unittest
from unittest.mock import Mock

import algo3_router as router


class PositionNumberTests(unittest.TestCase):
    def test_invalid_inventory_is_not_silently_coerced(self):
        for value in (1.5, float('nan'), float('inf'), True, '1'):
            with self.subTest(value=value):
                api = router.API()
                api.get = Mock(return_value=[dict(ticker=t, position=value) for t in router.MARKETS])
                with self.assertRaisesRegex(RuntimeError, 'position value'):
                    api.positions()

    def test_duplicate_inventory_rows_are_ambiguous(self):
        api = router.API()
        api.get = Mock(return_value=[dict(ticker=router.MAIN, position=1), dict(ticker=router.MAIN, position=2), dict(ticker=router.ALT, position=2)])
        with self.assertRaisesRegex(RuntimeError, 'Duplicate'):
            api.positions()

    def test_signed_integral_inventory_remains_supported(self):
        api = router.API()
        api.get = Mock(return_value=[dict(ticker=t, position=-100.) for t in router.MARKETS])
        self.assertEqual(api.positions(), dict.fromkeys(router.MARKETS, -100))
