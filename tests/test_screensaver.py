from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from diurna import bar, today
from diurna.__main__ import main
from diurna.collector import Span
from diurna.store import Store

MINUTE = 60 * today.SECOND
SCREENSAVER = "org.omarchy.screensaver"
FIXTURE = json.loads((Path(__file__).parent / "fixtures/screensaver.json").read_text())


class ScreensaverTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "spans.sqlite3"
        self.now = FIXTURE["now"] * MINUTE
        store = Store(self.path)
        try:
            for start, end, app, workspace, reason in FIXTURE["spans"]:
                store.write(Span(start * MINUTE, end * MINUTE, app, workspace, reason))
        finally:
            store.close()

    def test_stored_active_screensaver_reads_as_idle_without_migration(self):
        before = self.path.read_bytes()
        spans = today.rows(self.path, 0, self.now)
        # This is legacy storage: the collector really recorded active time.
        self.assertEqual(spans[0][4], "active")
        summary = today.summarize(spans, 0, self.now)
        expected = FIXTURE["expected"]
        self.assertEqual(summary["active_ns"], expected["active"] * MINUTE)
        self.assertEqual(summary["states"],
                         {f"{k}_ns": v * MINUTE for k, v in expected["states"].items()})
        self.assertEqual((summary["not_recorded_ns"], summary["pending_ns"]), (0, 0))
        self.assertEqual([[a["app_class"], a["active_ns"] // MINUTE]
                          for a in summary["apps"]], expected["apps"])
        self.assertNotIn(SCREENSAVER, [w["app_class"] for w in summary["windows"]])
        timeline = summary["timeline"]
        self.assertEqual([[r["start_ns"] // MINUTE, r["end_ns"] // MINUTE, r["state"]]
                          for r in timeline], expected["timeline"])
        self.assertEqual(today.timeline(spans, 0, self.now), timeline)
        self.assertTrue(all(a["end_ns"] == b["start_ns"]
                            for a, b in zip(timeline, timeline[1:])))
        for row in timeline:
            self.assertNotEqual(row["app_class"], SCREENSAVER)
            self.assertNotIn(SCREENSAVER, [a["app_class"] for a in row["apps"]])
            self.assertEqual(sum(a["active_ns"] for a in row["apps"]),
                             row["end_ns"] - row["start_ns"] if row["state"] == "active" else 0)
        for row in today.settle(timeline):
            self.assertNotIn(SCREENSAVER, [p["app_class"] for p in row["parts"]])
        self.assertEqual(self.path.read_bytes(), before)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(db.execute("""SELECT active, reason, input, media, inhibiting
                FROM spans ORDER BY start_ns LIMIT 1""").fetchone(), (1, "active", None, None, None))
        db.close()

    def test_today_and_bar_use_corrected_history(self):
        # Pin bounds instead of depending on the host timezone or wall clock.
        with patch("diurna.today.day_bounds", return_value=(None, 0, 86400 * today.SECOND)), \
                patch("diurna.__main__.time.time_ns", return_value=self.now):
            outputs = []
            for argv in (["today", "--json"], ["today"], ["bar"]):
                stream = io.StringIO()
                with patch.object(sys, "argv", ["diurna", *argv, "--database", str(self.path)]), \
                        redirect_stdout(stream):
                    self.assertEqual(main(), 0)
                outputs.append(stream.getvalue())
        summary = json.loads(outputs[0])
        self.assertEqual(summary["active_ns"], 13 * MINUTE)
        self.assertEqual(summary["states"]["idle_ns"], 15 * MINUTE)
        self.assertIn("Idle             15m", outputs[1])
        module = json.loads(outputs[2])
        self.assertEqual(module["text"], "13m")
        self.assertIn("idle 15m · locked 2m · asleep 2m", module["tooltip"])
        self.assertEqual(module, bar.bar(summary))
        for output in outputs:
            # The similarly named ordinary app must still appear.
            self.assertNotIn(SCREENSAVER, output.replace(SCREENSAVER + ".preview", "preview"))
