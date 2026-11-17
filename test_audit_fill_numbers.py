import unittest

from audit_events import summarize


class AuditFillNumberTests(unittest.TestCase):
    def test_invalid_fill_quantities_raise_in_all_record_forms(self):
        for event, identity in (("child", {}), ("child", {"order_id": 1}),
                                ("pending_order_resolved", {"order_id": 1})):
            for value in (-1, 1.5, True, False, "10", None,
                          float("nan"), float("inf"), -float("inf")):
                with self.subTest(event=event, identity=identity, value=value):
                    with self.assertRaisesRegex(ValueError, "filled"):
                        summarize([dict(event="start"),
                                   dict(event=event, filled=value, **identity)])

    def test_integral_float_and_zero_keep_cumulative_deduplication(self):
        rows = [dict(event="start"),
                dict(event="child", order_id=1, filled=10.0, requested=100),
                dict(event="pending_order_resolved", order_id=1, filled=15.0),
                dict(event="pending_order_resolved", order_id=1, filled=0),
                dict(event="child", filled=5.0, requested=100),
                dict(event="child", requested=100)]
        session, = summarize(rows)
        self.assertEqual(session["confirmed_child_volume"], 20)
        self.assertEqual(session["zero_fill_children"], 1)
