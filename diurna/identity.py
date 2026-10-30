"""Collector-owned identity for one local archive, never inferred from the host."""
import uuid

FORMAT = "diurna-identity-v1"
TABLE = "diurna_identity"


def _uuid4(value):
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return (str(parsed) == value and parsed.version == 4
            and parsed.variant == uuid.RFC_4122)


def read(db):
    """Require exactly one supported identity; even a damaged table cannot remint it."""
    def bounded(name, size):
        return (f"CASE WHEN typeof({name}) = 'text' "
                f"AND length(CAST({name} AS BLOB)) <= {size} THEN {name} ELSE NULL END")

    rows = db.execute(f"""SELECT
        CASE WHEN typeof(singleton) = 'integer' THEN singleton ELSE NULL END,
        {bounded('format', len(FORMAT))}, {bounded('machine', 36)},
        {bounded('generation', 36)} FROM diurna_identity LIMIT 2""").fetchall()
    if len(rows) != 1:
        raise ValueError("invalid diurna identity")
    singleton, format_, machine, generation = rows[0]
    if (singleton != 1 or format_ != FORMAT
            or not _uuid4(machine) or not _uuid4(generation)):
        raise ValueError("invalid diurna identity")
    return machine, generation


def create(db):
    """Only called inside the initial schema transaction, before version 3 exists."""
    db.execute("""CREATE TABLE diurna_identity (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        format TEXT NOT NULL, machine TEXT NOT NULL, generation TEXT NOT NULL)""")
    db.execute("INSERT INTO diurna_identity VALUES (1, ?, ?, ?)",
               (FORMAT, str(uuid.uuid4()), str(uuid.uuid4())))
    return read(db)
