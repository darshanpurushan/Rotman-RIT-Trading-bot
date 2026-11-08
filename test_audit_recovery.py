import unittest

from audit_events import summarize


class RecoveredFillAuditTests(unittest.TestCase):
    def test_recovery_only_fill_is_counted(self):
        session, = summarize([dict(event="start"),
                              dict(event="pending_order_resolved", order_id=1, filled=75)])
        self.assertEqual(session["confirmed_child_volume"], 75)

    def test_recovery_and_child_records_count_an_order_only_once(self):
        for events in (("pending_order_resolved", "child"), ("child", "pending_order_resolved")):
            rows = [dict(event="start")]
            rows.extend(dict(event=event, order_id=1, filled=50, requested=100) for event in events)
            rows.append(dict(event="pending_order_resolved", order_id="1", filled=50))
            session, = summarize(rows)
            self.assertEqual(session["confirmed_child_volume"], 50)
            self.assertEqual(session["children"], 1)

    def test_order_ids_are_scoped_to_each_session(self):
        rows = [dict(event="start"), dict(event="pending_order_resolved", order_id=1, filled=50),
                dict(event="start"), dict(event="pending_order_resolved", order_id=1, filled=20)]
        self.assertEqual([s["confirmed_child_volume"] for s in summarize(rows)], [50, 20])

    def test_later_confirmations_only_add_new_volume(self):
        rows = [dict(event="start"),
                dict(event="child", order_id=1, filled=50, requested=100),
                dict(event="pending_order_resolved", order_id=1, filled=75),
                dict(event="pending_order_resolved", order_id=1, filled=50),
                dict(event="pending_order_resolved", order_id=2, filled=20)]
        session, = summarize(rows)
        self.assertEqual(session["confirmed_child_volume"], 95)
