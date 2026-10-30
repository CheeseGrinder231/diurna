"""Synthetic complete-context equivalence and conservative seal witnesses."""
import itertools
import unittest
from unittest.mock import patch

from diurna import screen_classifier as frozen
from diurna.today import classify as view_classify

S = 1_000_000_000


def row(start, end, app="a", reason="active", input_=0, media=0, inhibiting=0):
    return (start, end, app, "workspace", reason, None, input_, media, inhibiting)


class ClassifierTests(unittest.TestCase):
    def test_complete_context_matches_inspected_default_for_all_stored_combinations(self):
        variants = list(itertools.product(
            ("a", frozen.SCREENSAVER),
            ("active", "idle", "locked", "suspended", "unknown", "disconnected"),
            (None, 0, 1), (None, 0, 1), (None, 0, 1)))
        for app, reason, input_, media, inhibitor in variants:
            with self.subTest(app=app, reason=reason, signals=(input_, media, inhibitor)):
                spans = [row(1, 2, reason="idle"),
                         row(2, 3, app, reason, input_, media, inhibitor),
                         row(3, 11*S+3, frozen.SCREENSAVER),
                         row(11*S+3, 12*S, "b"), row(12*S, 13*S, input_=1)]
                self.assertEqual(frozen.classify(spans), view_classify(spans, 10*S))
                # Future hot configuration/UI implementation cannot affect it.
                with patch('diurna.today.classify', side_effect=AssertionError):
                    self.assertEqual(len(frozen.classify(spans)), len(spans))
        legacy = [(1, 2, "a", "w", "active", None)]
        self.assertEqual(frozen.classify(legacy), view_classify(legacy))

    def test_grace_counterexamples_and_no_age_or_calendar_seals(self):
        for offset, duration in ((1, 60*S), (16*3600*S-60*S, 60*S), (1, 12*3600*S)):
            idle = 3600*S if duration > 60*S else 30*S
            spans = [row(offset, offset+duration),
                     row(offset+duration, offset+duration+idle, reason="idle"),
                     row(offset+duration+idle, offset+duration+idle+10*S,
                         frozen.SCREENSAVER)]
            self.assertEqual(frozen.classify(spans)[0][4], "active")
            self.assertEqual(frozen.barriers(spans), [])
            continued = spans + [row(spans[-1][1], spans[-1][1]+1, frozen.SCREENSAVER)]
            self.assertEqual(frozen.classify(continued)[0][4], "idle")
            self.assertEqual(frozen.barriers(continued), [])

    def test_every_issued_watermark_is_invariant_under_supported_future_appends(self):
        variants = list(itertools.product(
            ("a", frozen.SCREENSAVER),
            ("active", "idle", "locked", "suspended", "unknown", "disconnected"),
            (None, 0, 1), (None, 0, 1), (None, 0, 1)))
        base = [row(1, 60*S), row(60*S, 90*S, reason="idle")]
        for app, reason, input_, media, inhibiting in variants:
            for gap in (0, 1):
                barrier = row(90*S+gap, 91*S+gap, app, reason, input_, media, inhibiting)
                prefix = base + [barrier]
                evidence = frozen.barriers(prefix)
                if not evidence:
                    self.assertEqual(gap, 0)
                    continue
                watermark = evidence[-1][0]
                before = [r for r in frozen.classify(prefix) if r[1] <= watermark]
                end = barrier[1]
                for future_app, future_reason, future_input in itertools.product(
                        ("b", frozen.SCREENSAVER), ("active", "unknown", "idle"), (None, 0, 1)):
                    for length in (1, 10*S, 10*S+1):
                        future = [row(end, end+length, future_app, future_reason, future_input),
                                  row(end+length, end+length+11*S, frozen.SCREENSAVER),
                                  row(end+length+11*S, end+length+12*S, input_=1)]
                        after = [r for r in frozen.classify(prefix+future) if r[1] <= watermark]
                        self.assertEqual(before, after, (barrier, future, evidence))

    def test_screensaver_flags_never_supply_generic_barrier(self):
        for reason, input_ in itertools.product(("active", "unknown", "disconnected"), (None, 1)):
            spans = [row(1, 2, frozen.SCREENSAVER, reason, input_)]
            self.assertEqual(frozen.barriers(spans), [])
        self.assertEqual(frozen.barriers([row(1, 2), row(3, 4, frozen.SCREENSAVER)])[0][:3],
                         (3, "gap", 2))

    def test_media_fallback_and_away_input_gap_legacy_recovery(self):
        spans = [row(1, 2, media=None, inhibiting=1), row(2, 3, media=0, inhibiting=1),
                 row(3, 4, reason="idle"), row(4, 5, media=1), row(5, 6, input_=1),
                 row(6, 7, media=1), row(7, 8, reason="locked"), row(9, 10),
                 row(10, 11, reason="idle"), row(11, 12, input_=None), row(12, 13)]
        out = frozen.classify(spans)
        self.assertEqual(out[0][6], "watching")
        self.assertEqual(out[1][6], "on_screen")
        self.assertTrue(out[3][7])
        self.assertEqual(out[5][6], "watching")
        self.assertEqual(out[7][6], "on_screen")
        self.assertIsNone(out[9][6])
        self.assertEqual(out[10][6], "on_screen")
