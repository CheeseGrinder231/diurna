from pathlib import Path
import tempfile
import unittest

from scripts.verify_capture import verify
from diurna.collector import Span
from diurna.store import Store


class CaptureTests(unittest.TestCase):
    def test_exact_hour_clips_edges_and_sums_states(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "db"
            store = Store(path)
            try:
                store.write(Span(0, 1800_000_000_000, "foot", "1", "active"))
                store.write(Span(1800_000_000_000, 4000_000_000_000, "foot", "1", "suspended"))
                result = verify(path, 100_000_000_000, 3700_000_000_000)
                self.assertEqual(result, {"active": 1700_000_000_000, "suspended": 1900_000_000_000})
                self.assertEqual(sum(result.values()), 3600_000_000_000)
                with self.assertRaises(ValueError):
                    verify(path, 0, 5000_000_000_000)
                # Corrupt a synthetic copy to prove coverage checking detects it.
                with store.db:
                    store.db.execute("UPDATE spans SET start_ns=start_ns+1 WHERE id=2")
                with self.assertRaises(ValueError):
                    verify(path, 0, 3600_000_000_000)
                with store.db:
                    store.db.execute("UPDATE spans SET start_ns=start_ns-2 WHERE id=2")
                with self.assertRaises(ValueError):
                    verify(path, 0, 3600_000_000_000)
            finally:
                store.close()

    def test_unknown_time_is_not_an_acceptance_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "db"
            store = Store(path)
            try:
                store.write(Span(0, 5, "", "", "unknown"))
                with self.assertRaises(ValueError):
                    verify(path, 0, 5)
            finally:
                store.close()
