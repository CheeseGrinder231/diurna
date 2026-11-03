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
from .publication_limits import PublicationWaiting
from .screen_periods import periods

FORMAT = "diurna.screen-use-journal-v2"
MAX_ROWS = 4096
MAX_BYTES = 16 * 1024 * 1024
MAX_STRING_BYTES = 4096
MAX_SQL_STEPS = 5_000_000
U64_MAX = 2**64 - 1
MAX_NS = 2**63 - 1
TABLES = {"screen_use_state", "screen_use_journal", "screen_use_checkpoints", "screen_use_seal"}
TRIGGERS = {"screen_use_" + name for name in
            ("source_update", "source_delete", "source_insert", "journal_update",
             "journal_delete", "journal_insert", "checkpoint_update", "checkpoint_delete")}



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


def _within(condition, code):
    if not condition:
        raise PublicationWaiting(code)


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
    _within(MAX_SQL_STEPS > 0, "SQL step budget")
    remaining = MAX_SQL_STEPS
    exhausted = [False]
    granularity = min(1000, MAX_SQL_STEPS)

    def progress():
        nonlocal remaining
        remaining -= granularity
        if remaining < 0:
            exhausted[0] = True
            return 1
        return 0

    db.set_progress_handler(progress, granularity)
    return exhausted


def _source(db, lower=0, paging=False):
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

    predicate = "WHERE start_ns >= ?" if lower else ""
    parameters = (lower, MAX_ROWS + 1) if lower else (MAX_ROWS + 1,)
    rows = db.execute(f"""SELECT {integer('start_ns')},{integer('end_ns')},{text('app_class')},
        {text('workspace')},{text('reason')},NULL,{signal('input')},{signal('media')},
        {signal('inhibiting')},{integer('active')},title IS NULL
        FROM spans {predicate} ORDER BY start_ns LIMIT ?""", parameters).fetchall()
    if paging:
        rows = rows[:MAX_ROWS]
    else:
        _within(len(rows) <= MAX_ROWS, "source row budget")
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
    _within(len(_json(result).encode("utf-8")) <= MAX_BYTES, "source byte budget")
    return result


def _initialize(db):
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    columns = {r[1] for r in db.execute("PRAGMA table_info(diurna_identity)")}
    if "screen_use_guard" in columns:
        guard = db.execute("SELECT screen_use_guard = ? FROM diurna_identity LIMIT 2",
                           (FORMAT,)).fetchall()
        _require(guard == [(1,)] and TABLES <= names, "lost or unsupported journal")
        _guards(db)
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
    db.execute("CREATE TABLE screen_use_seal(singleton INTEGER PRIMARY KEY CHECK(singleton=1), lower_ns INTEGER NOT NULL)")
    db.execute("INSERT INTO screen_use_seal VALUES(1,0)")
    db.execute("""CREATE TABLE screen_use_checkpoints (
        page INTEGER PRIMARY KEY, document TEXT NOT NULL, digest TEXT NOT NULL,
        previous_digest TEXT, through_length INTEGER NOT NULL, through_sequence TEXT NOT NULL)""")
    db.execute("CREATE INDEX screen_use_checkpoint_sequence ON screen_use_checkpoints(through_length,through_sequence)")
    db.execute("CREATE INDEX screen_use_journal_numeric ON screen_use_journal(length(sequence),sequence)")
    for table, prefix in (("spans", "source"), ("screen_use_journal", "journal"),
                          ("screen_use_checkpoints", "checkpoint")):
        for operation in ("UPDATE", "DELETE"):
            db.execute(f"""CREATE TRIGGER screen_use_{prefix}_{operation.lower()}
                AFTER {operation} ON {table} BEGIN
                UPDATE screen_use_state SET error='publication blocked; intervention required'; END""")
    # Keep the raw insert guard independent of derived tables: even lost
    # publication storage must never stop ordinary append-only collection.
    db.execute("""CREATE TRIGGER screen_use_source_insert BEFORE INSERT ON spans
        WHEN NEW.start_ns < (SELECT MAX(start_ns) FROM spans)
        BEGIN SELECT RAISE(ABORT,'source must be append-only'); END""")
    db.execute("""CREATE TRIGGER screen_use_journal_insert BEFORE INSERT ON screen_use_journal
        WHEN typeof(NEW.sequence) != 'text' OR NEW.sequence NOT GLOB '[1-9]*'
        OR NEW.sequence GLOB '*[^0-9]*' OR length(NEW.sequence)>20
        OR (length(NEW.sequence),NEW.sequence) <=
           (SELECT length(sequence),sequence FROM screen_use_journal
            ORDER BY length(sequence) DESC,sequence DESC LIMIT 1)
        BEGIN SELECT RAISE(ABORT,'journal must be append-only'); END""")
    machine, generation = identity.read(db)
    state = _state([], machine, generation, [], 0)
    data = _json(state)
    db.execute("INSERT INTO screen_use_state VALUES(1,?,?,NULL)", (data, _digest(data)))


def _state(source, machine, generation, journal, sequence, page=None):
    page = dict(page or {"base": 0, "lower": 0, "beginning": None,
                         "count": 0, "head": None})
    if page["beginning"] is None and source:
        page["beginning"] = source[0][0]
    evidence = barriers(source)
    watermark = evidence[-1][0] if evidence else None
    classified = classify(source)
    return {"format": FORMAT, "rule": RULE, "machine": machine, "generation": generation,
            "page": page,
            "source": source, "barriers": evidence, "watermark": watermark,
            # Keep all context, including across cuts; never trim to W or midnight.
            "pending": [r for r in classified if watermark is None or r[1] > watermark],
            "sequence": str(sequence), "journalDigest": _digest(_json(journal))}


def _journal(db, base=0, through=U64_MAX):
    bounds = (len(str(base)), str(base), len(str(through)), str(through))
    where = "WHERE (length(sequence),sequence)>(?,?) AND (length(sequence),sequence)<=(?,?)"
    # Limit cells in SQLite: corruption must not defeat the memory budget.
    count, size = db.execute(f"""SELECT COUNT(*), COALESCE(SUM(
        length(CAST(sequence AS BLOB))+length(CAST(occurrence_id AS BLOB))+
        length(CAST(payload AS BLOB))+length(CAST(evidence AS BLOB))),0)
        FROM (SELECT * FROM screen_use_journal {where} LIMIT ?)""", (*bounds, MAX_ROWS + 1)).fetchone()
    _within(count <= MAX_ROWS and size <= MAX_BYTES, "journal budget")
    rows = db.execute(f"""SELECT sequence,
        CASE WHEN length(CAST(occurrence_id AS BLOB))<=64 THEN occurrence_id END,
        CASE WHEN length(CAST(payload AS BLOB))<=16384 THEN payload END,
        CASE WHEN length(CAST(evidence AS BLOB))<=? THEN evidence END
        FROM screen_use_journal {where} LIMIT ?""", (MAX_BYTES, *bounds, MAX_ROWS + 1)).fetchall()
    _within(len(rows) <= MAX_ROWS, "journal row budget")
    _require(all(all(isinstance(cell, str) for cell in row) for row in rows), "invalid journal")
    _within(sum(len(cell.encode("utf-8")) for row in rows for cell in row) <= MAX_BYTES,
             "journal byte budget")
    rows.sort(key=lambda row: _sequence(row[0]))
    return [list(row) for row in rows]


def _guards(db):
    triggers = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    _require(TRIGGERS <= triggers, "lost immutability guards")


def _read_state(db):
    machine, generation = identity.read(db)
    _guards(db)
    _require(db.execute("SELECT screen_use_guard FROM diurna_identity LIMIT 2").fetchall()
             == [(FORMAT,)], "unsupported journal guard")
    sizes = db.execute("SELECT typeof(document),length(CAST(document AS BLOB)) FROM screen_use_state LIMIT 2").fetchall()
    _require(len(sizes) == 1 and sizes[0][0] == "text", "lost or invalid journal state")
    _within(sizes[0][1] <= MAX_BYTES, "state byte budget")
    records = db.execute("SELECT singleton,document,digest,error FROM screen_use_state LIMIT 2").fetchall()
    _require(len(records) == 1 and records[0][0] == 1, "lost journal state")
    _, document, digest, error = records[0]
    _require(error is None, "stream blocked; intervention required")
    _require(_digest(document) == digest, "corrupt journal state")
    state = json.loads(document)
    _require(isinstance(state, dict) and state.get("format") == FORMAT
             and state.get("rule") == RULE,
             "unsupported rule")
    page = state["page"]
    _require(db.execute("SELECT singleton,lower_ns FROM screen_use_seal LIMIT 2").fetchall()
             == [(1, page["lower"])], "seal changed")
    head = db.execute("SELECT page,digest FROM screen_use_checkpoints ORDER BY page DESC LIMIT 1").fetchone()
    _require(head == ((page["count"], page["head"]) if page["count"] else None),
             "lost checkpoint")
    return state, machine, generation


def _audit_segment(db, state, machine, generation):
    base = state["page"]["base"]
    sequence = _sequence(state["sequence"])
    journal = _journal(db, base, sequence)
    _require(sequence - base == len(journal), "lost journal or sequence")
    expected = _state(state["source"], machine, generation, journal, sequence, state["page"])
    _require(state == json.loads(_json(expected)), "state or identity changed")
    old_periods = periods(state["source"], state["watermark"], MAX_ROWS)
    _require(len(old_periods) == len(journal), "published grouping changed")
    for number, (period, row) in enumerate(zip(old_periods, journal), base + 1):
        expected = occurrence(period, number, machine, generation)
        _require(row == [str(number), expected["id"], _json(expected), _json(asdict(period))],
                 "published fact changed")
    return journal


def _audit(db, source, state=None):
    state, machine, generation = _read_state(db) if state is None else state
    old_source = state["source"]
    _require(isinstance(old_source, list), "invalid source state")
    _within(len(old_source) <= MAX_ROWS, "accepted source row budget")
    _require(source[:len(old_source)] == old_source,
             "source history changed")
    journal = _audit_segment(db, state, machine, generation)
    # No unaccounted later facts may exist outside the current segment.
    last = db.execute("SELECT sequence FROM screen_use_journal ORDER BY length(sequence) DESC,sequence DESC LIMIT 1").fetchone()
    _require(last == ((state["sequence"],) if state["sequence"] != "0" else None),
             "unexpected journal facts")
    return state, journal, machine, generation


def _checkpoint(db, state, source, journal, machine, generation):
    """Seal only at a classifier reset that also closes the grouping domain.

    Keep the committed reset row in both evidence and the pending suffix. It
    proves closure and preserves the actual source boundary without inventing
    inactivity or splitting a domain at an input barrier.
    """
    if len(source) < max(2, MAX_ROWS // 2):
        return source, journal, state["page"]
    classified = classify(source)
    candidates = {e[0] for e in barriers(source)}
    cut = None
    for i in range(1, len(source)):
        if source[i][0] in candidates and (source[i][0] > source[i-1][1]
                                           or classified[i-1][4] != "active"):
            cut = i
    if cut is None:
        return source, journal, state["page"]
    boundary = source[cut][0]
    saved = [r for r in journal if json.loads(r[3])["closure_ns"] <= boundary]
    page = dict(state["page"])
    if page["beginning"] is None:
        page["beginning"] = source[0][0]
    sequence = page["base"] + len(saved)
    sealed = _state(source[:cut+1], machine, generation, saved, sequence, page)
    document = _json(sealed)
    _within(len(document.encode("utf-8")) <= MAX_BYTES, "checkpoint byte budget")
    digest = _digest(document)
    number = page["count"] + 1
    db.execute("INSERT INTO screen_use_checkpoints VALUES(?,?,?,?,?,?)",
               (number, document, digest, page["head"], len(str(sequence)), str(sequence)))
    page.update(base=sequence, lower=boundary, count=number, head=digest)
    db.execute("UPDATE screen_use_seal SET lower_ns=? WHERE singleton=1", (boundary,))
    return source[cut:], journal[len(saved):], page


def _load_checkpoint(db, number, machine, generation):
    row = db.execute("""SELECT CASE WHEN length(CAST(document AS BLOB))<=? THEN document END,
        digest,previous_digest FROM screen_use_checkpoints WHERE page=?""", (MAX_BYTES, number)).fetchone()
    _require(row is not None, "lost checkpoint")
    document, digest, previous = row
    _within(document is not None, "checkpoint byte budget")
    _require(_digest(document) == digest, "corrupt checkpoint")
    state = json.loads(document)
    _require(state["page"]["count"] == number - 1 and state["page"]["head"] == previous,
             "checkpoint chain changed")
    return state, _audit_segment(db, state, machine, generation), digest


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
    uncertain commit latches the stream; a known resource ceiling only waits.
    Neither outcome rolls back the caller's raw work.
    """
    _require(not db.in_transaction, "publication requires separate transaction")
    fault = fault or (lambda stage: None)
    exhausted = [False]
    committed = False
    try:
        exhausted = _budget(db)
        db.execute("BEGIN IMMEDIATE")
        identity.read(db)
        _initialize(db)
        # Establish the guard even when the first source replay exceeds budget.
        # Nothing has been advertised or allocated in this initialization.
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        saved_state = _read_state(db)
        source = _source(db, saved_state[0]["page"]["lower"], paging=True)
        state, journal, machine, generation = _audit(db, source, saved_state)
        evidence = barriers(source)
        watermark = evidence[-1][0] if evidence else None
        _require(state["watermark"] is None or (watermark is not None
                 and watermark >= state["watermark"]), "watermark regressed")
        complete = periods(source, watermark, MAX_ROWS)
        _require(len(complete) >= len(journal), "published domains lost")
        for index, period in enumerate(complete):
            sequence = state["page"]["base"] + index + 1
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
        sequence = state["page"]["base"] + len(journal)
        source, journal, page = _checkpoint(db, state, source, journal, machine, generation)
        fault("after_checkpoint")
        if len(source) >= MAX_ROWS:
            # No safe retirement point: do not persist an ever-growing suffix.
            raise PublicationWaiting("unresolved source row budget")
        document = _json(_state(source, machine, generation, journal, sequence, page))
        _within(len(document.encode("utf-8")) <= MAX_BYTES
                 and len(_json(journal).encode("utf-8")) <= MAX_BYTES, "state byte budget")
        fault("before_watermark")
        db.execute("UPDATE screen_use_state SET document=?,digest=? WHERE singleton=1",
                   (document, _digest(document)))
        fault("after_watermark")
        fault("before_commit")
        db.commit()
        committed = True
        fault("after_commit")
        return _scope(json.loads(document))
    except (ValueError, TypeError, KeyError, IndexError, RuntimeError,
            OSError, sqlite3.Error, OverflowError, MemoryError) as error:
        # Only known deterministic ceilings defer. Storage errors, arbitrary
        # interrupts and uncertain commits still require intervention.
        if not committed and (isinstance(error, PublicationWaiting) or
                              (isinstance(error, sqlite3.OperationalError)
                               and exhausted[0])):
            db.set_progress_handler(None, 0)
            db.rollback()
            raise PublicationWaiting("publication waiting; resource budget") from None
        _block(db)
        raise PublicationBlocked("publication blocked; intervention required") from None
    finally:
        db.set_progress_handler(None, 0)


def _scope(state):
    return Scope(FORMAT, RULE, state["machine"], state["generation"], "ready",
                 state["watermark"], _sequence(state["sequence"]), 1,
                 state["page"]["beginning"])


class Journal:
    """Typed, pinned read interface for a future feed. No grouping in consumers.

    This implementation audits a sealed evidence page before advertising its
    occurrences, and audits the mutable suffix when opening the snapshot.
    Read/export never advances publication, acknowledges intake or changes the retention floor. Use advance/catch_up explicitly to catch up.
    """
    def __init__(self, path):
        self.db = _connect(path)
        exhausted = [False]
        try:
            exhausted = _budget(self.db)
            self.exhausted = exhausted
            self.db.execute("PRAGMA query_only=ON")
            self.db.execute("PRAGMA trusted_schema=OFF")
            self.db.execute("BEGIN")
            saved_state = _read_state(self.db)
            source = _source(self.db, saved_state[0]["page"]["lower"], paging=True)
            self.state, self.rows, self.machine, self.generation = _audit(self.db, source, saved_state)
        except BaseException as error:
            self.db.close()
            if isinstance(error, sqlite3.OperationalError) and exhausted[0]:
                raise PublicationWaiting("publication waiting; SQL step budget") from None
            raise

    def scope(self):
        return _scope(self.state)

    def entries(self, after_sequence=0, limit=MAX_ROWS):
        _require(type(after_sequence) is int and 0 <= after_sequence <= U64_MAX
                 and type(limit) is int and 0 < limit <= MAX_ROWS, "invalid journal range")
        result, size = [], 0
        for rows in self._segments(after_sequence):
            for row in rows:
                sequence = _sequence(row[0])
                if sequence > after_sequence:
                    size += len(row[2].encode("utf-8"))
                    _within(size <= MAX_BYTES, "finished output budget")
                    result.append(Entry(sequence, json.loads(row[2])))
                    if len(result) == limit:
                        return tuple(result)
        return tuple(result)

    def _segments(self, after_sequence=0):
        try:
            yield from self._segment_rows(after_sequence)
        except sqlite3.OperationalError:
            if self.exhausted[0]:
                raise PublicationWaiting("publication waiting; SQL step budget") from None
            raise

    def _segment_rows(self, after_sequence=0):
        if not self.state["page"]["count"]:
            yield self.rows
            return
        first = self.db.execute("""SELECT page FROM screen_use_checkpoints
            WHERE (through_length,through_sequence)>(?,?) ORDER BY through_length,through_sequence LIMIT 1""",
            (len(str(after_sequence)), str(after_sequence))).fetchone()
        if first:
            for number in range(first[0], self.state["page"]["count"] + 1):
                _, rows, digest = _load_checkpoint(self.db, number, self.machine, self.generation)
                if number == self.state["page"]["count"]:
                    _require(digest == self.state["page"]["head"], "checkpoint head changed")
                else:
                    following = self.db.execute("SELECT previous_digest FROM screen_use_checkpoints WHERE page=?",
                                                (number + 1,)).fetchone()
                    _require(following == (digest,), "checkpoint chain changed")
                yield rows
        yield self.rows

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
        size = 0
        for rows in journal._segments():
            for row in rows:
                evidence = json.loads(row[3])
                if evidence['end_ns'] > start_ns and evidence['start_ns'] < end_ns:
                    record = {"type": "occurrence", "sequence": row[0],
                              "occurrence": json.loads(row[2])}
                    size += len(_json(record).encode("utf-8")) + 1
                    _within(size <= MAX_BYTES and len(selected) < MAX_ROWS,
                            "finished output budget")
                    selected.append(record)
        records = [{"type": "scope", **asdict(scope)}, *selected,
                   {"type": "complete", "rows": len(selected)}]
        data = ''.join(_json(r) + '\n' for r in records).encode('utf-8')
        _within(len(data) <= MAX_BYTES, "finished output budget")
        if output.write(data) != len(data):
            raise OSError("incomplete finished write")
