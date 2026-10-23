import unittest

from audit_events import summarize


class AuditTests(unittest.TestCase):
    def test_versions_and_bot_acceptances_stay_separate(self):
        rows = [
            dict(event="start", version="older"),
            dict(event="decision", tender=dict(tender_id=1), accept=True),
            dict(event="liquidation_mode", positions={"THOR_M": 100}),
            dict(event="start", version="newer"),
            dict(event="tender_accepted", tender_id=2),
            dict(event="child", requested=100, filled=40, price=10),
            dict(event="recovery", reason="read failure"),
            dict(event="recovery_rearmed"),
        ]
        first, second = summarize(rows)
        self.assertEqual(first["automatic_acceptances"], [])
        self.assertEqual(first["decision_tenders"], [1])
        self.assertEqual(second["automatic_acceptances"], [2])
        self.assertEqual(second["confirmed_child_volume"], 40)
        self.assertEqual(second["rearmed"], 1)
        self.assertNotIn("pnl", second)
