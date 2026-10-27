"""Read-only coverage check for a completed capture; prints no app data."""
import argparse
from pathlib import Path
import sqlite3


def verify(database, start_ns, end_ns):
    if end_ns <= start_ns:
        raise ValueError("end must follow start")
    db = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        cursor = start_ns
        totals = {}
        for start, end, reason, title in db.execute("""
                SELECT start_ns,end_ns,reason,title FROM spans
                WHERE end_ns > ? AND start_ns < ? ORDER BY start_ns
                """, (start_ns, end_ns)):
            start, end = max(start, start_ns), min(end, end_ns)
            if start != cursor or end <= start:
                raise ValueError("gap or overlap in capture")
            if title is not None:
                raise ValueError("unexpected title in capture")
            if reason in ("unknown", "disconnected"):
                raise ValueError("capture contains unobserved time")
            totals[reason] = totals.get(reason, 0) + end - start
            cursor = end
        if cursor != end_ns:
            raise ValueError("capture does not cover requested interval")
        return totals
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--start-ns", type=int, required=True)
    parser.add_argument("--end-ns", type=int, required=True)
    args = parser.parse_args()
    try:
        totals = verify(args.database, args.start_ns, args.end_ns)
    except (ValueError, sqlite3.Error) as error:
        parser.exit(1, f"capture check failed: {error}\n")
    print(f"covered_ns={sum(totals.values())} gap_ns=0 overlap_ns=0")
    for reason, duration in sorted(totals.items()):
        print(f"{reason}_ns={duration}")


if __name__ == "__main__":
    main()
