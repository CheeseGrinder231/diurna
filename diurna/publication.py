"""Bounded finished journal, separate from raw commits and raw schema version.

All source reads, verification and sequence allocation run in one serialized
transaction. No wall clock, desktop, hot settings, network, or ingestion ack.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from . import identity
from .export import _connect, REASONS
from .screen_classifier import RULE, barriers, classify
from .screen_periods import periods

FORMAT = "diurna.screen-use-journal-v1"
MAX_ROWS = 4096
MAX_BYTES = 16 * 1024 * 1024
MAX_STRING_BYTES = 4096
MAX_SQL_STEPS = 5_000_000
U64_MAX = 2**64 - 1
MAX_NS = 2**63 - 1
TABLES = {"screen_use_state", "screen_use_journal"}


class PublicationBlocked(ValueError):
    """Requires intervention; callers must not reset or replace the stream."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=True, allow_nan=False)


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require(condition, code):
    if not condition:
        raise PublicationBlocked(code)


def _sequence(value):
    _require(isinstance(value, str) and value.isascii() and value.isdecimal()
             and len(value) <= 20, "invalid sequence")
    result = int(value)
    _require(str(result) == value and 0 <= result <= U64_MAX, "invalid sequence")
    return result


def utc_ns(value):
    _require(type(value) is int and 0 < value <= MAX_NS, "invalid boundary")
    seconds, nanos = divmod(value, 1_000_000_000)
    date = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return date.strftime("%Y-%m-%dT%H:%M:%S") + f".{nanos:09d}Z"


def occurrence(period, sequence, machine, generation):
    _require(type(sequence) is int and 0 < sequence <= U64_MAX, "invalid sequence")
    _require(period.end_ns > period.start_ns, "invalid interval")
    apps = [app for app, _ in period.apps]
    if period.application is not None:
        apps.append(period.application)
    for app in apps:
        _require(isinstance(app, str) and 0 < len(app.encode("utf-8")) <= MAX_STRING_BYTES
                 and all(c.isprintable() and not c.isspace() and c not in '/\\' for c in app),
                 "invalid application")
    activity = {"kind": "screen-use", "useMode": "mixed"}
    if period.application is not None:
        activity.update(useMode="application", application=period.application)
    activity["activeDurationNs"] = str(period.end_ns - period.start_ns)
    return {"machine": machine, "generation": generation,
            "id": f"screen-use:{sequence}", "start": utc_ns(period.start_ns),
            "end": utc_ns(period.end_ns), "activity": activity}


@dataclass(frozen=True)
class Scope:
    format: str
    rule: str
    machine: str
    generation: str
    status: str
    watermark_ns: int | None
    through_sequence: int
    retention_floor_sequence: int
    original_beginning_ns: int | None
    retained_all: bool = True


@dataclass(frozen=True)
class Entry:
    sequence: int
    occurrence: dict


def _budget(db):
    _require(MAX_SQL_STEPS > 0, "SQL step budget")
    remaining = MAX_SQL_STEPS
    granularity = min(1000, MAX_SQL_STEPS)

    def progress():
        nonlocal remaining
        remaining -= granularity
        return int(remaining < 0)

    db.set_progress_handler(progress, granularity)


def _source(db):
    _require(db.execute("PRAGMA user_version").fetchone()[0] == 3, "migration required")
    # Bound text in SQLite before decoding. Never load titles.
    def text(name):
        return (f"CASE WHEN typeof({name})='text' AND length(CAST({name} AS BLOB))"
                f" <= {MAX_STRING_BYTES} THEN {name} END")

    def integer(name):
        return f"CASE WHEN typeof({name})='integer' THEN {name} END"

    def signal(name):
        return (f"CASE WHEN {name} IS NULL OR (typeof({name})='integer' AND "
                f"{name} IN (0,1)) THEN {name} ELSE 2 END")

    rows = db.execute(f"""SELECT {integer('start_ns')},{integer('end_ns')},{text('app_class')},
        {text('workspace')},{text('reason')},NULL,{signal('input')},{signal('media')},
        {signal('inhibiting')},{integer('active')},title IS NULL
        FROM spans ORDER BY start_ns LIMIT ?""", (MAX_ROWS + 1,)).fetchall()
    _require(len(rows) <= MAX_ROWS, "source row budget")
    result, previous = [], None
    for start, end, app, workspace, reason, _, input_, media, inhibiting, active, no_title in rows:
        _require(type(start) is int and type(end) is int and 0 < start < end <= MAX_NS
                 and (previous is None or start >= previous), "invalid source boundaries")
        _require(isinstance(app, str) and isinstance(workspace, str) and reason in REASONS
                 and type(active) is int and active == int(reason == "active") and no_title,
                 "invalid source observation")
        _require(all(v is None or (type(v) is int and v in (0, 1))
                     for v in (input_, media, inhibiting)), "invalid source signals")
        # Empty app is valid raw state, but cannot name a finished application.
        result.append([start, end, app, workspace, reason, None, input_, media, inhibiting])
        previous = end
    _require(len(_json(result).encode("utf-8")) <= MAX_BYTES, "source byte budget")
    return result


def _initialize(db):
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    columns = {r[1] for r in db.execute("PRAGMA table_info(diurna_identity)")}
    if "screen_use_guard" in columns:
        guard = db.execute("SELECT screen_use_guard = ? FROM diurna_identity LIMIT 2",
                           (FORMAT,)).fetchall()
        _require(guard == [(1,)] and TABLES <= names, "lost or unsupported journal")
        return
    _require(not TABLES & names, "orphan journal")
    db.execute("ALTER TABLE diurna_identity ADD COLUMN screen_use_guard TEXT")
    db.execute("UPDATE diurna_identity SET screen_use_guard=?", (FORMAT,))
    db.execute("""CREATE TABLE screen_use_state (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        document TEXT NOT NULL, digest TEXT NOT NULL, error TEXT)""")
    db.execute("""CREATE TABLE screen_use_journal (
        sequence TEXT PRIMARY KEY, occurrence_id TEXT NOT NULL UNIQUE,
        payload TEXT NOT NULL, evidence TEXT NOT NULL)""")
    machine, generation = identity.read(db)
    state = _state([], machine, generation, [], 0)
    data = _json(state)
    db.execute("INSERT INTO screen_use_state VALUES(1,?,?,NULL)", (data, _digest(data)))


def _state(source, machine, generation, journal, sequence):
    evidence = barriers(source)
    watermark = evidence[-1][0] if evidence else None
    classified = classify(source)
    return {"format": FORMAT, "rule": RULE, "machine": machine, "generation": generation,
            "source": source, "barriers": evidence, "watermark": watermark,
            # Keep all context, including across cuts; never trim to W or midnight.
            "pending": [r for r in classified if watermark is None or r[1] > watermark],
            "sequence": str(sequence), "journalDigest": _digest(_json(journal))}


def _journal(db):
    # Limit cells in SQLite: corruption must not defeat the memory budget.
    count, size = db.execute("""SELECT COUNT(*), COALESCE(SUM(
        length(CAST(sequence AS BLOB))+length(CAST(occurrence_id AS BLOB))+
        length(CAST(payload AS BLOB))+length(CAST(evidence AS BLOB))),0)
        FROM (SELECT * FROM screen_use_journal LIMIT ?)""", (MAX_ROWS + 1,)).fetchone()
    _require(count <= MAX_ROWS and size <= MAX_BYTES, "journal budget")
    rows = db.execute("""SELECT sequence,
        CASE WHEN length(CAST(occurrence_id AS BLOB))<=64 THEN occurrence_id END,
        CASE WHEN length(CAST(payload AS BLOB))<=16384 THEN payload END,
        CASE WHEN length(CAST(evidence AS BLOB))<=? THEN evidence END
        FROM screen_use_journal LIMIT ?""", (MAX_BYTES, MAX_ROWS + 1)).fetchall()
    _require(len(rows) <= MAX_ROWS, "journal row budget")
    _require(all(all(isinstance(cell, str) for cell in row) for row in rows), "invalid journal")
    _require(sum(len(cell.encode("utf-8")) for row in rows for cell in row) <= MAX_BYTES,
             "journal byte budget")
    rows.sort(key=lambda row: _sequence(row[0]))
    return [list(row) for row in rows]


def _audit(db, source):
    machine, generation = identity.read(db)
    columns = {r[1] for r in db.execute("PRAGMA table_info(diurna_identity)")}
    _require("screen_use_guard" in columns, "journal not initialized")
    _require(db.execute("SELECT screen_use_guard = ? FROM diurna_identity LIMIT 2",
                        (FORMAT,)).fetchall() == [(1,)],
             "unsupported journal guard")
    states = db.execute("""SELECT singleton,
        CASE WHEN typeof(document)='text' AND length(CAST(document AS BLOB))<=? THEN document END,
        CASE WHEN typeof(digest)='text' AND length(CAST(digest AS BLOB))=64 THEN digest END,
        error IS NULL
        FROM screen_use_state LIMIT 2""", (MAX_BYTES,)).fetchall()
    _require(len(states) == 1 and states[0][0] == 1, "lost journal state")
    _, document, digest, healthy = states[0]
    _require(healthy == 1, "stream blocked; intervention required")
    _require(isinstance(document, str) and _digest(document) == digest, "corrupt journal state")
    state = json.loads(document)
    _require(isinstance(state, dict) and state.get("format") == FORMAT
             and state.get("rule") == RULE, "unsupported rule")
    old_source = state.get("source")
    _require(isinstance(old_source, list) and source[:len(old_source)] == old_source,
             "source history changed")
    journal = _journal(db)
    sequence = _sequence(state.get("sequence"))
    _require(sequence == len(journal), "lost journal or sequence")
    _require(document == _json(_state(old_source, machine, generation, journal, sequence)),
             "state or identity changed")
    old_periods = periods(old_source, state["watermark"], MAX_ROWS)
    _require(len(old_periods) == len(journal), "published grouping changed")
    for number, (period, row) in enumerate(zip(old_periods, journal), 1):
        expected = occurrence(period, number, machine, generation)
        _require(row == [str(number), expected["id"], _json(expected), _json(asdict(period))],
                 "published fact changed")
    return state, journal, machine, generation


def _block(db):
    """Best effort durable latch. Structural loss also fails every later audit."""
    try:
        db.rollback()
        # A step-budget failure must not prevent the small error transaction.
        db.set_progress_handler(None, 0)
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE screen_use_state SET error='publication blocked; intervention required'")
        db.commit()
    except sqlite3.Error:
        db.rollback()


def advance(db, fault=None):
    """Advance atomically from committed history. Never called in a raw transaction.

    fault(stage) is only an injection seam for synthetic storage witnesses.
    Competing pumps serialize before taking the source snapshot. A failed or
    uncertain commit latches the stream; it never rolls back the caller's raw work.
    """
    _require(not db.in_transaction, "publication requires separate transaction")
    fault = fault or (lambda stage: None)
    try:
        _budget(db)
        db.execute("BEGIN IMMEDIATE")
        identity.read(db)
        _initialize(db)
        # Establish the guard even when the first source replay exceeds budget.
        # Nothing has been advertised or allocated in this initialization.
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        source = _source(db)
        state, journal, machine, generation = _audit(db, source)
        evidence = barriers(source)
        watermark = evidence[-1][0] if evidence else None
        _require(state["watermark"] is None or (watermark is not None
                 and watermark >= state["watermark"]), "watermark regressed")
        complete = periods(source, watermark, MAX_ROWS)
        _require(len(complete) >= len(journal), "published domains lost")
        for index, period in enumerate(complete):
            sequence = index + 1
            _require(sequence <= U64_MAX, "sequence exhausted")
            payload = occurrence(period, sequence, machine, generation)
            row = [str(sequence), payload["id"], _json(payload), _json(asdict(period))]
            if index < len(journal):
                _require(journal[index] == row, "published grouping changed")
                continue
            fault("before_sequence")
            # Allocation, payload and grouping evidence share the transaction.
            fault("after_sequence")
            db.execute("INSERT INTO screen_use_journal VALUES(?,?,?,?)", row)
            journal.append(row)
            fault("after_journal")
        document = _json(_state(source, machine, generation, journal, len(journal)))
        _require(len(document.encode("utf-8")) <= MAX_BYTES
                 and len(_json(journal).encode("utf-8")) <= MAX_BYTES, "state byte budget")
        fault("before_watermark")
        db.execute("UPDATE screen_use_state SET document=?,digest=? WHERE singleton=1",
                   (document, _digest(document)))
        fault("after_watermark")
        fault("before_commit")
        db.commit()
        fault("after_commit")
        return _scope(json.loads(document))
    except (ValueError, TypeError, KeyError, IndexError, RuntimeError,
            OSError, sqlite3.Error, OverflowError, MemoryError):
        _block(db)
        raise PublicationBlocked("publication blocked; intervention required") from None
    finally:
        db.set_progress_handler(None, 0)


def _scope(state):
    source = state["source"]
    return Scope(FORMAT, RULE, state["machine"], state["generation"], "ready",
                 state["watermark"], _sequence(state["sequence"]), 1,
                 source[0][0] if source else None)


class Journal:
    """Typed, pinned read interface for a future feed. No grouping in consumers.

    This implementation audits the complete bounded archive before advertising
    anything. Read/export never advances publication, acknowledges intake or
    changes the retention floor. Use advance/catch_up explicitly to catch up.
    """
    def __init__(self, path):
        self.db = _connect(path)
        try:
            _budget(self.db)
            self.db.execute("PRAGMA query_only=ON")
            self.db.execute("PRAGMA trusted_schema=OFF")
            self.db.execute("BEGIN")
            source = _source(self.db)
            self.state, self.rows, _, _ = _audit(self.db, source)
        except BaseException:
            self.db.close()
            raise

    def scope(self):
        return _scope(self.state)

    def entries(self, after_sequence=0, limit=MAX_ROWS):
        _require(type(after_sequence) is int and 0 <= after_sequence <= U64_MAX
                 and type(limit) is int and 0 < limit <= MAX_ROWS, "invalid journal range")
        return tuple(Entry(_sequence(row[0]), json.loads(row[2]))
                     for row in self.rows if _sequence(row[0]) > after_sequence)[:limit]

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def catch_up(path):
    # Reuse raw export's private-file checks without creating or migrating state.
    check = _connect(path)
    check.close()
    db = sqlite3.connect(Path(path).absolute().as_uri() + '?mode=rw',
                         uri=True, timeout=1)
    try:
        return advance(db)
    finally:
        db.close()


def export(path, output, start_ns, end_ns):
    _require(type(start_ns) is int and type(end_ns) is int
             and 0 < start_ns < end_ns <= MAX_NS, "invalid finished range")
    with Journal(path) as journal:
        scope = journal.scope()
        selected = []
        # Boundaries are retained in journal evidence; never clip occurrences.
        for row in journal.rows:
            evidence = json.loads(row[3])
            if evidence['end_ns'] > start_ns and evidence['start_ns'] < end_ns:
                selected.append({"type": "occurrence", "sequence": row[0],
                                 "occurrence": json.loads(row[2])})
        records = [{"type": "scope", **asdict(scope)}, *selected,
                   {"type": "complete", "rows": len(selected)}]
        data = ''.join(_json(r) + '\n' for r in records).encode('utf-8')
        _require(len(data) <= MAX_BYTES, "finished output budget")
        if output.write(data) != len(data):
            raise OSError("incomplete finished write")
