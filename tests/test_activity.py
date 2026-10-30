"""Hands-on, watching and on screen: recorded signals in, split spans out."""
from contextlib import ExitStack
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from diurna import bar, runtime, today, tui
from diurna.changes import Changes
from diurna.collector import Collector, Span, focused_media, player_name, same_app
from diurna.store import Store

SECOND = today.SECOND
FIXTURES = Path(__file__).parent / "fixtures"
ACTIVITY = json.loads((FIXTURES / "activity.json").read_text())
AWAY = json.loads((FIXTURES / "away.json").read_text())
SCREENSAVER = today.SCREENSAVER
FLAG = {None: None, 0: False, 1: True}


class FakeClock:
    now = 0

    def __call__(self):
        return self.now


def replay(fixture, write):
    """Feed a recorded stream to the reducer. Hyprland reports inhibitors only
    when asked, so the fake answers for the app the latest focus event names."""
    clock, focused = FakeClock(), {"app": fixture["initial"][0]["class"]}
    collector = Collector(clock, write,
                          inhibitor=lambda: fixture["inhibitors"].get(focused["app"]))
    collector.snapshot(*fixture["initial"])
    for seconds, source, data in fixture["events"]:
        clock.now = seconds * SECOND
        if source == "hypr":
            if data.startswith("activewindow>>"):
                focused["app"] = data.partition(">>")[2].partition(",")[0]
            assert collector.hyprland(data)
        elif source == "hook":
            collector.signal(*runtime.HOOKS[data])
        elif source == "media":
            collector.media(runtime.players(data))
        else:
            collector.checkpoint()
    return collector


def stored(spans, unit, offset=0):
    return [(offset + start * unit, offset + end * unit, app, workspace, reason, None,
             FLAG[input_], FLAG[media], FLAG[inhibiting])
            for start, end, app, workspace, reason, input_, media, inhibiting in spans]


def old_active(spans, start_ns, end_ns):
    """Active time as read before this change: stored active, less the screensaver."""
    return sum(max(0, min(end, end_ns) - max(start, start_ns))
               for start, end, app, _, reason, *_ in spans
               if reason == "active" and app != SCREENSAVER)


def totals(summary, unit):
    activity = summary["activity"]
    result = {key: activity[f"{key}_ns"] // unit for key in today.ACTIVITIES}
    result.update(idle=summary["states"]["idle_ns"] // unit, away=summary["away_ns"] // unit)
    if activity["unsplit_ns"]:
        result["unsplit"] = activity["unsplit_ns"] // unit
    return result


class ReplayTests(unittest.TestCase):
    def test_recorded_signals_give_the_expected_spans_in_sqlite(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "spans.sqlite3"
            store = Store(path)
            try:
                replay(ACTIVITY, store.write)
                rows = store.db.execute("""SELECT start_ns,end_ns,app_class,workspace,reason,
                    input,media,inhibiting FROM spans ORDER BY id""").fetchall()
            finally:
                store.close()
            expected = ACTIVITY["expected"]
            self.assertEqual(rows, [(a * SECOND, b * SECOND, *rest[:6])
                                    for a, b, *rest in expected])
            spans = today.rows(path, 0, 700 * SECOND)
            self.assertEqual([row[4] if row[6] is None else row[6]
                              for row in today.classify(spans)], [row[-1] for row in expected])
            summary = today.summarize(spans, 0, 700 * SECOND)
            self.assertEqual(totals(summary, SECOND), ACTIVITY["totals"])
            self.assertNotIn(b"PRIVATE", path.read_bytes())

    def test_media_in_an_unfocused_app_does_not_make_the_focused_app_watching(self):
        spans = []
        replay({**ACTIVITY, "events": ACTIVITY["events"][:3] + [[100, "flush", ""]]},
               spans.append)
        # Spotify plays the whole time; foot, focused, has no player.
        self.assertEqual([(s.app_class, s.input, s.media) for s in spans],
                         [("foot", True, None), ("foot", False, None)])
        classified = today.classify([(s.start_ns, s.end_ns, s.app_class, s.workspace, s.reason,
                                      None, s.input, s.media, s.inhibiting) for s in spans])
        self.assertEqual([row[6] for row in classified], ["hands_on", "on_screen"])

    def test_inhibitor_is_read_on_focus_fullscreen_and_input_changes_only(self):
        clock, asked, spans = FakeClock(), [], []
        collector = Collector(clock, spans.append, inhibitor=lambda: asked.append(1) or True)
        collector.snapshot({"class": "foot", "address": "0xaa"}, "1", [])
        for tick in range(1, 50):
            # A terminal retitling itself re-sends activewindow for the same window.
            clock.now = tick
            collector.hyprland("activewindow>>foot,PRIVATE %d" % tick)
            collector.hyprland("activewindowv2>>aa")
        self.assertEqual(asked, [])
        collector.hyprland("activewindow>>mpv,PRIVATE")
        collector.hyprland("activewindowv2>>bb")
        self.assertEqual(len(asked), 1)  # once per focus change, not per event
        collector.hyprland("activewindow>>foot,PRIVATE")
        collector.hyprland("activewindowv2>>cc")  # a second foot window
        collector.hyprland("activewindowv2>>dd")
        collector.hyprland("fullscreen>>1")
        collector.signal("input", False)
        collector.signal("idle", True)
        self.assertEqual(len(asked), 5)
        collector.hyprland("closewindow>>dd")
        self.assertIsNone(collector.context.inhibiting)


class ViewTests(unittest.TestCase):
    def test_today_bar_and_tabs_show_the_split_only_when_recorded(self):
        spans = stored([row[:8] for row in ACTIVITY["expected"]], SECOND)
        summary = today.summarize(spans, 0, 700 * SECOND)
        with patch.object(bar, "policy", return_value={"safety_seconds": 60}):
            tooltip = bar.bar(summary)["tooltip"]
        self.assertEqual(tooltip.splitlines()[:2],
                         ["Today 10m active", "hands-on 3m · watching 4m · on screen 3m"])
        text = bar.render(summary)
        self.assertIn("  Active           10m\n  · hands-on        3m\n  · watching        4m\n"
                      "  · on screen       3m\n  Idle              1m", text)
        self.assertIn("mpv · ws 1 · watching (+1 switch)", text)
        self.assertIn("chromium · ws 1 · watching 2m, on screen 1m (+1 switch)", text)
        view = tui.View(24, 80)
        view.update(summary, "")
        overview = ["".join(t for t, _ in line) for line in view.lines()]
        self.assertIn("█ · hands-on       3m", overview[3])
        legacy = today.summarize([span[:6] for span in spans], 0, 700 * SECOND)
        self.assertEqual(bar.activity_totals(legacy), [])
        self.assertNotIn("hands-on", bar.render(legacy))


class PlayerTests(unittest.TestCase):
    def test_player_names_match_window_classes(self):
        self.assertEqual(player_name("org.mpris.MediaPlayer2.chromium.instance12345"), "chromium")
        self.assertEqual(player_name("org.mpris.MediaPlayer2.firefox.instance_1_23"), "firefox")
        self.assertEqual(player_name("org.mpris.MediaPlayer2.spotify"), "spotify")
        for app, name in (("chromium", "chromium"), ("Chromium", "org.mpris.MediaPlayer2.chromium.instance9"),
                          ("chromium-browser", "chromium"), ("org.mozilla.firefox", "firefox"),
                          ("chrome-www.youtube.com__-Default", "chromium"), ("mpv", "mpv")):
            with self.subTest(app=app, name=name):
                self.assertTrue(same_app(app, name))
        for app, name in (("foot", "spotify"), ("", "chromium"), ("chromium", ""),
                          ("mpvpaper", "mpv"), ("chrome-x-Default", "firefox")):
            with self.subTest(app=app, name=name):
                self.assertFalse(same_app(app, name))

    def test_focused_media_is_per_app(self):
        players = {"chromium": False, "org.mpris.MediaPlayer2.chromium.instance1": True,
                   "spotify": True}
        self.assertTrue(focused_media("chromium", players))
        self.assertFalse(focused_media("chromium", {"chromium": False}))
        self.assertIsNone(focused_media("foot", players))

    def test_media_tokens_are_validated(self):
        self.assertEqual(runtime.players(["a:paused", "a:playing", "b.c-d_e:paused"]),
                         {"a": True, "b.c-d_e": False})
        for tokens in (["a"], ["a:stopped"], ["a b:playing"], ["PRIVATE TITLE:playing"],
                       ["x" * 129 + ":playing"], ["a:playing"] * 65):
            with self.subTest(tokens=tokens), self.assertRaises(ValueError):
                runtime.players(tokens)


class AwayTests(unittest.TestCase):
    def test_fixture_days(self):
        for name, day in AWAY["days"].items():
            with self.subTest(day=name):
                spans = stored(day["spans"], SECOND)
                now = day["now"] * SECOND
                summary = today.summarize(spans, 0, now)
                self.assertEqual([[r["start_ns"] // SECOND, r["end_ns"] // SECOND, r["state"],
                                   r["app_class"]] for r in summary["timeline"]], day["timeline"])
                self.assertEqual(totals(summary, SECOND), day["totals"])
                # The screensaver window is never an app, whatever the rules read.
                self.assertNotIn(SCREENSAVER, [a["app_class"] for a in summary["apps"]])
                self.assertNotIn(SCREENSAVER, [w["app_class"] for w in summary["windows"]])
                for row in summary["timeline"]:
                    self.assertNotIn(SCREENSAVER, [a["app_class"] for a in row["apps"]])
                    self.assertEqual(sum(row["activity"].values()) <= sum(
                        a["active_ns"] for a in row["apps"]), True)

    def test_grace_is_a_reading_setting(self):
        day = AWAY["days"]["screensaver_dismissed_after_3s_keeps_on_screen"]
        spans = stored(day["spans"], SECOND)
        summary = today.summarize(spans, 0, day["now"] * SECOND, grace_ns=2 * SECOND)
        self.assertEqual(summary["away_ns"], 90 * SECOND)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "spans.sqlite3"
            store = Store(path)
            try:
                for span in spans:
                    store.write(Span(*span[:5], *span[6:]))
            finally:
                store.close()
            with patch("diurna.today.day_bounds", return_value=(None, 0, 86400 * SECOND)), \
                    patch.object(today, "policy",
                                 return_value={"screensaver_grace_seconds": 2.5}):
                self.assertEqual(today.today(path, day["now"] * SECOND)["away_ns"], 90 * SECOND)

    def test_away_carries_across_the_start_of_the_day(self):
        day = AWAY["days"]["screensaver_until_the_lock_then_no_window_then_idle_is_away"]
        midnight = 3700 * SECOND  # inside the no-window stretch after the screensaver
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "spans.sqlite3"
            store = Store(path)
            try:
                for span in stored(day["spans"], SECOND):
                    store.write(Span(*span[:5], *span[6:]))
            finally:
                store.close()
            spans = today.rows(path, midnight, day["now"] * SECOND)
            summary = today.summarize(spans, midnight, day["now"] * SECOND)
        self.assertEqual(spans[0][0], 0)  # back to the last input
        self.assertEqual(summary["states"]["idle_ns"], 300 * SECOND)
        self.assertEqual(summary["away_ns"], 51 * SECOND)
        self.assertEqual(summary["activity"]["hands_on_ns"], 100 * SECOND)
        self.assertEqual([r["state"] for r in summary["timeline"]], ["idle", "active"])

    def test_split_and_away_add_up_to_active_before_this_change_for_every_fixture_day(self):
        days = []
        day = json.loads((FIXTURES / "day.json").read_text())
        days.append(("day", [(s * 60, e * 60, *rest) for s, e, *rest in day["spans"]],
                     day["now"] * 60, SECOND))
        saver = json.loads((FIXTURES / "screensaver.json").read_text())
        days.append(("screensaver", [(s * 60, e * 60, *rest) for s, e, *rest in saver["spans"]],
                     saver["now"] * 60, SECOND))
        for name, timeline in json.loads((FIXTURES / "timelines.json").read_text())["days"].items():
            days.append((name, [tuple(s[:5]) for s in timeline["spans"]], timeline["now"], SECOND))
        blips = json.loads((FIXTURES / "blips.json").read_text())
        days.append(("blips", [tuple(s[:5]) for s in blips["spans"]], blips["now"],
                     1_000_000))
        for name, away in AWAY["days"].items():
            days.append((name, away["spans"], away["now"], SECOND))
        days.append(("activity", [row[:8] for row in ACTIVITY["expected"]], 700, SECOND))
        for name, spans, now, unit in days:
            with self.subTest(day=name):
                rows = [(start * unit, end * unit, app, workspace, reason, None,
                         *(FLAG[flag] for flag in signals))
                        for start, end, app, workspace, reason, *signals in spans]
                summary = today.summarize(rows, 0, now * unit)
                activity = summary["activity"]
                self.assertEqual(sum(activity.values()), summary["active_ns"])
                self.assertEqual(sum(activity.values()) + summary["away_ns"],
                                 old_active(rows, 0, now * unit))


class StoreTests(unittest.TestCase):
    def test_version_1_database_upgrades_in_place_and_reads_as_before(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "spans.sqlite3"
            with sqlite3.connect(path) as db:
                db.executescript("""
                    CREATE TABLE spans (id INTEGER PRIMARY KEY, start_ns INTEGER NOT NULL,
                        end_ns INTEGER NOT NULL CHECK(end_ns > start_ns),
                        app_class TEXT NOT NULL, workspace TEXT NOT NULL,
                        active INTEGER NOT NULL CHECK(active IN (0, 1)),
                        reason TEXT NOT NULL, title TEXT CHECK(title IS NULL),
                        CHECK(active = (reason = 'active')), UNIQUE(start_ns));
                    INSERT INTO spans VALUES (1, 0, 60, 'foot', '1', 1, 'active', NULL);
                    INSERT INTO spans VALUES (2, 60, 90, 'foot', '1', 0, 'idle', NULL);
                    PRAGMA user_version = 1;""")
            db.close()
            before = today.summarize(today.rows(path, 0, 90), 0, 90)
            self.assertEqual(before["activity"]["unsplit_ns"], 60)
            store = Store(path)
            try:
                store.write(Span(90, 100, "foot", "1", "active", True, None, False))
                self.assertEqual(store.db.execute("PRAGMA user_version").fetchone(), (2,))
                with self.assertRaises(sqlite3.IntegrityError):
                    store.db.execute("UPDATE spans SET input = 2")
            finally:
                store.close()
            self.assertEqual(today.summarize(today.rows(path, 0, 90), 0, 90), before)
            after = today.summarize(today.rows(path, 0, 100), 0, 100)
            self.assertEqual((after["activity"]["unsplit_ns"], after["activity"]["hands_on_ns"]),
                             (60, 10))
            # Opening again changes nothing.
            Store(path).close()
            self.assertEqual(today.rows(path, 0, 100)[-1][6:], (1, None, 0))


class RuntimeSignalTests(unittest.TestCase):
    def test_input_and_media_signals_reach_stored_spans(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            directory = Path(temporary)
            store = Store(directory / "db")
            stack.callback(store.close)
            clock = SimpleNamespace(now=0)
            messages = [b"noinput\n", b"media chromium:playing spotify:paused\n",
                        b"media PRIVATE TITLE\n", b"input extra\n", b"input\n"]
            steps = iter([(10 * (i + 1), "control") for i in range(len(messages))]
                         + [(60, "stop")])

            def select(_):
                clock.now, kind = next(steps)
                return [(SimpleNamespace(data=kind), 1)]

            control, client = Mock(), Mock()
            client.__enter__ = Mock(return_value=client)
            client.__exit__ = Mock(return_value=False)
            client.recv.side_effect = messages
            control.accept.return_value = (client, "")
            selector = Mock()
            selector.select.side_effect = select
            stack.enter_context(patch.object(runtime, "flags", return_value={
                "idle": False, "locked": False, "suspended": False, "input": True}))
            stack.enter_context(patch.object(runtime, "snapshot", return_value=(
                {"class": "chromium", "address": "0xbb", "inhibitingIdle": False}, "1", [])))
            stack.enter_context(patch.object(runtime, "inhibiting", return_value=True))
            stack.enter_context(patch.object(runtime, "policy", return_value={
                "safety_seconds": 60, "ipc_timeout_seconds": 3}))
            stack.enter_context(patch.object(runtime.time, "monotonic", return_value=0))
            stack.enter_context(patch.object(runtime, "SleepDetector"))
            runtime._loop(selector, Mock(), control, store, lambda: clock.now, directory,
                          directory, directory / "control.sock",
                          Changes(directory, proc=directory / "proc", kill=Mock()))
            self.assertEqual(store.db.execute("""SELECT start_ns,end_ns,app_class,input,media,
                inhibiting FROM spans""").fetchall(),
                             [(0, 10, "chromium", 1, None, 0),
                              (10, 20, "chromium", 0, None, 1),
                              (20, 50, "chromium", 0, 1, 1),
                              (50, 60, "chromium", 1, 1, 1)])
            # Malformed media and a hook with arguments are refused, not acknowledged.
            self.assertEqual(client.sendall.call_count, 3)

    def test_signal_state_persists_while_the_collector_is_down(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch.object(runtime, "paths", return_value=(directory, directory)), \
                    patch.object(runtime, "policy", return_value={"ipc_timeout_seconds": 1}):
                self.assertIsNone(runtime.flags(directory)["input"])
                with self.assertRaises(OSError):
                    runtime.notify("noinput")
                with self.assertRaises(OSError):
                    runtime.notify("media", ["chromium:playing", "spotify:paused"])
                with self.assertRaises(ValueError):
                    runtime.notify("media", ["PRIVATE TITLE"])
                with self.assertRaises(ValueError):
                    runtime.notify("idle", ["chromium:playing"])
            self.assertFalse(runtime.flags(directory)["input"])
            self.assertEqual(runtime.media(directory), {"chromium": True, "spotify": False})


if __name__ == "__main__":
    unittest.main()
