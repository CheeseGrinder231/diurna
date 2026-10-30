"""Append-only SQLite spans. Gaps between collector runs are explicitly unknown."""
import sqlite3

from .collector import Span

SCHEMA_VERSION = 2
# Raw activity signals, added in version 2. NULL means no source reported it:
# every row written before, or without the shell plugin, reads as plain active.
SIGNALS = ("input", "media", "inhibiting")


def _flag(value):
    return None if value is None else int(value)


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        try:
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, SCHEMA_VERSION):
                raise ValueError("unsupported diurna schema")
            # One transaction: an interrupted upgrade leaves version 1 as it was.
            self.db.executescript("BEGIN;" + """
                CREATE TABLE IF NOT EXISTS spans (
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
                );
                CREATE INDEX IF NOT EXISTS spans_end ON spans(end_ns);
                """ + "".join(
                    f"ALTER TABLE spans ADD COLUMN {name} INTEGER CHECK({name} IN (0, 1));"
                    for name in SIGNALS if version < SCHEMA_VERSION)
                + f"PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;")
            self.last_end = self.db.execute("SELECT MAX(end_ns) FROM spans").fetchone()[0]
        except BaseException:
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
