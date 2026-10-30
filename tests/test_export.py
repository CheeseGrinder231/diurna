"""Raw wire contract witnesses, with synthetic private SQLite sources only."""
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from diurna import export
from diurna.collector import Span
from diurna.store import Store
from test_identity import legacy

MACHINE = "11111111-1111-4111-8111-111111111111"
GENERATION = "22222222-2222-4222-8222-222222222222"
ROOT = Path(__file__).resolve().parents[1]


def capture(path, start=-100, end=100, high=None):
    output = io.BytesIO()
    export.export(path, output, start, end, high)
    return output.getvalue()


def records(data):
    return [json.loads(line) for line in data.splitlines()]


class ExportTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.directory = Path(scratch.name)
        self.path = self.directory / "archive.db"
        archive = Store(self.path)
        archive.db.execute("UPDATE diurna_identity SET machine=?, generation=?",
                           (MACHINE, GENERATION))
        archive.db.commit()
        archive.close()

    def write(self, spans):
        archive = Store(self.path)
        try:
            for span in spans:
                archive.write(Span(*span))
        finally:
            archive.close()

    def cli(self, *arguments):
        return subprocess.run([sys.executable, "-m", "diurna", *arguments],
                              cwd=ROOT, capture_output=True)

    def test_independently_authored_document_golden_is_exact(self):
        self.write([(-9007199254740993, -1, "終端", "", "active"),
                    (-1, 9007199254740993, "終端", "", "active", False, True, False)])
        golden = (ROOT / "docs/export.md").read_bytes().split(b"```jsonl\n")[1].split(b"```", 1)[0]
        self.assertEqual(capture(self.path, -2, 1), golden)
        parsed = records(golden)
        self.assertEqual(parsed[1]["id"], "span:-9007199254740993")
        self.assertEqual(parsed[2]["endNs"], 9007199254740993)
        self.assertEqual(self.cli("export", "--database", str(self.path),
                                  "--start-ns", "-2", "--end-ns", "1").stdout, golden)

    def test_reasons_allowlist_nullable_legacy_signals_unicode_and_escaping(self):
        reasons = ("active", "idle", "locked", "suspended", "disconnected", "unknown")
        self.write([(i * 10, (i + 1) * 10, "應用\n\"\\", "桌面二", reason)
                    for i, reason in enumerate(reasons)])
        result = capture(self.path)
        self.assertNotIn(b"title", result)
        spans = records(result)[1:-1]
        self.assertEqual([span["reason"] for span in spans], list(reasons))
        for span in spans:
            self.assertEqual(set(span), {"format", "version", "type", "machine", "generation",
                                        "id", "startNs", "endNs", "appClass", "workspace",
                                        "reason", "input", "media", "inhibiting"})
            self.assertEqual(span["appClass"], "應用\n\"\\")
            self.assertIsNone(span["input"])
        old = self.directory / "v1.db"
        legacy(old, 1)
        Store(old).close()
        self.assertTrue(all(row["media"] is None for row in records(capture(old))[1:-1]))

    def test_range_edges_checkpoints_repeat_overlap_and_filename_independence(self):
        self.write([(-10, 0, "", "", "active"), (0, 10, "a", "1", "active"),
                    (10, 20, "a", "1", "active"), (20, 30, "", "", "unknown")])
        all_rows = records(capture(self.path))
        selected = records(capture(self.path, 0, 20))
        self.assertEqual(selected[1:-1], all_rows[2:4])
        overlapping = records(capture(self.path, 5, 11))
        self.assertEqual(overlapping[1:-1], selected[1:-1])
        self.assertEqual(capture(self.path, 0, 20), capture(self.path, 0, 20))
        renamed = self.directory / "renamed.db"
        shutil.copy2(self.path, renamed)
        self.assertEqual(capture(self.path), capture(renamed))
        bounded = records(capture(self.path, 0, 30, 0))
        self.assertEqual(bounded[0]["throughStartNs"], 0)
        self.assertEqual(bounded[1:-1], all_rows[2:3])
        self.assertEqual(records(capture(self.path, 30, 40))[-1],
                         {"format": export.FORMAT, "version": 1, "type": "complete", "rows": 0})

    def test_signed_64_bit_endpoints_and_empty_archive(self):
        empty = records(capture(self.path))
        self.assertIsNone(empty[0]["throughStartNs"])
        self.assertEqual(empty[-1]["rows"], 0)
        self.write([(export.MIN_NS, export.MAX_NS, "", "", "active")])
        span = records(capture(self.path, export.MIN_NS, export.MAX_NS))[1]
        self.assertEqual((span["startNs"], span["endNs"], span["id"]),
                         (export.MIN_NS, export.MAX_NS, f"span:{export.MIN_NS}"))

    def test_concurrent_append_cannot_enter_pinned_snapshot(self):
        self.write([(0, 10, "a", "", "active")])
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA journal_mode=WAL")
        db.close()
        test = self

        class AppendAfterHeader(io.BytesIO):
            appended = False

            def write(self, data):
                if not self.appended:
                    self.appended = True
                    test.write([(10, 20, "b", "", "active")])
                return super().write(data)

        output = AppendAfterHeader()
        export.export(self.path, output, 0, 100)
        first = records(output.getvalue())
        self.assertEqual((first[0]["throughStartNs"], first[-1]["rows"]), (0, 1))
        later = records(capture(self.path, 0, 100))
        self.assertEqual((later[0]["throughStartNs"], later[-1]["rows"]), (10, 2))
        replay = records(capture(self.path, 0, 100, first[0]["throughStartNs"]))
        self.assertEqual(replay, first)

    def test_missing_legacy_future_and_identity_damage_fail_read_only(self):
        self.path.unlink()
        result = self.cli("export", "--database", str(self.path), "--start-ns", "0", "--end-ns", "1")
        self.assertEqual((result.returncode, result.stderr), (1, export.ERROR.encode()))
        self.assertFalse(self.path.exists())
        for version in (1, 2, 4):
            path = self.directory / f"v{version}.db"
            legacy(path, version)
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                capture(path)
            self.assertEqual(path.read_bytes(), before)
        mutations = ["DROP TABLE diurna_identity", "DELETE FROM diurna_identity",
                     "UPDATE diurna_identity SET machine='PRIVATE TITLE'",
                     "UPDATE diurna_identity SET format='unsupported'",
                     "ALTER TABLE diurna_identity RENAME TO original; "
                     "CREATE TABLE diurna_identity AS SELECT * FROM original; "
                     "INSERT INTO diurna_identity SELECT * FROM original"]
        for mutation in mutations:
            self.path.unlink(missing_ok=True)
            Store(self.path).close()
            db = sqlite3.connect(self.path)
            db.executescript(mutation)
            db.close()
            before = self.path.read_bytes()
            result = self.cli("export", "--database", str(self.path),
                              "--start-ns", "0", "--end-ns", "1")
            self.assertEqual((result.returncode, result.stderr), (1, export.ERROR.encode()))
            self.assertNotIn(b"PRIVATE TITLE", result.stdout + result.stderr)
            self.assertEqual(self.path.read_bytes(), before)

    def test_unsafe_sources_and_invalid_requests(self):
        symlink = self.directory / "link.db"
        symlink.symlink_to(self.path)
        hardlink = self.directory / "hard.db"
        os.link(self.path, hardlink)
        for path in (symlink, hardlink, self.directory):
            with self.assertRaises(ValueError):
                capture(path)
        hardlink.unlink()
        self.path.chmod(0o640)
        with self.assertRaises(ValueError):
            capture(self.path)
        self.path.chmod(0o600)
        with patch.object(export.os, "getuid", return_value=self.path.stat().st_uid + 1):
            with self.assertRaises(ValueError):
                capture(self.path)
        for start, end, high in [(0, 0, None), (2, 1, None), (export.MIN_NS - 1, 0, None),
                                 (0, export.MAX_NS + 1, None), (0, 1, export.MAX_NS + 1),
                                 (False, 1, None)]:
            with self.assertRaises(ValueError):
                capture(self.path, start, end, high)
        for arguments in [("--start-ns", "PRIVATE TITLE"), ("--surprise", "PRIVATE TITLE"), ()]:
            result = self.cli("export", "--database", str(self.path), *arguments)
            self.assertEqual((result.returncode, result.stderr),
                             (2, b"diurna: invalid request\n"))
            self.assertNotIn(b"PRIVATE TITLE", result.stdout + result.stderr)
        result = self.cli("export", "--database", str(self.path), "--start-ns", "0", "--end-ns", "0")
        self.assertEqual((result.returncode, result.stderr), (1, export.ERROR.encode()))

    def test_budgets_exact_boundary_and_one_above_never_complete(self):
        self.write([(0, 1, "é" * 2048, "", "active")])
        exact = capture(self.path)
        with patch.object(export, "MAX_OUTPUT_BYTES", len(exact)):
            self.assertEqual(capture(self.path), exact)
        for name, value in [("MAX_ROWS", 0), ("MAX_STRING_BYTES", 4095),
                            ("MAX_OUTPUT_BYTES", len(exact) - 1)]:
            output = io.BytesIO()
            with patch.object(export, name, value), self.assertRaises(ValueError):
                export.export(self.path, output, -100, 100)
            self.assertNotIn(b'"type":"complete"', output.getvalue())
        with patch.object(export, "MAX_ROWS", 1):
            self.assertEqual(capture(self.path), exact)
            self.write([(1, 2, "b", "", "active")])
            with self.assertRaises(ValueError):
                capture(self.path)
        # Real SQL budget interruption, driven by VM progress, not elapsed time.
        self.write([(i, i + 1, "", "", "active") for i in range(2, 102)])
        with patch.object(export, "MAX_SQL_STEPS", 0), self.assertRaises(sqlite3.Error):
            capture(self.path)

    def test_default_row_ceiling_is_not_silent_pagination(self):
        db = sqlite3.connect(self.path)
        try:
            db.executemany("""INSERT INTO spans
                (start_ns,end_ns,app_class,workspace,active,reason)
                VALUES (?,?,'','',1,'active')""",
                ((i, i + 1) for i in range(export.MAX_ROWS)))
            db.commit()
            self.assertEqual(records(capture(self.path, 0, export.MAX_ROWS + 1))[-1]["rows"],
                             export.MAX_ROWS)
            db.execute("""INSERT INTO spans
                (start_ns,end_ns,app_class,workspace,active,reason)
                VALUES (?,?,'','',1,'active')""", (export.MAX_ROWS, export.MAX_ROWS + 1))
            db.commit()
        finally:
            db.close()
        output = io.BytesIO()
        with self.assertRaises(ValueError):
            export.export(self.path, output, 0, export.MAX_ROWS + 1)
        self.assertNotIn(b'"type":"complete"', output.getvalue())

    def test_malformed_source_never_succeeds_or_leaks_title(self):
        # Model damaged/unconstrained sources without relying on live corruption.
        db = sqlite3.connect(self.path)
        db.executescript("DROP TABLE spans; CREATE TABLE spans "
                         "(id, start_ns, end_ns, app_class, workspace, active, reason, title, "
                         "input, media, inhibiting)")
        db.close()
        valid = [1, 0, 10, "a", "1", 1, "active", None, None, None, None]
        variants = [(1, None), (1, "PRIVATE TITLE"), (2, 0), (2, 1.5), (2, None),
                    (3, b"blob"), (3, "a" * 4097), (4, None), (5, 0),
                    (6, "PRIVATE TITLE"), (7, "PRIVATE TITLE"), (8, 2), (9, b"blob"), (10, 0.5)]
        for index, value in variants:
            row = valid.copy()
            row[index] = value
            db = sqlite3.connect(self.path)
            db.execute("DELETE FROM spans")
            db.execute("INSERT INTO spans VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
            db.commit()
            db.close()
            before = self.path.read_bytes()
            result = self.cli("export", "--database", str(self.path),
                              "--start-ns", "0", "--end-ns", "100")
            self.assertEqual((result.returncode, result.stderr), (1, export.ERROR.encode()), row)
            self.assertNotIn(b"PRIVATE TITLE", result.stdout + result.stderr)
            self.assertNotIn(b'"type":"complete"', result.stdout)
            self.assertEqual(self.path.read_bytes(), before)
        db = sqlite3.connect(self.path)
        db.execute("DELETE FROM spans")
        db.executemany("INSERT INTO spans VALUES (?,?,?,?,?,?,?,?,?,?,?)", [valid, valid])
        db.commit()
        db.close()
        with self.assertRaises(ValueError):
            capture(self.path)

    def test_failed_and_short_writes_raise_without_success(self):
        class Short(io.BytesIO):
            def write(self, data):
                return len(data) - 1

        with self.assertRaises(OSError):
            export.export(self.path, Short(), -100, 100)
        output = io.BytesIO()
        output.close()
        with self.assertRaises(ValueError):
            export.export(self.path, output, -100, 100)

    def test_cli_broken_pipe_has_only_the_fixed_diagnostic(self):
        process = subprocess.Popen([sys.executable, "-m", "diurna", "export",
                                    "--database", str(self.path), "--start-ns", "0",
                                    "--end-ns", "1"], cwd=ROOT,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process.stdout.close()
        try:
            diagnostic = process.stderr.read()
            self.assertEqual(process.wait(), 1)
            self.assertEqual(diagnostic, export.ERROR.encode())
        finally:
            process.stderr.close()
            process.wait()

    def test_explicit_cli_migration_is_idempotent_and_needs_no_desktop(self):
        old = self.directory / "old.db"
        legacy(old, 2)
        for _ in range(2):
            result = self.cli("migrate", "--database", str(old))
            self.assertEqual((result.returncode, result.stderr, result.stdout), (0, b"", b""))
        result = self.cli("export", "--database", str(old), "--start-ns", "-100", "--end-ns", "100")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(records(result.stdout)[-1]["rows"], 2)
