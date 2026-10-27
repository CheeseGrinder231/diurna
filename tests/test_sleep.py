from types import SimpleNamespace
import unittest

from diurna.collector import Collector, Context
from diurna.sleep import SleepDetector

SECOND = 1_000_000_000


class SleepTests(unittest.TestCase):
    def run_gap(self, context, gaps):
        clock = SimpleNamespace(now=0)
        rows = []
        collector = Collector(lambda: clock.now, rows.append, context)
        offsets = iter((value * SECOND, 0) for value in gaps)
        detector = SleepDetector(lambda: next(offsets))
        return clock, rows, collector, detector

    def test_fake_suspend_is_exact_gap_and_preserves_idle_and_lock(self):
        for context in (Context(app_class="foot"), Context(idle=True), Context(locked=True)):
            with self.subTest(context=context):
                clock, rows, collector, detector = self.run_gap(context, [0, 120, 120, 180])
                clock.now = 130 * SECOND  # ten awake seconds and 120 asleep
                detector.reconcile(collector)
                clock.now = 140 * SECOND
                detector.reconcile(collector)
                collector.checkpoint()
                clock.now = 200 * SECOND
                detector.reconcile(collector)
                self.assertEqual([(r.start_ns // SECOND, r.end_ns // SECOND, r.reason) for r in rows],
                                 [(0, 10, context.reason), (10, 130, "suspended"),
                                  (130, 140, context.reason), (140, 200, "suspended")])
                self.assertEqual(collector.context, context)
                self.assertEqual(sum(r.end_ns - r.start_ns for r in rows if r.reason == "suspended"),
                                 180 * SECOND)

    def test_explicit_sleep_hook_is_not_double_counted(self):
        clock, rows, collector, detector = self.run_gap(Context(locked=True), [0, 120])
        clock.now = 10 * SECOND
        collector.boundary(suspended=True)
        clock.now = 130 * SECOND
        detector.reconcile(collector)
        collector.boundary(suspended=False)
        clock.now = 140 * SECOND
        collector.checkpoint()
        self.assertEqual([(r.end_ns - r.start_ns, r.reason) for r in rows],
                         [(10 * SECOND, "locked"), (120 * SECOND, "suspended"),
                          (10 * SECOND, "locked")])

    def test_clock_read_noise_never_makes_sleep_or_overlapping_rows(self):
        values = iter([(0, 0), (100, 300), (-100, 300), (20, 100)])
        detector = SleepDetector(lambda: next(values))
        rows = []
        collector = Collector(lambda: 0, rows.append)
        for _ in range(3):
            detector.reconcile(collector)
        self.assertEqual(rows, [])
