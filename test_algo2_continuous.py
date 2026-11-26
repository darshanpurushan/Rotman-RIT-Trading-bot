import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import algo2_market_maker as m
import requests
from test_algo2 import Fake, book, order


class ContinuousTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.api = Fake()
        self.bot = m.Bot(
            self.api,
            m.Config(),
            True,
            Path(self.tmp.name) / "state.json",
            Path(self.tmp.name) / "events.jsonl",
        )
        self.bot.log = lambda *args, **kw: None
        self.bot.initialize()

    def drive(self, cases, step):
        def next_case():
            value = next(cases)
            if isinstance(value, Exception):
                raise value
            self.api.tick = value["tick"]
            return value

        with patch.object(self.api, "case", side_effect=next_case), patch.object(
            self.bot, "step", side_effect=step
        ), patch.object(m.signal, "signal"), patch.object(m.time, "sleep"):
            self.bot.run()

    def test_multiple_rounds_without_relaunch(self):
        seen = []

        def step(tick):
            seen.append(tick)
            if len(seen) == 4:
                self.bot.request_stop()

        cases = iter(
            [
                dict(status="ACTIVE", tick=1),
                dict(status="ACTIVE", tick=299),
                dict(status="STOPPED", tick=300),
                dict(status="STOPPED", tick=0),
                dict(status="ACTIVE", tick=0),
                dict(status="ACTIVE", tick=1),
            ]
        )
        self.drive(cases, step)
        self.assertEqual(seen, [1, 299, 0, 1])
        self.assertEqual(self.bot.round_number, 3)  # test setup initializes once
        self.assertFalse(self.bot.unknown)

    def test_pause_resume_does_not_reinitialize_round(self):
        seen = []

        def step(tick):
            seen.append(tick)
            if tick == 51:
                self.bot.request_stop()

        self.drive(
            iter(
                [
                    dict(status="ACTIVE", tick=50),
                    dict(status="PAUSED", tick=50),
                    dict(status="ACTIVE", tick=51),
                ]
            ),
            step,
        )
        self.assertEqual(seen, [50, 51])
        self.assertEqual(self.bot.round_number, 2)

    def test_clean_recovery_resumes_in_same_round(self):
        seen = []
        self.bot.started = True
        self.bot.c.recovery_cooldown = 0

        def step(tick):
            seen.append(tick)
            if len(seen) == 1:
                raise RuntimeError("recoverable processing failure")
            if len(seen) == 3:
                self.bot.request_stop()

        self.drive(iter([dict(status="ACTIVE", tick=t) for t in (50, 51, 52)]), step)
        self.assertEqual(seen, [50, 51, 52])

    def test_unknown_response_never_rearms_at_round_reset(self):
        self.bot.started = True
        self.bot.last_tick = 299
        self.bot.unknown = True
        with patch.object(self.bot, "cancel") as cancel:
            self.drive(
                iter([dict(status="ACTIVE", tick=1)]),
                lambda _: self.fail("must not quote"),
            )
        cancel.assert_not_called()
        self.assertTrue(self.bot.unknown)

    def test_known_unresolved_order_never_cancelled_in_new_round(self):
        self.bot.submit("BUY", 1000, 10)
        self.bot.started = True
        self.bot.last_tick = 299
        with patch.object(self.bot, "cancel") as cancel:
            self.drive(
                iter([dict(status="ACTIVE", tick=1)]),
                lambda _: self.fail("must not quote"),
            )
        cancel.assert_not_called()
        self.assertIn("1", self.bot.records)

    def test_dirty_new_round_does_not_quote(self):
        self.bot.started = True
        self.bot.last_tick = 299
        self.api.position = 100
        self.drive(
            iter([dict(status="ACTIVE", tick=1)]), lambda _: self.fail("must not quote")
        )
        self.assertFalse(self.api.posts)

    def test_read_timeout_does_not_disable_next_round(self):
        seen = []

        def step(tick):
            seen.append(tick)
            if tick == 1:
                self.bot.request_stop()

        self.drive(
            iter(
                [
                    dict(status="ACTIVE", tick=299),
                    requests.Timeout("slow read"),
                    dict(status="STOPPED", tick=300),
                    dict(status="ACTIVE", tick=1),
                ]
            ),
            step,
        )
        self.assertEqual(seen, [299, 1])

    def test_once_exits_even_if_active_at_end(self):
        self.bot.c.repeat = False
        self.bot.started = True
        self.drive(
            iter([dict(status="ACTIVE", tick=300)]),
            lambda _: self.fail("must not step"),
        )

    def test_ctrl_c_inactive_with_inventory_exits_preserving_state(self):
        self.bot.started = True
        self.bot.expected = 100
        self.bot.save()
        self.bot.request_stop()
        self.drive(
            iter([dict(status="STOPPED", tick=300)]),
            lambda _: self.fail("must not step"),
        )
        self.assertEqual(self.bot.expected, 100)

    def test_can_quote_after_280_with_smaller_size(self):
        self.api.tick = 281
        self.bot.step(281)
        self.assertEqual({o["action"] for o in self.api.posts}, {"BUY", "SELL"})
        self.assertTrue(all(0 < o["quantity"] < 4000 for o in self.api.posts))

    def test_no_new_risk_at_290(self):
        self.api.tick = 290
        self.bot.step(290)
        self.assertFalse(self.api.posts)

    def test_taper_reduces_size_and_inventory_capacity(self):
        earlier = m.quotes(book(), 0, [], m.Config(), 100)
        later = m.quotes(book(), 0, [], m.Config(), 280)
        self.assertLess(later["BUY"][1], earlier["BUY"][1])
        self.assertNotIn("BUY", m.quotes(book(), 3000, [], m.Config(), 280))

    def test_high_latency_brings_closeout_forward(self):
        fast = self.bot.timing(16000)
        self.api.latency = 0.5
        slow = self.bot.timing(16000)
        self.assertLess(slow[0], fast[0])
        self.assertLess(slow[1], fast[1])
        self.api.tick = 280
        self.bot.expected = self.api.position = 16000
        self.bot.step(280)
        self.assertEqual(self.api.posts[-1]["type"], "MARKET")

    def test_outstanding_orders_count_in_closeout_budget(self):
        self.api.latency = 0.3
        self.assertLess(
            self.bot.timing(0, [order(quantity=16000)])[1], self.bot.timing(0)[1]
        )

    def test_candidate_order_must_fit_remaining_time(self):
        self.api.latency = 1
        self.api.tick = 280
        self.bot.submit("BUY", 4000, 10)
        self.assertFalse(self.api.posts)

    def test_detail_open_list_missing_keeps_capacity_reserved(self):
        self.bot.submit("BUY", 4000, 10)
        self.api.orders = lambda _: []
        _, opened = self.bot.reconcile()
        self.assertEqual(len(opened), 1)
        self.bot.submit("BUY", 4000, 10)
        self.assertEqual(len(self.api.posts), 1)

    def test_one_cent_behind_touch_reprices_after_patience(self):
        self.bot.submit("BUY", 4000, 9.99)
        with patch.object(
            m.time, "monotonic", return_value=self.bot.records["1"]["created"] + 2
        ):
            self.bot.step(50)
        self.assertEqual(self.api.rows[1]["status"], "CANCELLED")

    def test_one_cent_adverse_price_change_cancels(self):
        self.bot.submit("BUY", 4000, 10.02)
        with patch.object(
            m.time, "monotonic", return_value=self.bot.records["1"]["created"] + 1
        ):
            self.bot.step(50)
        self.assertEqual(self.api.rows[1]["status"], "CANCELLED")

    def test_no_cancel_writes_after_end(self):
        self.bot.submit("BUY", 4000, 10)
        self.api.tick = 300
        with self.assertRaises(m.VisibilityPending):
            self.bot.cancel()
        self.assertEqual(self.api.rows[1]["status"], "OPEN")

    def test_empty_state_is_explicitly_blocked(self):
        self.bot.path.write_text("")
        with self.assertRaisesRegex(m.Uncertain, "unreadable"):
            self.bot.initialize()

    def test_cancel_pending_only_blocks_replacement_on_its_side(self):
        self.bot.submit("BUY", 500, 10)
        self.bot.records["1"]["cancel_requested"] = m.time.monotonic()
        self.bot.step(50)
        self.assertEqual([p["action"] for p in self.api.posts], ["BUY", "SELL"])
        self.assertEqual(self.api.rows[1]["status"], "OPEN")
        self.assertIn("1", self.bot.records)

    def test_cancel_pending_cannot_quote_other_side_on_position_mismatch(self):
        self.bot.submit("BUY", 500, 10)
        self.bot.records["1"]["cancel_requested"] = m.time.monotonic()
        self.api.position = 100
        with self.assertRaises(m.VisibilityPending):
            self.bot.step(50)
        self.assertEqual(len(self.api.posts), 1)

    def test_adaptive_queue_improves_only_crowded_side(self):
        b = book()
        b["asks"][0]["price"] = 10.03
        b["bids"][0]["quantity"] = 10000
        base = m.quotes(b, 0, [], self.bot.c, 50)
        adjusted = m.queue_adjusted(base, b)
        self.assertEqual(adjusted["BUY"], (10.01, 4000))
        self.assertEqual(adjusted["SELL"], base["SELL"])
        self.assertFalse(self.bot.c.adaptive_queue)

    def test_adaptive_queue_can_clear_small_residual_in_crowded_book(self):
        b = book()
        b["asks"][0]["price"] = 10.02
        b["asks"][0]["quantity"] = 20000
        base = {"BUY": (10.00, 3500), "SELL": (10.02, 4000)}
        adjusted = m.queue_adjusted(base, b, position=380)
        self.assertEqual(adjusted, {"BUY": (10.00, 3500), "SELL": (10.01, 380)})
        self.assertEqual(m.queue_adjusted(base, b, position=501), base)

    def test_residual_order_rechecks_position_before_sending(self):
        self.bot.expected = self.api.position = 145
        self.bot.submit("SELL", 380, 10.02, reduce_only=True)
        self.assertEqual(self.api.posts[-1]["quantity"], 145)

    def test_two_level_quotes_split_existing_side_budget(self):
        self.bot.c.two_levels = True
        self.bot.step(50)
        expected = {
            "BUY": [(10.00, 1000), (10.01, 3000)],
            "SELL": [(10.04, 3000), (10.05, 1000)],
        }
        for side, levels in expected.items():
            self.assertEqual(
                sorted(
                    (o["price"], o["quantity"])
                    for o in self.api.posts
                    if o["action"] == side
                ),
                levels,
            )
        self.bot.step(50)
        self.assertEqual(len(self.api.posts), 4)

    def test_two_level_submission_keeps_reservations_and_caps_side_size(self):
        self.bot.c.two_levels = True
        self.bot.submit("BUY", 3000, 10, level=0)
        self.bot.records["1"]["cancel_requested"] = m.time.monotonic()
        self.bot.submit("BUY", 3000, 9.99, level=1)
        self.assertEqual(len(self.api.posts), 1)
        self.bot.records["1"].pop("cancel_requested")
        self.bot.submit("BUY", 3000, 9.99, level=1)
        self.bot.submit("BUY", 200, 9.98, level=1)
        self.assertEqual([o["quantity"] for o in self.api.posts], [3000, 1000])

    def test_ladder_reprices_one_side_without_starving_other_side(self):
        self.bot.c.two_levels = True
        self.bot.submit("BUY", 3000, 9.99, level=0)
        with patch.object(
            m.time, "monotonic", return_value=self.bot.records["1"]["created"] + 2
        ):
            self.bot.step(50)
        self.assertEqual(self.api.rows[1]["status"], "CANCELLED")
        self.assertEqual([o["action"] for o in self.api.posts], ["BUY", "SELL", "SELL"])

    def test_ladder_traces_missing_primary_and_rejects_inverted_levels(self):
        self.bot.c.two_levels = True
        self.bot.submit("BUY", 1000, 9.99, level=1)
        self.assertEqual(self.bot.ladder_blocks["BUY", 1], "awaiting_working_primary")
        self.bot.submit("BUY", 3000, 10, level=0)
        self.bot.submit("BUY", 1000, 9.99, level=1)
        self.bot.cancel(["1"])
        self.bot.submit("BUY", 3000, 9.98, level=0)
        self.assertEqual(self.bot.ladder_blocks["BUY", 0], "outer_quote_in_way")
        self.assertEqual(len(self.api.posts), 2)

    def test_ladder_can_quote_inside_crowded_three_cent_spread_after_fill_gap(self):
        self.bot.c.two_levels = True
        b = book()
        b["bids"][0]["quantity"] = 20000
        b["asks"][0]["price"] = 10.03
        self.api.book = lambda _: b
        with patch.object(m.time, "monotonic", return_value=self.bot.last_fill + 6):
            self.bot.step(50)
        buys = [
            (o["price"], o["quantity"]) for o in self.api.posts if o["action"] == "BUY"
        ]
        self.assertEqual(buys, [(10.01, 3000), (10.0, 1000)])

    def test_two_level_quotes_remove_extra_layers_when_taper_starts(self):
        self.bot.c.two_levels = True
        self.bot.step(50)
        self.api.tick = 240
        for _ in range(3):
            self.bot.step(240)
        self.assertEqual(sum(o["status"] == "OPEN" for o in self.api.rows.values()), 2)
        self.assertTrue(all(rec["level"] == 0 for rec in self.bot.records.values()))

    def test_two_level_quotes_block_added_layer_if_case_enters_taper(self):
        self.bot.c.two_levels = True
        self.bot.submit("BUY", 3000, 10, level=0)
        with patch.object(
            self.api,
            "case",
            side_effect=[
                {"status": "ACTIVE", "tick": 239},
                {"status": "ACTIVE", "tick": 240},
            ],
        ):
            self.bot.submit("BUY", 1000, 9.99, level=1)
        self.assertEqual(len(self.api.posts), 1)

    def test_aged_exit_joins_inside_only_to_reduce_inventory(self):
        b = book()
        b["asks"][0]["price"] = 10.02
        b["bids"][0]["quantity"] = 25000
        base = {"BUY": (10, 4000), "SELL": (10.02, 3000)}
        self.assertEqual(m.queue_adjusted(base, b, -3587), base)
        self.assertEqual(
            m.queue_adjusted(base, b, -3587, aged_exit=True),
            {"BUY": (10.01, 3587), "SELL": base["SELL"]},
        )
        protected = dict(base, BUY=(9.99, 4000))
        self.assertEqual(
            m.queue_adjusted(protected, b, -3587, aged_exit=True), protected
        )

    def test_aged_exit_only_after_fill_gap_and_never_trades_through_flat(self):
        b = book()
        b["asks"][0]["price"] = 10.02
        b["bids"][0]["quantity"] = 25000
        self.api.book = lambda _: b
        self.bot.c.aged_exit = True
        self.bot.last_fill = 100
        self.bot.expected = self.api.position = -3587
        with patch.object(
            m, "quotes", return_value={"BUY": (10, 4000), "SELL": (10.02, 3000)}
        ):
            with patch.object(m.time, "monotonic", return_value=109):
                self.bot.step(50)
            self.assertEqual(self.api.posts[0]["price"], 10)
            with patch.object(m.time, "monotonic", return_value=111):
                self.bot.step(51)
                self.bot.step(52)
        buys = [p for p in self.api.posts if p["action"] == "BUY"]
        self.assertEqual(
            [(p["price"], p["quantity"]) for p in buys], [(10, 4000), (10.01, 3587)]
        )

    def test_residual_order_does_not_open_short_when_position_is_already_flat(self):
        self.bot.submit("SELL", 380, 10.02, reduce_only=True)
        self.assertFalse(self.api.posts)

    def test_adaptive_queue_reprices_resting_touch_quote(self):
        self.bot.submit("BUY", 4000, 10)
        b = book()
        b["asks"][0]["price"] = 10.03
        b["bids"][0]["quantity"] = 10000
        self.api.book = lambda _: b
        self.bot.c.adaptive_queue = True
        with patch.object(
            m.time, "monotonic", return_value=self.bot.records["1"]["created"] + 6
        ):
            self.bot.step(50)
            self.assertEqual(self.api.rows[1]["status"], "CANCELLED")
            self.bot.step(51)
        self.assertEqual(
            [p["price"] for p in self.api.posts if p["action"] == "BUY"], [10, 10.01]
        )

    def test_primary_reprice_keeps_opposite_quote_live_during_cancel_wait(self):
        self.bot.submit("BUY", 4000, 9.98)
        original = self.api.call

        def delay_delete(method, path, **params):
            if method == "DELETE":
                return {}
            return original(method, path, **params)

        self.api.call = delay_delete
        with patch.object(
            m.time, "monotonic", return_value=self.bot.records["1"]["created"] + 2
        ):
            self.bot.step(50)
        self.assertEqual([p["action"] for p in self.api.posts], ["BUY", "SELL"])
        self.assertEqual(self.api.rows[1]["status"], "OPEN")
        self.assertIn("cancel_requested", self.bot.records["1"])

    def test_preflight_reports_dirty_saved_state_without_writes(self):
        self.bot.expected = -2223
        self.bot.save()
        snapshot = self.bot.path.read_bytes()
        with patch.object(self.bot, "log") as log:
            self.assertFalse(self.bot.preflight())
        self.assertEqual(log.call_args.args[0], "preflight_blocked")
        self.assertEqual(self.bot.path.read_bytes(), snapshot)
        self.assertFalse(self.api.posts)

    def test_observed_reset_blocks_dirty_previous_round_even_if_new_tick_passed_old(
        self,
    ):
        self.bot.started = True
        self.bot.last_tick = 5
        self.bot.expected = -2223
        self.drive(
            iter([dict(status="STOPPED", tick=0), dict(status="ACTIVE", tick=8)]),
            lambda tick: self.fail("must not quote on unresolved reset"),
        )
        self.assertEqual(self.bot.expected, -2223)
        self.assertFalse(self.api.posts)

    def test_fully_filled_order_is_not_cancelled_while_terminal_status_lags(self):
        self.bot.submit("BUY", 500, 10)
        self.api.rows[1]["quantity_filled"] = 500
        self.api.position = 500
        self.bot.reconcile()
        with self.assertRaises(m.CancellationPending):
            self.bot.cancel(["1"])
        self.assertEqual(self.api.rows[1]["status"], "OPEN")

    def test_limit_markout_uses_signed_difference_and_never_trades(self):
        self.bot.markouts.append((100, 1, "x", "SELL", 500, 10.04))
        self.bot.markouts.append((100, 5, "x", "SELL", 500, 10.04))
        with patch.object(m.time, "monotonic", return_value=101.1):
            with patch.object(self.bot, "log") as log:
                self.bot.observe_book(book())
        log.assert_called_once()
        self.assertAlmostEqual(log.call_args.kwargs["signed_mid_minus_limit"], 0.015)
        self.assertEqual(len(self.bot.markouts), 1)
        self.assertFalse(self.api.posts)

    def test_three_tick_book_does_not_give_away_both_ticks(self):
        b = book()
        b["asks"][0]["price"] = 10.03
        flat = m.quotes(b, 0, [], self.bot.c, 100)
        self.assertEqual((flat["BUY"][0], flat["SELL"][0]), (10, 10.03))
        long = m.quotes(b, 2000, [], self.bot.c, 100)
        self.assertEqual((long["BUY"][0], long["SELL"][0]), (10, 10.02))
        short = m.quotes(b, -2000, [], self.bot.c, 100)
        self.assertEqual((short["BUY"][0], short["SELL"][0]), (10.01, 10.03))

    def test_late_reducing_quote_cannot_flip_inventory(self):
        for position, side in [(35, "SELL"), (-35, "BUY")]:
            quotes = m.quotes(book(), position, [], self.bot.c, 281)
            self.assertEqual(quotes[side][1], 35)

    def test_cancel_skips_order_filled_since_last_snapshot(self):
        self.bot.submit("BUY", 500, 10)
        self.bot.records["1"]["seen"] = True
        self.api.rows[1].update(status="TRANSACTED", quantity_filled=500)
        self.api.position = 500
        with patch.object(self.api, "call", wraps=self.api.call) as call:
            self.bot.cancel(["1"])
        self.assertFalse(any(c.args[0] == "DELETE" for c in call.call_args_list))
        self.assertEqual(self.bot.expected, 500)
        self.assertFalse(self.bot.records)

    def test_fresh_flatten_order_is_not_cancelled_or_replaced(self):
        self.bot.expected = self.api.position = 300
        self.api.tick = 295
        self.bot.step(295)
        with self.assertRaises(m.CancellationPending):
            self.bot.step(295)
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(self.api.rows[1]["status"], "OPEN")

    def test_submit_skips_write_if_round_resets_during_book_read(self):
        with patch.object(
            self.api,
            "case",
            side_effect=[
                {"status": "ACTIVE", "tick": 281},
                {"status": "ACTIVE", "tick": 0},
            ],
        ):
            self.bot.submit("BUY", 500, 10)
        self.assertFalse(self.api.posts)

    def test_cancel_never_sends_old_order_id_after_tick_reset(self):
        self.bot.submit("BUY", 500, 10)
        self.bot.last_tick = 299
        self.api.tick = 0
        with self.assertRaises(m.VisibilityPending):
            self.bot.cancel()
        self.assertEqual(self.api.rows[1]["status"], "OPEN")

    def test_partial_fill_size_change_keeps_competitive_queue(self):
        self.bot.step(50)
        self.api.rows[1]["quantity_filled"] = 100
        self.api.position = 100
        with patch.object(self.bot, "cancel") as cancel:
            self.bot.step(51)
        cancel.assert_not_called()


if __name__ == "__main__":
    unittest.main()
