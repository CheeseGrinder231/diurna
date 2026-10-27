import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from diurna.collector import Collector, Context, Span
from diurna.runtime import HOOKS
from diurna.store import Store

SECOND = 1_000_000_000


class FakeClock:
    now = 0

    def __call__(self):
        return self.now


class ReplayTests(unittest.TestCase):
    def test_recorded_stream_exact_spans_in_sqlite(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/desktop.json").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "spans.sqlite3"
            store = Store(path)
            try:
                clock = FakeClock()
                collector = Collector(clock, store.write)
                collector.snapshot(*fixture["initial"])
                for seconds, source, data in fixture["events"]:
                    clock.now = seconds * SECOND
                    if source == "hypr":
                        self.assertTrue(collector.hyprland(data))
                    elif source == "hook":
                        flag, value = HOOKS[data]
                        collector.boundary(**{flag: value})
                    else:
                        collector.checkpoint()
                rows = store.db.execute("SELECT start_ns,end_ns,app_class,workspace,reason FROM spans ORDER BY id").fetchall()
                expected = [(a * SECOND, b * SECOND, app, ws, reason)
                            for a, b, app, ws, reason in fixture["expected"]]
                self.assertEqual(rows, expected)
                self.assertEqual(sum(b - a for a, b, *_ in rows), 120 * SECOND)
                self.assertTrue(all(a[1] == b[0] for a, b in zip(rows, rows[1:])))
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM spans WHERE title IS NOT NULL").fetchone(), (0,))
                self.assertEqual(store.db.execute("PRAGMA integrity_check").fetchone(), ("ok",))
            finally:
                store.close()
            self.assertNotIn(b"PRIVATE", path.read_bytes())

    def test_restart_marks_unknown_without_overlapping_or_inventing_activity(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "spans.sqlite3"
            store = Store(path)
            store.write(Span(0, 5, "foot", "1", "active"))
            store.close()
            store = Store(path)
            try:
                store.resume(20)
                store.write(Span(20, 25, "firefox", "2", "active"))
                self.assertEqual(store.db.execute("SELECT start_ns,end_ns,reason FROM spans").fetchall(),
                                 [(0, 5, "active"), (5, 20, "unknown"), (20, 25, "active")])
                with self.assertRaises(ValueError):
                    store.resume(10)
                with self.assertRaises(ValueError):
                    store.write(Span(24, 30, "", "", "active"))
                with self.assertRaises(sqlite3.IntegrityError):
                    store.db.execute("UPDATE spans SET title = 'private'")
            finally:
                store.close()

    def test_overlap_flags_do_not_resume_activity_while_still_locked(self):
        clock, spans = FakeClock(), []
        collector = Collector(clock, spans.append, Context(locked=True, suspended=True))
        clock.now = 10
        collector.boundary(suspended=False)
        clock.now = 20
        collector.boundary(idle=False)
        clock.now = 30
        collector.boundary(locked=False)
        clock.now = 40
        collector.checkpoint()
        self.assertEqual([(s.start_ns, s.reason) for s in spans],
                         [(0, "suspended"), (10, "locked"), (30, "active")])

    def test_no_op_event_burst_is_one_span(self):
        # A terminal retitling itself re-sends activewindow for the same window
        # many times a second; none of it changes (app, workspace, reason).
        clock, spans = FakeClock(), []
        collector = Collector(clock, spans.append)
        collector.snapshot({"class": "foot", "address": "0xaa"}, "1",
                           [{"class": "foot", "address": "0xaa"}])
        for tick in range(1, 2001):
            clock.now = tick * SECOND // 10
            for line in ("activewindow>>foot,PRIVATE TITLE %d" % tick, "activewindowv2>>aa",
                         "workspace>>1", "focusedmon>>DP-1,1", "openwindow>>cc,1,foot,x",
                         "closewindow>>cc", "movewindowv2>>cc,1,1"):
                self.assertTrue(collector.hyprland(line))
            collector.boundary(idle=False, locked=False)
        self.assertEqual(spans, [])
        clock.now = 300 * SECOND
        collector.hyprland("activewindow>>firefox,PRIVATE")
        collector.hyprland("activewindowv2>>bb")
        self.assertEqual(spans, [Span(0, 300 * SECOND, "foot", "1", "active")])
        # A flag that does not change the recorded reason is not a boundary either.
        collector.boundary(locked=True)
        clock.now = 310 * SECOND
        collector.boundary(idle=True)
        clock.now = 320 * SECOND
        collector.checkpoint()
        collector.checkpoint()
        self.assertEqual(spans[1:], [Span(300 * SECOND, 320 * SECOND, "firefox", "1", "locked")])
        self.assertEqual(collector.context.reason, "locked")

    def test_empty_focus_move_close_and_malformed_input(self):
        clock, spans = FakeClock(), []
        collector = Collector(clock, spans.append)
        collector.snapshot({"class": "foot", "address": "0xaa"}, "1", [])
        for data in ("bogus", "openwindow>>short", "workspacev2>>short", "activewindow>>short", "windowtitle>>aa"):
            self.assertFalse(collector.hyprland(data))
        clock.now = 1
        collector.hyprland("movewindowv2>>aa,3,dev")
        self.assertEqual(collector.context.workspace, "dev")
        clock.now = 2
        collector.hyprland("activewindow>>new-class,title")
        collector.hyprland("activewindowv2>>aa")
        self.assertEqual(collector.context.app_class, "new-class")
        collector.hyprland("closewindow>>aa")
        self.assertEqual(collector.context.app_class, "")
        clock.now = 1
        with self.assertRaises(ValueError):
            collector.boundary()

    def test_future_schema_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "future.sqlite3"
            with sqlite3.connect(path) as db:
                db.execute("PRAGMA user_version=99")
            db.close()
            with self.assertRaises(ValueError):
                Store(path)
