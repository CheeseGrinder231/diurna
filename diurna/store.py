"""Append-only SQLite spans. Gaps between collector runs are explicitly unknown."""
import fcntl
import os
from pathlib import Path
import sqlite3
import stat

from . import identity
from .collector import Span

SCHEMA_VERSION = 3
# Raw activity signals, added in version 2. NULL means no source reported it:
# every row written before, or without the shell plugin, reads as plain active.
SIGNALS = ("input", "media", "inhibiting")


def _flag(value):
    return None if value is None else int(value)


class Store:
    def __init__(self, path):
        path = Path(path)
        # Create private state even when called outside the daemon's umask.
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        self.db = sqlite3.connect(path.absolute().as_uri() + "?mode=rw", uri=True)
        try:
            # Lock BEFORE reading the version: competing initializers must see
            # the winner's complete upgrade, never its old version.
            self.db.execute("BEGIN IMMEDIATE")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, SCHEMA_VERSION):
                raise ValueError("unsupported diurna schema")
            if version == 0:
                if self.db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
                    raise ValueError("unversioned diurna state")
                self.db.execute("""CREATE TABLE spans (
                    id INTEGER PRIMARY KEY,
                    start_ns INTEGER NOT NULL,
                    end_ns INTEGER NOT NULL CHECK(end_ns > start_ns),
                    app_class TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    active INTEGER NOT NULL CHECK(active IN (0, 1)),
                    reason TEXT NOT NULL CHECK(reason IN
                        ('active','idle','locked','suspended','disconnected','unknown')),
                    title TEXT CHECK(title IS NULL),
                    CHECK(active = (reason = 'active')),
                    UNIQUE(start_ns)
                )""")
                self.db.execute("CREATE INDEX spans_end ON spans(end_ns)")
            # Version 2 already has these columns. Do not use SCHEMA_VERSION.
            if version < 2:
                for name in SIGNALS:
                    self.db.execute(f"ALTER TABLE spans ADD COLUMN {name} INTEGER "
                                    f"CHECK({name} IN (0, 1))")
            if version < 3:
                identity.create(self.db)
            self.db.execute("""SELECT id,start_ns,end_ns,app_class,workspace,
                active,reason,title,input,media,inhibiting FROM spans LIMIT 0""")
            self.machine, self.generation = identity.read(self.db)
            self.last_end = self.db.execute("SELECT MAX(end_ns) FROM spans").fetchone()[0]
            if version < SCHEMA_VERSION:
                self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.db.commit()
        except BaseException:
            try:
                self.db.rollback()
            finally:
                self.db.close()
            raise

    def resume(self, now):
        # Do not guess what happened while the process was not running.
        if self.last_end is not None:
            if now < self.last_end:
                raise ValueError("system clock precedes last stored span")
            if now > self.last_end:
                self.write(Span(self.last_end, now, "", "", "unknown"))

    def write(self, span):
        if self.last_end is not None and span.start_ns != self.last_end:
            raise ValueError("non-contiguous span")
        with self.db:
            self.db.execute("""INSERT INTO spans
                (start_ns,end_ns,app_class,workspace,active,reason,title,input,media,inhibiting)
                VALUES (?,?,?,?,?,?,NULL,?,?,?)""",
                (span.start_ns, span.end_ns, span.app_class, span.workspace,
                 int(span.reason == "active"), span.reason,
                 _flag(span.input), _flag(span.media), _flag(span.inhibiting)))
        self.last_end = span.end_ns

    def close(self):
        self.db.close()


def migrate(path):
    """Explicit offline migration of existing state, excluding a live collector."""
    path = Path(path).absolute()
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise ValueError("unsafe diurna state")
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        Store(path).close()
