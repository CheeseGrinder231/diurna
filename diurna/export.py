"""Bounded, read-only raw JSONL snapshots. No classifier or desktop dependency."""
import json
import os
from pathlib import Path
import sqlite3
import stat

from . import identity

FORMAT = "diurna.raw-spans"
VERSION = 1
MIN_NS, MAX_NS = -(2 ** 63), 2 ** 63 - 1
MAX_ROWS = 10_000
MAX_STRING_BYTES = 4096
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_SQL_STEPS = 10_000_000
REASONS = ("active", "idle", "locked", "suspended", "disconnected", "unknown")
ERROR = "diurna: raw export failed\n"


def _integer(value):
    return type(value) is int and MIN_NS <= value <= MAX_NS


def _request(start_ns, end_ns, through_start_ns):
    if (not _integer(start_ns) or not _integer(end_ns) or start_ns >= end_ns
            or (through_start_ns is not None and not _integer(through_start_ns))):
        raise ValueError("invalid raw range")


def _connect(path):
    path = Path(path).absolute()
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise ValueError("unsafe raw source")
    # mode=ro never creates the database or migrates it; immutable would miss WAL.
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)


def export(database, output, start_ns, end_ns, through_start_ns=None):
    """Write UTF-8 bytes, ending in a completion record only on full success.

    The first source read pins a SQLite snapshot. All selected intervals overlap
    [start_ns, end_ns) and start at/before the saved high point. No clipping.
    Errors propagate to the CLI's single sanitized diagnostic.
    """
    _request(start_ns, end_ns, through_start_ns)
    used = 0

    def emit(record):
        nonlocal used
        data = (json.dumps({"format": FORMAT, "version": VERSION, **record},
                           ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                + "\n").encode("utf-8")
        if used + len(data) > MAX_OUTPUT_BYTES:
            raise ValueError("raw output budget")
        used += len(data)
        if output.write(data) != len(data):
            raise OSError("incomplete raw write")

    db = _connect(database)
    try:
        remaining = MAX_SQL_STEPS

        def progress():
            nonlocal remaining
            remaining -= 1000
            return int(remaining < 0)

        db.set_progress_handler(progress, 1000)
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA cache_size=-2048")
        db.execute("BEGIN")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version != 3:
            raise ValueError("raw source requires migration")
        machine, generation = identity.read(db)
        kind, high = db.execute("""SELECT typeof(MAX(start_ns)),
            CASE WHEN typeof(MAX(start_ns)) = 'integer' THEN MAX(start_ns) END
            FROM spans""").fetchone()
        if kind not in ('null', 'integer') or (high is not None and not _integer(high)):
            raise ValueError("invalid raw high point")
        if high is not None and through_start_ns is not None:
            high = min(high, through_start_ns)
        emit({"type": "snapshot", "machine": machine, "generation": generation,
              "startNs": start_ns, "endNs": end_ns, "throughStartNs": high})
        # Select bounded strings without ever loading a title or oversized value
        # into Python. The +1 row detects exhaustion rather than truncating.
        text = lambda name: (f"CASE WHEN typeof({name}) = 'text' AND "
                             f"length(CAST({name} AS BLOB)) <= {MAX_STRING_BYTES} "
                             f"THEN {name} ELSE NULL END")
        cursor = db.execute(f"""SELECT
            CASE WHEN typeof(start_ns) = 'integer' THEN start_ns END,
            CASE WHEN typeof(end_ns) = 'integer' THEN end_ns END, {text('app_class')},
            {text('workspace')}, {text('reason')},
            CASE WHEN input IS NULL OR (typeof(input) = 'integer' AND input IN (0,1))
                THEN input ELSE 2 END,
            CASE WHEN media IS NULL OR (typeof(media) = 'integer' AND media IN (0,1))
                THEN media ELSE 2 END,
            CASE WHEN inhibiting IS NULL OR (typeof(inhibiting) = 'integer' AND inhibiting IN (0,1))
                THEN inhibiting ELSE 2 END,
            CASE WHEN typeof(active) = 'integer' THEN active ELSE 2 END, title IS NULL FROM spans
            WHERE ((end_ns > ? AND start_ns < ?)
                OR typeof(start_ns) != 'integer' OR typeof(end_ns) != 'integer'
                OR end_ns <= start_ns)
                AND (start_ns <= ? OR typeof(start_ns) != 'integer')
            ORDER BY start_ns LIMIT ?""", (start_ns, end_ns, high, MAX_ROWS + 1))
        count, previous_end = 0, None
        for start, end, app, workspace, reason, input_, media, inhibiting, active, no_title \
                in cursor:
            if (count >= MAX_ROWS or not _integer(start) or not _integer(end)
                    or start >= end or (previous_end is not None and start < previous_end)
                    or not isinstance(app, str) or not isinstance(workspace, str)
                    or reason not in REASONS or not no_title
                    or type(active) is not int or active != int(reason == "active")
                    or any(value is not None and (type(value) is not int or value not in (0, 1))
                           for value in (input_, media, inhibiting))):
                raise ValueError("invalid raw span")
            count += 1
            previous_end = end
            emit({"type": "span", "machine": machine, "generation": generation,
                  "id": f"span:{start}", "startNs": start, "endNs": end,
                  "appClass": app, "workspace": workspace, "reason": reason,
                  "input": None if input_ is None else bool(input_),
                  "media": None if media is None else bool(media),
                  "inhibiting": None if inhibiting is None else bool(inhibiting)})
        emit({"type": "complete", "rows": count})
    finally:
        db.close()
