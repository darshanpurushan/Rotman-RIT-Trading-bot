import unittest

import algo3_router as router


class BookNumberTests(unittest.TestCase):
    def test_malformed_book_rows_cannot_create_executable_depth(self):
        for field, value in [('price', float('inf')), ('price', float('nan')), ('price', 0.), ('quantity', 1.5), ('quantity_filled', -1), ('quantity_filled', 101)]:
            with self.subTest(field=field, value=value):
                row = dict(price=20., quantity=100, quantity_filled=0)
                row[field] = value
                with self.assertRaisesRegex(RuntimeError, 'book'):
                    router.aggregate({router.MAIN: {'asks': [row]}}, True)

    def test_valid_partial_depth_is_preserved(self):
        out = router.aggregate({router.MAIN: {'asks': [dict(price=20., quantity=100, quantity_filled=40)]}}, True)
        self.assertEqual(out, [(20., 60, router.MAIN)])
