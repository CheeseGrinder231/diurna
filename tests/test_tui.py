import curses
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch

from diurna import bar, today, tui
from diurna.__main__ import main
from test_today import FIXTURE, MIDNIGHT, MINUTE, SECOND, TIMELINES, FixedZone, expand, \
    fixture_database

FIXTURES = Path(__file__).parent / "fixtures"
BLIPS = json.loads((FIXTURES / "blips.json").read_text())
MILLISECOND = SECOND // 1000
SIZES = ((24, 80), (20, 60))


def screen(view):
    """The view as plain text, one line per row, trailing blanks dropped."""
    return ["".join(text for text, _ in line).rstrip() for line in view.lines()]


def snapshots():
    """Expected screens from fixtures/screens.txt, keyed "Tab WxH"."""
    sections, name = {}, None
    for line in (FIXTURES / "screens.txt").read_text().splitlines():
        if line.startswith("== "):
            name = line[3:]
            sections[name] = []
        else:
            sections[name].append(line[1:-1].rstrip())  # between the | borders
    return sections


def blip_summary():
    spans = [(MIDNIGHT + start * MILLISECOND, MIDNIGHT + end * MILLISECOND,
              app, workspace, reason, None) for start, end, app, workspace, reason in BLIPS["spans"]]
    return today.summarize(spans, MIDNIGHT, MIDNIGHT + BLIPS["now"] * MILLISECOND,
                           today.day_bounds(MIDNIGHT)[0])


def busy_summary():
    """08:00 to 20:00 switching between two apps every ten minutes: 72 rows."""
    spans = [(MIDNIGHT + (480 + 10 * i) * MINUTE, MIDNIGHT + (490 + 10 * i) * MINUTE,
              ("code", "firefox")[i % 2], "1", "active", None) for i in range(72)]
    return today.summarize(spans, MIDNIGHT, MIDNIGHT + 1200 * MINUTE,
                           today.day_bounds(MIDNIGHT)[0])


class Fits:
    def assert_fits(self, view):
        lines = view.lines()
        self.assertEqual(len(lines), view.height)
        for line in lines:
            text = "".join(text for text, _ in line)
            self.assertLessEqual(tui.cells(text), view.width, text)
            self.assertTrue(text.isprintable(), text)  # nothing that wraps or moves


class SnapshotTests(Fits, unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = fixture_database(directory.name)

    def test_each_tab_matches_its_snapshot_at_80x24_and_60x20(self):
        expected = snapshots()
        with FixedZone("UTC"):
            summary = today.today(self.path, MIDNIGHT + FIXTURE["now"] * MINUTE)
            for height, width in SIZES:
                for index, name in enumerate(tui.TABS):
                    with self.subTest(tab=name, size=f"{height}x{width}"):
                        view = tui.View(height, width)
                        view.update(summary, "16:00")
                        view.tab = index
                        self.assert_fits(view)
                        self.assertEqual(screen(view), expected[f"{name} {width}x{height}"])
                        highlighted = [text for text, style in view.lines()[1]
                                       if style == "tab_on"]
                        self.assertEqual(highlighted, [f" {name} "])

    def test_every_fixture_day_fits_each_tab_without_overflow(self):
        days = [today.summarize(expand(day["spans"]), MIDNIGHT, MIDNIGHT + day["now"] * SECOND,
                                today.day_bounds(MIDNIGHT)[0])
                for day in TIMELINES["days"].values()]
        days += [blip_summary(), busy_summary(), today.summarize([], MIDNIGHT, MIDNIGHT + MINUTE)]
        with FixedZone("UTC"):
            for number, summary in enumerate(days):
                for height, width in SIZES + ((5, 20), (1, 1)):
                    for tab in range(len(tui.TABS)):
                        view = tui.View(height, width)
                        view.update(summary, "20:00")
                        view.tab = tab
                        with self.subTest(day=number, size=(height, width), tab=tab):
                            self.assert_fits(view)

    def test_wide_and_control_characters_in_app_names_stay_inside_the_line(self):
        summary = today.summarize([(0, 2 * MINUTE, "微信微信微信微信微信微信微信微信", "1", "active", None),
                                   (2 * MINUTE, 8 * MINUTE, "bad\x1b[2Japp\n", "1", "active", None)],
                                  0, 8 * MINUTE)
        for height, width in SIZES:
            for tab in range(len(tui.TABS)):
                view = tui.View(height, width)
                view.update(summary, "00:08")
                view.tab = tab
                with self.subTest(size=(height, width), tab=tab):
                    self.assert_fits(view)
        self.assertEqual(tui.clip([("微信微信", "")], 5), [("微信…", "")])

    def test_an_unreadable_store_says_so_inside_the_frame(self):
        view = tui.View(20, 60)
        tui.refresh(view, lambda: (_ for _ in ()).throw(ValueError("schema")), lambda: "09:00")
        self.assertEqual(screen(view)[0], "Diurna · updated 09:00")
        self.assertIn("  Screen time unavailable (ValueError)", screen(view))
        self.assert_fits(view)


class TimelineFoldTests(Fits, unittest.TestCase):
    def test_sub_second_blips_leave_no_timeline_row_under_the_fold_threshold(self):
        summary = blip_summary()
        raw = summary["timeline"]
        # The stored day really has them: the plain and JSON timelines keep each.
        self.assertGreaterEqual(sum(r["end_ns"] - r["start_ns"] < SECOND for r in raw), 7)
        with FixedZone("UTC"):
            self.assertIn("0s  idle", bar.render(summary))
        settled = today.settle(raw)
        self.assertTrue(all(r["end_ns"] - r["start_ns"] >= today.FOLD_NS for r in settled),
                        [(r["start_ns"], r["end_ns"]) for r in settled])
        self.assertEqual(settled[0]["start_ns"], summary["start_ns"])
        self.assertEqual(settled[-1]["end_ns"], summary["end_ns"])
        self.assertTrue(all(a["end_ns"] == b["start_ns"] for a, b in zip(settled, settled[1:])))
        for row in settled:
            self.assertEqual(sum(p["ns"] for p in row["parts"]), row["end_ns"] - row["start_ns"])
        by_state = {}
        for row in settled:
            for part in row["parts"]:
                by_state[part["state"]] = by_state.get(part["state"], 0) + part["ns"]
        self.assertEqual(by_state["active"], summary["active_ns"])
        self.assertEqual(by_state["not_recorded"], summary["not_recorded_ns"])
        view = tui.View(24, 80)
        view.update(summary, "12:30")
        view.tab = 1
        with FixedZone("UTC"):
            self.assertEqual(screen(view)[3:9], [
                " · 00:00–08:00    8h 0m  not recorded (Diurna not running)",
                " █ 08:00–10:00    2h 0m  code · ws 3 (+4 switches)",
                " █ 10:00–10:40      40m  firefox · ws 2 (+5 switches)",
                " █ 10:40–11:00      19m  slack · ws 4 (+1 switch)",
                " █ 11:00–11:30      29m  code · ws 3",
                " ▒ 11:30–12:00      30m  idle",
            ])
            self.assertEqual(screen(view)[9], " █ 12:00–12:30      30m  code · ws 3 (+1 switch)")
            self.assertFalse(any(" 0s " in line for line in screen(view)))

    def test_settle_keeps_long_rows_and_a_lone_short_day(self):
        summary = today.summarize([(0, 30 * SECOND, "foot", "1", "active", None)], 0, 40 * SECOND)
        self.assertEqual([(r["start_ns"], r["end_ns"], r["state"], r["app_class"])
                          for r in today.settle(summary["timeline"])],
                         [(0, 40 * SECOND, "active", "foot")])
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with FixedZone("UTC"):
            day = today.today(fixture_database(directory.name), MIDNIGHT + FIXTURE["now"] * MINUTE)
        self.assertEqual([(r["start_ns"], r["end_ns"], r["state"])
                          for r in today.settle(day["timeline"])],
                         [(r["start_ns"], r["end_ns"], r["state"]) for r in day["timeline"]])
        self.assertEqual(today.settle([]), [])


class KeyTests(unittest.TestCase):
    def view(self, height=20, width=60):
        view = tui.View(height, width)
        view.update(busy_summary(), "20:00")
        return view

    def test_tab_shift_tab_arrows_and_digits_switch_tabs(self):
        view = self.view()
        presses = [ord("\t"), ord("\t"), ord("\t"), curses.KEY_BTAB, curses.KEY_RIGHT,
                   curses.KEY_LEFT, curses.KEY_LEFT, ord("3"), ord("1"), ord("2")]
        tabs = []
        for key in presses:
            self.assertTrue(view.key(key))
            tabs.append(view.tab)
        self.assertEqual(tabs, [1, 2, 0, 2, 0, 2, 1, 2, 0, 1])

    def test_q_and_escape_close(self):
        for key in (ord("q"), 27):
            self.assertFalse(self.view().key(key))

    def test_timeline_opens_at_now_and_scrolls_inside_its_tab(self):
        view = self.view()
        view.key(ord("2"))
        with FixedZone("UTC"):
            lines = screen(view)
            self.assertEqual(lines[-2], " █ 19:50–20:00      10m  firefox · ws 1")
            self.assertEqual(lines[1].split()[-3:], ["58–73", "of", "73"])
            view.key(curses.KEY_UP)
            self.assertEqual(screen(view)[-2], " █ 19:40–19:50      10m  code · ws 1")
            view.key(ord("k"))
            view.key(curses.KEY_PPAGE)
            self.assertEqual(screen(view)[3], " █ 14:20–14:30      10m  code · ws 1")
            self.assertEqual(screen(view)[-2], " █ 16:50–17:00      10m  firefox · ws 1")
            view.key(ord("g"))
            self.assertEqual(screen(view)[3],
                             " · 00:00–08:00    8h 0m  not recorded (Diurna not running)")
            view.key(curses.KEY_NPAGE)
            view.key(ord("j"))
            view.key(curses.KEY_DOWN)
            self.assertEqual(screen(view)[3], " █ 10:50–11:00      10m  firefox · ws 1")
            self.assertEqual(len(screen(view)), 20)
            # Other tabs keep their own place; the timeline's is kept on return.
            view.key(ord("1"))
            self.assertEqual(screen(view)[3].split()[1:3], ["Active", "12h"])
            view.key(ord("2"))
            self.assertEqual(screen(view)[3], " █ 10:50–11:00      10m  firefox · ws 1")
            view.key(ord("G"))
            self.assertEqual(screen(view)[-2], " █ 19:50–20:00      10m  firefox · ws 1")

    def test_scrolled_to_the_end_the_timeline_follows_new_rows(self):
        view = self.view()
        view.key(ord("2"))
        longer = busy_summary()
        longer["timeline"] = longer["timeline"] + [dict(
            longer["timeline"][-1], start_ns=MIDNIGHT + 1200 * MINUTE,
            end_ns=MIDNIGHT + 1210 * MINUTE, app_class="code",
            apps=[{"app_class": "code", "workspace": "1", "active_ns": 10 * MINUTE}])]
        view.update(longer, "20:10")
        with FixedZone("UTC"):
            self.assertEqual(screen(view)[-2], " █ 20:00–20:10      10m  code · ws 1")


class LoopTests(Fits, unittest.TestCase):
    def test_keys_changes_and_resizes_redraw_and_q_closes(self):
        loads = iter([busy_summary(), blip_summary()])
        painted = []
        view = tui.View(24, 80)
        stream = iter([("key", ord("2")), ("resize", (20, 60)), ("change", None),
                       ("resize", (24, 80)), ("key", ord("q")), ("key", ord("1"))])
        with FixedZone("UTC"):
            self.assertEqual(tui.loop(view, stream, lambda: next(loads), lambda: "20:00",
                                      painted.append), 0)
        self.assertEqual([len(p) for p in painted], [24, 24, 20, 20, 24])
        for lines, width in zip(painted, (80, 80, 60, 60, 80)):
            self.assertTrue(all(tui.cells("".join(t for t, _ in line)) <= width
                                for line in lines))
        self.assertIn("Timeline", "".join(t for t, s in painted[1][1] if s == "tab_on"))
        self.assertIn("19:50–20:00", "".join(t for t, _ in painted[2][-2]))
        self.assertIn("12:00–12:30", "".join(t for t, _ in painted[3][9]))  # the change
        self.assertEqual(next(stream), ("key", ord("1")))  # q stopped the loop

    def test_events_turn_signals_keys_and_changes_into_redraws(self):
        wake, wake_end = os.pipe2(os.O_NONBLOCK)
        keys, keys_end = os.pipe()
        changes, changes_end = os.pipe2(os.O_NONBLOCK)
        for fd in (wake, wake_end, keys, keys_end, changes, changes_end):
            self.addCleanup(os.close, fd)

        class Window:
            pending = []

            def getch(self):
                return self.pending.pop(0) if self.pending else -1

            def getmaxyx(self):
                return (18, 50)

        class Watcher:
            fd = changes

            def wait(self, timeout):
                os.read(changes, 64)

        resized, waits = [], []

        def wait(read, write, error, timeout):
            waits.append(timeout)
            return tui.select.select(read, write, error, 0)

        window = Window()
        stream = tui.events(window, Watcher(), wake, keys, 60, lambda: (30, 100),
                            lambda *size: resized.append(size), wait)
        previous = tui.on_resize(wake_end)
        try:
            os.kill(os.getpid(), signal.SIGWINCH)
        finally:
            signal.signal(signal.SIGWINCH, previous)
        self.assertEqual(next(stream), ("resize", (30, 100)))
        self.assertEqual(resized, [(30, 100)])
        os.write(keys_end, b"x")
        window.pending = [ord("j"), curses.KEY_RESIZE]
        self.assertEqual([next(stream), next(stream)], [("key", ord("j")), ("resize", (18, 50))])
        os.read(keys, 1)
        os.write(changes_end, b"1")
        self.assertEqual(next(stream), ("change", None))
        self.assertEqual(next(stream), ("change", None))  # nothing ready: the net
        self.assertEqual(waits, [60] * 4)


class CommandTests(unittest.TestCase):
    def test_watch_uses_the_tabbed_view_only_on_a_terminal(self):
        calls = []
        tty = type("Tty", (io.StringIO,), {"isatty": lambda self: True})
        for stdin, stdout, expected in ((tty(), tty(), "tui"), (tty(), io.StringIO(), "plain"),
                                        (io.StringIO(), tty(), "plain")):
            with patch.object(sys, "argv", ["diurna", "today", "--watch", "--database", "x"]), \
                    patch.object(sys, "stdin", stdin), patch.object(sys, "stdout", stdout), \
                    patch.object(tui, "run", lambda *a: calls.append("tui") or 0), \
                    patch.object(bar, "watch", lambda *a: calls.append("plain") or 0), \
                    patch.object(bar, "Watcher", lambda directory: None):
                self.assertEqual(main(), 0)
            self.assertEqual(calls.pop(), expected)


if __name__ == "__main__":
    unittest.main()
