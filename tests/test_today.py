from contextlib import redirect_stdout
import io
import json
import os
import sqlite3
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from diurna import bar, today
from diurna.__main__ import main
from diurna.collector import Span
from diurna.store import Store

SECOND = 1_000_000_000
MINUTE = 60 * SECOND
# 2026-09-20 00:00 UTC; tests that need local midnight pin TZ=UTC.
MIDNIGHT = 20716 * 86400 * SECOND
FIXTURE = json.loads((Path(__file__).parent / "fixtures/day.json").read_text())
TIMELINES = json.loads((Path(__file__).parent / "fixtures/timelines.json").read_text())


def expand(spans):
    """Fixture spans as stored rows; a sixth element splits a span into equal rows."""
    rows = []
    for start, end, app, workspace, reason, *pieces in spans:
        count = pieces[0] if pieces else 1
        cuts = [MIDNIGHT + start * SECOND + (end - start) * SECOND * i // count
                for i in range(count)] + [MIDNIGHT + end * SECOND]
        rows += [(a, b, app, workspace, reason, None) for a, b in zip(cuts, cuts[1:])]
    return rows


def as_rows(timeline, unit):
    return [[(r["start_ns"] - MIDNIGHT) // unit, (r["end_ns"] - MIDNIGHT) // unit,
             r["state"], r["app_class"],
             r["workspace"], r["switches"]] for r in timeline]


def assert_tiles(test, summary):
    """Rows run from midnight to now with no gaps or overlaps, and agree with the totals."""
    timeline = summary["timeline"]
    test.assertEqual(timeline[0]["start_ns"], summary["start_ns"])
    test.assertEqual(timeline[-1]["end_ns"], summary["end_ns"])
    test.assertTrue(all(a["end_ns"] == b["start_ns"] for a, b in zip(timeline, timeline[1:])))
    test.assertTrue(all(r["end_ns"] > r["start_ns"] for r in timeline))
    test.assertEqual(sum(r["end_ns"] - r["start_ns"] for r in timeline),
                     summary["end_ns"] - summary["start_ns"])
    by_state = {}
    for row in timeline:
        by_state[row["state"]] = by_state.get(row["state"], 0) + row["end_ns"] - row["start_ns"]
        test.assertEqual(sum(a["active_ns"] for a in row["apps"]),
                         row["end_ns"] - row["start_ns"] if row["state"] == "active" else 0)
    expected = {k.removesuffix("_ns"): v for k, v in summary["states"].items() if v}
    expected.update({k: summary[f"{k}_ns"] for k in ("not_recorded", "pending")
                     if summary[f"{k}_ns"]})
    test.assertEqual(by_state, expected)


def fixture_database(directory):
    path = Path(directory) / "spans.sqlite3"
    store = Store(path)
    try:
        for start, end, app, workspace, reason in FIXTURE["spans"]:
            store.write(Span(MIDNIGHT + start * MINUTE, MIDNIGHT + end * MINUTE,
                             app, workspace, reason))
    finally:
        store.close()
    return path


class FixedZone:
    def __init__(self, zone):
        self.zone = zone

    def __enter__(self):
        self.patch = patch.dict(os.environ, {"TZ": self.zone})
        self.patch.start()
        time.tzset()

    def __exit__(self, *_):
        self.patch.stop()
        time.tzset()


class TodayTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = fixture_database(self.directory)
        self.now = MIDNIGHT + FIXTURE["now"] * MINUTE

    def test_fixture_day_totals_clip_midnight_and_now(self):
        with FixedZone("UTC"):
            summary = today.today(self.path, self.now)
        expected = FIXTURE["expected"]
        self.assertEqual(summary["date"], "2026-09-20")
        self.assertEqual((summary["start_ns"], summary["end_ns"]), (MIDNIGHT, self.now))
        self.assertEqual(summary["active_ns"], expected["active"] * MINUTE)
        self.assertEqual(summary["states"], {f"{k}_ns": v * MINUTE
                                             for k, v in expected["states"].items()})
        self.assertEqual(summary["not_recorded_ns"], expected["not_recorded"] * MINUTE)
        self.assertEqual(summary["pending_ns"], expected["pending"] * MINUTE)
        self.assertEqual([(a["app_class"], a["active_ns"]) for a in summary["apps"]],
                         [(app, minutes * MINUTE) for app, minutes in expected["apps"]])
        self.assertEqual([(w["app_class"], w["workspace"], w["title"], w["active_ns"])
                          for w in summary["windows"]],
                         [(app, ws, None, m * MINUTE) for app, ws, m in expected["windows"]])
        self.assertEqual(sum(summary["states"].values()) + summary["not_recorded_ns"]
                         + summary["pending_ns"], self.now - MIDNIGHT)
        self.assertEqual(as_rows(summary["timeline"], MINUTE), expected["timeline"])
        assert_tiles(self, summary)

    def test_fixture_day_bar_text_and_tooltip(self):
        summary = today.summarize(today.rows(self.path, MIDNIGHT, self.now), MIDNIGHT, self.now)
        self.assertEqual(bar.bar(summary), FIXTURE["bar"])

    def test_cli_json_and_bar_match_the_fixture(self):
        with FixedZone("UTC"), patch("diurna.__main__.time.time_ns", return_value=self.now):
            outputs = []
            for argv in (["today", "--json"], ["bar"], ["today"]):
                stream = io.StringIO()
                with patch.object(sys, "argv", ["diurna", *argv, "--database", str(self.path)]), \
                        redirect_stdout(stream):
                    self.assertEqual(main(), 0)
                outputs.append(stream.getvalue())
        self.assertEqual(json.loads(outputs[0])["active_ns"], FIXTURE["expected"]["active"] * MINUTE)
        self.assertEqual(json.loads(outputs[1]), FIXTURE["bar"])
        self.assertIn("code · ws 3", outputs[2])
        self.assertEqual(json.loads(outputs[0])["timeline"][0],
                         {"start_ns": MIDNIGHT, "end_ns": MIDNIGHT + 30 * MINUTE,
                          "state": "active", "app_class": "foot", "workspace": "1",
                          "switches": 0,
                      "activity": {"hands_on_ns": 0, "watching_ns": 0, "on_screen_ns": 0},
                      "apps": [{"app_class": "foot", "workspace": "1",
                                                   "active_ns": 30 * MINUTE}]})
        self.assertIn("Asleep        7h 30m", outputs[2])
        self.assertIn("Unknown          20m", outputs[2])
        self.assertIn("Not saved        10m", outputs[2])
        self.assertNotIn("Not recorded", outputs[2])
        timeline = outputs[2][outputs[2].index("\nTimeline\n"):].splitlines()[2:]
        self.assertEqual(timeline, [
            "  00:00–00:30      30m  foot · ws 1",
            "  00:30–08:00   7h 30m  asleep",
            "  08:00–08:05       5m  locked",
            "  08:05–10:00   1h 55m  firefox · ws 2",
            "  10:00–10:15      15m  idle",
            "  10:15–11:40   1h 25m  code · ws 3",
            "  11:40–12:00      20m  unknown (collector unavailable)",
            "  12:00–13:00    1h 0m  firefox · ws web",
            "  13:00–13:20      20m  slack · ws 4",
            "  13:20–13:35      15m  spotify · ws 5",
            "  13:35–13:45      10m  mpv · ws 5",
            "  13:45–13:50       5m  (desktop) · ws 1",
            "  13:50–15:50    2h 0m  code · ws 3",
            "  15:50–16:00      10m  not saved (is the collector running?)",
        ])

    def test_legacy_views_and_cli_bytes_are_identical_after_identity_migration(self):
        for version in (1, 2):
            path = self.directory / f"legacy-{version}.db"
            original = sqlite3.connect(self.path)
            db = sqlite3.connect(path)
            try:
                columns = "id,start_ns,end_ns,app_class,workspace,active,reason,title"
                if version == 2:
                    columns += ",input,media,inhibiting"
                # An independently anonymous copy, never a downgraded real archive.
                db.execute("CREATE TABLE spans AS SELECT " + columns + " FROM "
                           "(SELECT 0 id, 0 start_ns, 1 end_ns, '' app_class, '' workspace, "
                           "1 active, 'active' reason, NULL title, NULL input, "
                           "NULL media, NULL inhibiting) WHERE 0")
                data = original.execute(f"SELECT {columns} FROM spans").fetchall()
                db.executemany("INSERT INTO spans VALUES (" + ",".join("?" for _ in data[0])
                               + ")", data)
                if version == 2:
                    db.execute("UPDATE spans SET input=0, media=1, inhibiting=0 "
                               "WHERE reason='active'")
                db.execute(f"PRAGMA user_version={version}")
                db.commit()
            finally:
                original.close()
                db.close()

            def views():
                with FixedZone("UTC"), patch("diurna.__main__.time.time_ns", return_value=self.now):
                    outputs = []
                    for argv in (["today", "--json"], ["today"], ["bar"]):
                        stream = io.StringIO()
                        with patch.object(sys, "argv", ["diurna", *argv, "--database", str(path)]), \
                                redirect_stdout(stream):
                            self.assertEqual(main(), 0)
                        outputs.append(stream.getvalue())
                    return (today.rows(path, MIDNIGHT, self.now), outputs)

            before = views()
            Store(path).close()
            self.assertEqual(views(), before)

    def test_missing_database_is_an_empty_day_and_bad_schema_is_unavailable(self):
        missing = self.directory / "missing.sqlite3"
        summary = today.summarize(today.rows(missing, 0, MINUTE), 0, MINUTE)
        self.assertEqual((summary["active_ns"], summary["pending_ns"]), (0, MINUTE))
        self.assertNotIn("Not saved", bar.render(summary))  # under the stale bound
        gap = today.summarize([(0, MINUTE, "a", "1", "active", None),
                               (5 * MINUTE, 6 * MINUTE, "a", "1", "active", None)], 0, 6 * MINUTE)
        self.assertEqual((gap["not_recorded_ns"], gap["pending_ns"]), (4 * MINUTE, 0))
        self.assertIn("Not recorded      4m", bar.render(gap))
        with FixedZone("UTC"):
            self.assertIn("00:01–00:05       4m  not recorded (Diurna not running)",
                          bar.render(gap))
        self.assertFalse(missing.exists())
        self.assertEqual(bar.bar(summary)["class"], ["diurna", "empty"])
        with Store(self.directory / "future.sqlite3").db as db:
            db.execute("PRAGMA user_version=99")
        db.close()
        stream = io.StringIO()
        with patch.object(sys, "argv", ["diurna", "bar", "--database",
                                        str(self.directory / "future.sqlite3")]), \
                redirect_stdout(stream):
            self.assertEqual(main(), 0)
        self.assertEqual(json.loads(stream.getvalue()), bar.unavailable())

    def test_bar_tracks_restart_gap_before_first_focus_change_or_checkpoint(self):
        path = self.directory / "restart.sqlite3"
        store = Store(path)
        try:
            store.write(Span(0, MINUTE, "foot", "1", "active"))
            # A sub-second restart gap displays as 0 s but is still stored.
            restarted = MINUTE + SECOND // 2
            store.resume(restarted)
        finally:
            store.close()
        now = restarted + 16 * SECOND
        summary = today.summarize(today.rows(path, 0, now), 0, now)
        self.assertEqual([row["state"] for row in summary["timeline"]],
                         ["active", "unknown", "pending"])
        self.assertEqual(summary["pending_ns"], 16 * SECOND)
        module = bar.bar(summary)
        self.assertEqual(module["text"], "1m")
        self.assertEqual(module["alt"], "tracking")
        self.assertEqual(module["class"], ["diurna"])
        self.assertNotIn("not recording", module["tooltip"])

    def test_bar_freshness_is_independent_of_last_saved_state(self):
        with patch.object(bar, "policy", return_value={"safety_seconds": 60}):
            for reason in today.REASONS:
                for pending in (0, 16 * SECOND, bar.STALE_NS - 1,
                                bar.STALE_NS, bar.STALE_NS + SECOND):
                    with self.subTest(reason=reason, pending=pending):
                        saved = [(0, MINUTE, "foot", "1", reason, None)]
                        module = bar.bar(today.summarize(saved, 0, MINUTE + pending))
                        stale = pending >= bar.STALE_NS
                        self.assertEqual("not recording" in module["text"], stale)
                        self.assertEqual("not recording" in module["tooltip"], stale)
                        self.assertEqual("unavailable" in module["class"], stale)
                        self.assertEqual(module["alt"], "unavailable" if stale else
                                         "tracking" if reason == "active" else "empty")

    def test_bar_stale_tail_respects_checkpoint_interval_and_recovers(self):
        stopped = [(0, MINUTE, "foot", "1", "active", None)]
        with patch.object(bar, "policy", return_value={"safety_seconds": 300}):
            for pending, stale in ((2 * MINUTE, False), (10 * MINUTE - 1, False),
                                   (10 * MINUTE, True), (10 * MINUTE + SECOND, True)):
                with self.subTest(pending=pending):
                    module = bar.bar(today.summarize(stopped, 0, MINUTE + pending))
                    self.assertEqual("unavailable" in module["class"], stale)
            restarted = stopped + [(MINUTE, 12 * MINUTE, "", "", "unknown", None)]
            module = bar.bar(today.summarize(restarted, 0, 12 * MINUTE + SECOND))
            self.assertEqual(module["alt"], "tracking")
        self.assertIn("not recording", bar.bar(today.summarize([], 0, 3 * MINUTE))["text"])

    def test_timeline_fixture_days(self):
        for name, day in TIMELINES["days"].items():
            with self.subTest(day=name):
                now = MIDNIGHT + day["now"] * SECOND
                summary = today.summarize(expand(day["spans"]), MIDNIGHT, now)
                self.assertEqual(as_rows(summary["timeline"], SECOND), day["expected"])
                assert_tiles(self, summary)

    def test_timeline_view_rows_label_switching_and_the_early_gap(self):
        day = TIMELINES["days"]["collection_starts_mid_day_with_a_no_op_burst"]
        summary = today.summarize(expand(day["spans"]), MIDNIGHT,
                                  MIDNIGHT + day["now"] * SECOND)
        with FixedZone("UTC"):
            screen = bar.render(summary)
        self.assertIn("  Not recorded  8h 41m", screen)
        self.assertTrue(screen.endswith(
            "Timeline\n"
            "  00:00–08:41   8h 41m  not recorded (Diurna not running)\n"
            "  08:41–09:04      22m  foot · ws 2 (+2 switches)\n"
            "  09:04–09:05       1m  now, not saved yet"), screen)
        switching = TIMELINES["days"]["rapid_switching"]
        summary = today.summarize(expand(switching["spans"]), MIDNIGHT,
                                  MIDNIGHT + switching["now"] * SECOND)
        with FixedZone("UTC"):
            screen = bar.render(summary)
        self.assertIn("  09:10–09:20      10m  mixed · firefox 5m, slack 5m (+29 switches)",
                      screen)
        self.assertIn("  09:20–09:26       6m  code · ws 3 (+2 switches)", screen)

    def test_day_bounds_follow_local_midnight_across_dst(self):
        with FixedZone("UTC"):
            self.assertEqual(today.day_bounds(self.now)[1:],
                             (MIDNIGHT, MIDNIGHT + 86400 * SECOND))
        with FixedZone("America/New_York"):
            # 2026-03-08 is 23 hours long in New York.
            day, start, end = today.day_bounds(1772978400 * SECOND)
            self.assertEqual(str(day), "2026-03-08")
            self.assertEqual(end - start, 23 * 3600 * SECOND)

    def test_durations_and_markup_free_text(self):
        self.assertEqual([bar.duration(n * SECOND) for n in (0, 59, 60, 3599, 3600, 3660)],
                         ["0m", "0m", "1m", "59m", "1h 0m", "1h 1m"])
        summary = today.summarize([(0, 2 * MINUTE, "<b>&", "1", "active", None)], 0, 2 * MINUTE)
        # The Waybar module sets "escape": true; the tooltip stays raw text here.
        self.assertIn("<b>&  2m", bar.bar(summary)["tooltip"])


class WatchTests(unittest.TestCase):
    def test_watch_redraws_on_each_change_and_exits_130_on_interrupt(self):
        summaries = iter([
            today.summarize([(0, MINUTE, "foot", "1", "active", None)], 0, MINUTE),
            today.summarize([(0, MINUTE, "foot", "1", "active", None),
                             (MINUTE, 3 * MINUTE, "firefox", "2", "active", None)], 0, 3 * MINUTE),
        ])

        class Changes:
            waits, closed = [], False

            def wait(self, timeout):
                self.waits.append(timeout)
                if len(self.waits) == 2:
                    raise KeyboardInterrupt

            def close(self):
                self.closed = True

        screens, watcher = [], Changes()
        self.assertEqual(bar.watch(lambda: next(summaries), screens.append,
                                   lambda: "12:00", watcher), 130)
        self.assertEqual(len(screens), 2)
        self.assertIn("foot", screens[0])
        self.assertNotIn("firefox", screens[0])
        self.assertIn("firefox · ws 2", screens[1])
        self.assertEqual(watcher.waits, [60, 60])
        self.assertTrue(watcher.closed)

    def test_inotify_watcher_wakes_on_the_change_stamp(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "diurna"
            watcher = bar.Watcher(directory)
            self.addCleanup(watcher.close)
            self.assertGreaterEqual(watcher.fd, 0)
            self.assertEqual(bar.select.select([watcher.fd], [], [], 0)[0], [])
            (directory / "changed").write_text("1\n")
            self.assertEqual(bar.select.select([watcher.fd], [], [], 0)[0], [watcher.fd])
            watcher.wait(0)  # drains the queued change
            self.assertEqual(bar.select.select([watcher.fd], [], [], 0)[0], [])
