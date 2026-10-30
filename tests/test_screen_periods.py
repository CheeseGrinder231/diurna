import unittest

from diurna.screen_classifier import barriers, SCREENSAVER
from diurna.screen_periods import periods, fold, _label, _cut, DAY_NS, FOLD_NS

S = 1_000_000_000


def row(start, end, app="a", reason="active", input_=1, workspace="w"):
    return (start, end, app, workspace, reason, None, input_, 0, 0)


def grouped(spans):
    evidence = barriers(spans)
    return periods(spans, evidence[-1][0] if evidence else None)


def frow(start, length, app):
    return [start, start+length, {app: length}]


class PeriodTests(unittest.TestCase):
    def test_strict_threshold_lone_short_and_no_workspace_or_signal_splits(self):
        exact = fold([frow(1, FOLD_NS, "a"), frow(1+FOLD_NS, FOLD_NS, "b")])
        self.assertEqual(len(exact), 2)
        short = fold([frow(1, FOLD_NS-1, "a"), frow(FOLD_NS, FOLD_NS, "b")])
        self.assertEqual(len(short), 1)
        self.assertEqual(_label(short[0]), "b")
        self.assertEqual(fold([frow(1, 1, "a")]), [frow(1, 1, "a")])
        spans = [row(1, 10*S), row(10*S, 20*S, workspace="different"),
                 row(20*S, 30*S, input_=0), row(30*S, 31*S, reason="unknown", input_=0)]
        found = grouped(spans)
        self.assertEqual([(p.start_ns, p.end_ns, p.application) for p in found], [(1, 30*S, "a")])

    def test_shortest_first_both_neighbor_and_earliest_ties(self):
        # The central blip joins both a neighbors, despite the first b being short.
        rows = [frow(0, 200*S, "a"), frow(200*S, 50*S, "b"),
                frow(250*S, 200*S, "a")]
        found = fold(rows)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][2], {"a": 400*S, "b": 50*S})
        rows = [frow(0, 200*S, "a"), frow(200*S, S, "b"), frow(201*S, 200*S, "c")]
        found = fold(rows)
        self.assertEqual(found[0][2], {"a": 200*S, "b": S})
        # Equal short lengths choose the earliest; neighbor lengths tie to left.
        found = fold([frow(0, 100*S, "a"), frow(100*S, 100*S, "b"),
                      frow(200*S, 200*S, "c")])
        self.assertEqual(found[0][2], {"a": 100*S, "b": 100*S})
        self.assertIsNone(_label(found[0]))

    def test_strict_half_mixed_and_final_adjacent_equal_label_merge(self):
        self.assertIsNone(_label([0, 2, {"終": 1, "a": 1}]))
        self.assertEqual(_label([0, 3, {"終": 2, "a": 1}]), "終")
        found = fold([[0, 200*S, {"a": 100*S, "b": 100*S}],
                      [200*S, 400*S, {"c": 100*S, "d": 100*S}]])
        self.assertEqual(len(found), 1)
        self.assertIsNone(_label(found[0]))

    def test_input_seal_does_not_split_open_run(self):
        spans = [row(1, 200*S), row(200*S, 400*S, "b")]
        self.assertEqual(grouped(spans), [])
        spans.append(row(400*S, 401*S, reason="idle"))
        self.assertEqual([p.end_ns for p in grouped(spans)], [200*S, 400*S])

    def test_midnight_cut_waits_and_preserves_away_context(self):
        midnight = 16*3600*S  # 1970-01-02 00:00 in Taipei
        spans = [row(midnight-60*S, midnight+20*S, input_=0),
                 row(midnight+20*S, midnight+30*S, SCREENSAVER, input_=0)]
        self.assertEqual(grouped(spans), [])
        spans.append(row(midnight+30*S, midnight+30*S+1, SCREENSAVER, input_=0))
        spans.append(row(midnight+30*S+1, midnight+40*S, "b", input_=0))
        spans.append(row(midnight+40*S, midnight+50*S, input_=1))
        self.assertEqual(grouped(spans), [])  # Earlier day became away too.
        stable = [row(midnight-200*S, midnight+200*S),
                  row(midnight+200*S, midnight+201*S, reason="unknown", input_=0)]
        found = grouped(stable)
        self.assertEqual([(p.start_ns, p.end_ns) for p in found],
                         [(midnight-200*S, midnight), (midnight, midnight+200*S)])
        self.assertEqual(found[0].closure_kind, "policy")
        self.assertTrue(all(p.end_ns-p.domain_start_ns <= DAY_NS for p in found))

    def test_gap_and_inactive_do_not_fill_or_absorb_missing_time(self):
        spans = [row(1, 2), row(10, 11), row(11, 12, reason="unknown", input_=0)]
        self.assertEqual([(p.start_ns, p.end_ns) for p in grouped(spans)], [(1, 2), (10, 11)])
        self.assertEqual(grouped([row(1, 2) , row(2, 3, reason="idle", input_=0)]), [])

    def test_elapsed_ceiling_even_on_historical_long_calendar_day(self):
        # Taipei ended DST on 1979-09-30: a 25-hour policy day.
        start = 307465200*S  # 1979-09-30T00:00:00+09:00
        end = start+25*3600*S
        self.assertEqual(_cut(start), start+DAY_NS)
        spans = [row(start, end), row(end, end+1, reason="unknown", input_=0)]
        self.assertEqual([(p.start_ns, p.end_ns) for p in grouped(spans)],
                         [(start, start+DAY_NS), (start+DAY_NS, end)])
