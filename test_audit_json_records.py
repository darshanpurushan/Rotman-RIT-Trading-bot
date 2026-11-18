import json
import tempfile
import unittest
from pathlib import Path

from audit_events import read_events, summarize


class AuditJsonRecordTests(unittest.TestCase):
    def test_non_object_records_report_physical_line_number(self):
        for value in (None, [], 1, True, "event"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "events.jsonl"
                path.write_text('\n{"event":"start"}\n' + json.dumps(value) + '\n')
                with self.assertRaisesRegex(ValueError, "JSON object on line 3"):
                    summarize(read_events(path))

    def test_bom_blank_lines_and_valid_events_remain_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text('\ufeff\n{"event":"start"}\n\n{"event":"child","filled":5}\n')
            session, = summarize(read_events(path))
            self.assertEqual(session["confirmed_child_volume"], 5)

    def test_invalid_json_retains_line_number(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text('\n{invalid}\n')
            with self.assertRaisesRegex(ValueError, "Invalid JSON on line 2"):
                list(read_events(path))
