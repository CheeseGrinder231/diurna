"""Synthetic archive lifecycle witnesses; no desktop or real state."""
import fcntl
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from diurna import identity, store
from diurna.collector import Span


def legacy(path, version):
    db = sqlite3.connect(path)
    try:
        db.executescript("""CREATE TABLE spans (
            id INTEGER PRIMARY KEY, start_ns INTEGER NOT NULL UNIQUE,
            end_ns INTEGER NOT NULL CHECK(end_ns > start_ns),
            app_class TEXT NOT NULL, workspace TEXT NOT NULL,
            active INTEGER NOT NULL, reason TEXT NOT NULL, title TEXT CHECK(title IS NULL));
            INSERT INTO spans VALUES (7, -100, 0, '終端', '', 1, 'active', NULL);
            INSERT INTO spans VALUES (42, 0, 100, '', '二', 0, 'idle', NULL);
            CREATE INDEX spans_end ON spans(end_ns);""")
        if version == 2:
            for name in store.SIGNALS:
                db.execute(f"ALTER TABLE spans ADD COLUMN {name} INTEGER")
            db.execute("UPDATE spans SET input=0, media=1, inhibiting=0 WHERE id=7")
        db.execute(f"PRAGMA user_version={version}")
        db.commit()
    finally:
        db.close()
    os.chmod(path, 0o600)


def facts(path):
    db = sqlite3.connect(path)
    try:
        return (db.execute("PRAGMA user_version").fetchone(),
                db.execute("SELECT * FROM spans ORDER BY id").fetchall(),
                db.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall())
    finally:
        db.close()


class IdentityTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.directory = Path(scratch.name)
        self.path = self.directory / "archive.db"

    def test_fresh_and_both_migrations_preserve_every_span_and_reopen(self):
        for version in (0, 1, 2):
            path = self.directory / f"v{version}.db"
            old = []
            if version:
                legacy(path, version)
                old = facts(path)[1]
            archive = store.Store(path)
            pair = (archive.machine, archive.generation)
            self.assertTrue(all(identity._uuid4(value) for value in pair))
            self.assertNotEqual(*pair)
            self.assertEqual(archive.db.execute("PRAGMA user_version").fetchone(), (3,))
            actual = archive.db.execute("SELECT * FROM spans ORDER BY id").fetchall()
            self.assertEqual(actual, [row + (None,) * (3 if version == 1 else 0) for row in old])
            archive.close()
            unchanged = facts(path)
            again = store.Store(path)
            self.assertEqual((again.machine, again.generation), pair)
            again.close()
            self.assertEqual(facts(path), unchanged)
            self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_interrupt_and_commit_failure_roll_back_schema_signals_and_identity(self):
        original_connect = sqlite3.connect

        class FailedCommit(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("injected commit failure")

        for version in (0, 1, 2):
            for failure in ("interrupt", "commit"):
                path = self.directory / f"{version}-{failure}.db"
                if version:
                    legacy(path, version)
                    before = facts(path)
                if failure == "interrupt":
                    context = patch.object(identity.uuid, "uuid4", side_effect=KeyboardInterrupt)
                    error = KeyboardInterrupt
                else:
                    context = patch.object(store.sqlite3, "connect", side_effect=lambda *a, **k:
                                           original_connect(*a, **k, factory=FailedCommit))
                    error = sqlite3.OperationalError
                with context, self.assertRaises(error):
                    store.Store(path)
                db = sqlite3.connect(path)
                try:
                    self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (version,))
                    self.assertIsNone(db.execute("SELECT name FROM sqlite_master "
                                                 "WHERE name='diurna_identity'").fetchone())
                    if version == 0:
                        self.assertEqual(db.execute("SELECT name FROM sqlite_master").fetchall(), [])
                finally:
                    db.close()
                if version:
                    self.assertEqual(facts(path), before)
                store.Store(path).close()

    def test_competing_initializers_observe_one_complete_identity(self):
        legacy(self.path, 1)
        barrier = threading.Barrier(2)
        connect = sqlite3.connect
        pairs, errors = [], []

        def synchronized(*args, **kwargs):
            db = connect(*args, **kwargs)
            barrier.wait()
            return db

        def initialize():
            try:
                archive = store.Store(self.path)
                try:
                    pairs.append((archive.machine, archive.generation))
                finally:
                    archive.close()
            except BaseException as error:
                errors.append(error)

        with patch.object(store.sqlite3, "connect", side_effect=synchronized):
            threads = [threading.Thread(target=initialize) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(pairs), 2)
        self.assertEqual(pairs[0], pairs[1])

    def test_established_identity_damage_never_remints(self):
        mutations = ["DROP TABLE diurna_identity", "DELETE FROM diurna_identity",
                     "UPDATE diurna_identity SET machine='bad'",
                     "UPDATE diurna_identity SET generation=upper(generation)",
                     "UPDATE diurna_identity SET format='future'",
                     "UPDATE diurna_identity SET machine='00000000-0000-1000-8000-000000000000'",
                     "UPDATE diurna_identity SET generation='00000000-0000-4000-0000-000000000000'",
                     "ALTER TABLE diurna_identity RENAME TO old_identity; "
                     "CREATE TABLE diurna_identity AS SELECT * FROM old_identity; "
                     "INSERT INTO diurna_identity SELECT * FROM old_identity"]
        for i, mutation in enumerate(mutations):
            path = self.directory / f"damaged-{i}.db"
            store.Store(path).close()
            db = sqlite3.connect(path)
            db.executescript(mutation)
            db.close()
            before = path.read_bytes()
            with self.assertRaises((ValueError, sqlite3.Error)):
                store.Store(path)
            self.assertEqual(path.read_bytes(), before)

    def test_incomplete_schema_two_rolls_back_instead_of_completing_upgrade(self):
        legacy(self.path, 1)
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA user_version=2")
        db.close()
        before = facts(self.path)
        with self.assertRaises(sqlite3.Error):
            store.Store(self.path)
        self.assertEqual(facts(self.path), before)

    def test_unsupported_and_unversioned_nonempty_state_is_unchanged(self):
        for version in (0, 4):
            path = self.directory / f"unsupported-{version}.db"
            legacy(path, version)
            before = facts(path)
            with self.assertRaises(ValueError):
                store.Store(path)
            self.assertEqual(facts(path), before)

    def test_complete_backup_and_restore_retain_identity_new_history_does_not(self):
        archive = store.Store(self.path)
        archive.write(Span(-10, 10, "app", "1", "active"))
        pair = (archive.machine, archive.generation)
        archive.close()
        backup = self.directory / "backup.db"
        shutil.copy2(self.path, backup)
        self.path.unlink()
        shutil.copy2(backup, self.path)
        restored = store.Store(self.path)
        self.assertEqual((restored.machine, restored.generation), pair)
        restored.close()
        # Explicitly choose an empty, separate archive; retained spans stay named.
        separate = store.Store(self.directory / "new-history.db")
        self.assertTrue(set(pair).isdisjoint((separate.machine, separate.generation)))
        separate.close()
        self.assertEqual(self.path.read_bytes(), backup.read_bytes())

    def test_offline_migration_requires_existing_state_and_collector_lock(self):
        with self.assertRaises(FileNotFoundError):
            store.migrate(self.path)
        self.assertFalse(self.path.exists())
        legacy(self.path, 2)
        alias = self.directory / "alias.db"
        alias.symlink_to(self.path)
        with self.assertRaises(ValueError):
            store.migrate(alias)
        alias.unlink()
        os.link(self.path, alias)
        with self.assertRaises(ValueError):
            store.migrate(alias)
        alias.unlink()
        before = facts(self.path)
        with self.path.with_name(self.path.name + ".lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaises(BlockingIOError):
                store.migrate(self.path)
        self.assertEqual(facts(self.path), before)
        store.migrate(self.path)
        self.assertEqual(facts(self.path)[0], (3,))
