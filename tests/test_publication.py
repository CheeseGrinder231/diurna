"""Only private synthetic archives, deterministic schedules and injected faults."""
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from diurna import publication as p
from diurna.collector import Span
from diurna.export import export as raw_export
from diurna.screen_classifier import SCREENSAVER
from diurna.screen_periods import Period
from diurna.store import Store

S = 1_000_000_000
MACHINE = "11111111-1111-4111-8111-111111111111"
GENERATION = "22222222-2222-4222-8222-222222222222"


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "synthetic.db"
        store = Store(self.path)
        with store.db:
            store.db.execute("UPDATE diurna_identity SET machine=?,generation=?", (MACHINE, GENERATION))
        store.close()

    def append(self, start, end, app="a", reason="active", input_=1, publish=True):
        store = Store(self.path)
        try:
            if publish:
                store.write(Span(start, end, app, "secret workspace", reason, input_, 0, 0))
            else:
                with patch('diurna.publication.advance'):
                    store.write(Span(start, end, app, "secret workspace", reason, input_, 0, 0))
        finally:
            store.close()

    def connection(self):
        db = sqlite3.connect(self.path)
        self.addCleanup(db.close)
        return db

    def journal(self):
        journal = p.Journal(self.path)
        journal.close()
        return journal

    def finished(self):
        self.append(1, 200*S)
        self.append(200*S, 400*S, "b")
        self.append(400*S, 401*S, reason="unknown", input_=0)

    def test_golden_application_mixed_payloads_ids_and_exact_nanoseconds(self):
        app = Period(9007199254740993, 9007199254740995, "終端", (("終端", 2),),
                     9007199254740993, 9007199254740995, "inactive")
        self.assertEqual(p.occurrence(app, 1, MACHINE, GENERATION), {
            "machine": MACHINE, "generation": GENERATION, "id": "screen-use:1",
            "start": "1970-04-15T05:59:59.254740993Z",
            "end": "1970-04-15T05:59:59.254740995Z",
            "activity": {"kind": "screen-use", "useMode": "application", "application": "終端",
                         "activeDurationNs": "2"}})
        mixed = Period(1, 3, None, (("a", 1), ("b", 1)), 1, 3, "inactive")
        self.assertEqual(p.occurrence(mixed, 2, MACHINE, GENERATION), {
            "machine": MACHINE, "generation": GENERATION, "id": "screen-use:2",
            "start": "1970-01-01T00:00:00.000000001Z",
            "end": "1970-01-01T00:00:00.000000003Z",
            "activity": {"kind": "screen-use", "useMode": "mixed", "activeDurationNs": "2"}})

    def test_restart_each_boundary_holds_unresolved_midnight_suffix_without_clock(self):
        midnight = 16*3600*S
        history = [(midnight-60*S, midnight, "a", "active"),
                   (midnight, midnight+30*S, "a", "idle"),
                   (midnight+30*S, midnight+40*S, SCREENSAVER, "active"),
                   (midnight+40*S, midnight+40*S+1, SCREENSAVER, "active")]
        for start, end, app, reason in history:
            self.append(start, end, app, reason, 0)
            for fake_now in (end, end+10**20):
                with patch('time.time_ns', return_value=fake_now):
                    scope = p.catch_up(self.path)
                    self.assertIsNone(scope.watermark_ns)
                    self.assertEqual(scope.through_sequence, 0)
                    with p.Journal(self.path) as journal:
                        self.assertEqual(journal.entries(), ())
                        self.assertEqual(len(journal.state['source']), history.index((start,end,app,reason))+1)
                        self.assertTrue(journal.state['pending'])
        end = history[-1][1]
        self.append(end, end+S, input_=1)
        self.assertEqual(self.journal().entries(), ())  # Prior day reclassified away.

    def test_scope_retention_export_clipping_and_replay_do_not_acknowledge(self):
        self.finished()
        before = self.path.read_bytes()
        with p.Journal(self.path) as journal:
            scope = journal.scope()
            entries = journal.entries()
            self.assertEqual([e.sequence for e in entries], [1, 2])
            self.assertEqual(journal.entries(after_sequence=1, limit=1), entries[1:])
            self.assertEqual(scope.retention_floor_sequence, 1)
            self.assertEqual(scope.original_beginning_ns, 1)
            self.assertTrue(scope.retained_all)
            self.assertEqual(scope.watermark_ns, 400*S)
        output = io.BytesIO()
        p.export(self.path, output, 2, 3)
        clipped = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(clipped[1]['occurrence'], entries[0].occurrence)
        self.assertEqual(clipped[-1], {"type": "complete", "rows": 1})
        self.assertNotIn(b'workspace', output.getvalue())
        self.assertNotIn(b'input', output.getvalue())
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(p.catch_up(self.path), scope)
        with p.Journal(self.path) as journal:
            self.assertEqual(journal.entries(), entries)
        # New facts use later publication sequence; timestamps are not cursors.
        self.append(401*S, 601*S, "c")
        self.append(601*S, 602*S, reason="unknown", input_=0)
        with p.Journal(self.path) as journal:
            self.assertEqual(journal.entries()[:2], entries)
            self.assertEqual(journal.entries()[-1].sequence, 3)

    def test_old_archive_catchup_and_proved_gap(self):
        self.append(1, 2, publish=False)
        db = self.connection()
        with db:
            db.execute("""INSERT INTO spans(start_ns,end_ns,app_class,workspace,active,reason,input)
                VALUES(10,11,'b','w',1,'active',0)""")
        scope = p.catch_up(self.path)
        self.assertEqual(scope.watermark_ns, 10)
        self.assertEqual(self.journal().entries()[0].occurrence['activity']['activeDurationNs'], '1')
        self.assertEqual(self.journal().state['barriers'][-1][1], 'gap')

    def test_inactive_boundary_checkpoint_and_termination_supply_no_seal(self):
        self.append(1, 60*S, input_=0)
        self.append(60*S, 90*S, reason="idle", input_=0)
        self.append(90*S, 100*S, SCREENSAVER, input_=0)
        scope = p.catch_up(self.path)  # Process exit and catch-up still no seal.
        self.assertIsNone(scope.watermark_ns)
        self.assertEqual(self.journal().entries(), ())
        self.append(100*S, 100*S+1, SCREENSAVER, input_=0)
        self.append(100*S+1, 101*S, input_=1)
        self.assertEqual(self.journal().entries(), ())

    def test_faults_expose_old_or_complete_new_publication_and_latch_stream(self):
        for stage in ('before_sequence', 'after_sequence', 'after_journal',
                      'before_watermark', 'after_watermark', 'before_commit', 'after_commit'):
            with self.subTest(stage=stage):
                # A separate synthetic archive for every failure boundary.
                path = Path(self.temporary.name) / (stage+'.db')
                store = Store(path)
                try:
                    store.write(Span(1, 200*S, 'a', 'w', 'active', True))
                    with patch('diurna.publication.advance'):
                        store.write(Span(200*S, 201*S, '', '', 'unknown'))
                    def fail(point):
                        if point == stage:
                            raise sqlite3.OperationalError('synthetic storage failure')
                    with self.assertRaises(p.PublicationBlocked):
                        p.advance(store.db, fail)
                    count = store.db.execute('SELECT COUNT(*) FROM screen_use_journal').fetchone()[0]
                    self.assertEqual(count, int(stage == 'after_commit'))
                    state = json.loads(store.db.execute('SELECT document FROM screen_use_state').fetchone()[0])
                    self.assertEqual(state['sequence'], str(count))
                    self.assertEqual(len(state['source']), 2 if count else 1)
                    with self.assertRaises(p.PublicationBlocked):
                        p.advance(store.db)
                    # The source commit is durable regardless of publication.
                    self.assertEqual(store.db.execute('SELECT COUNT(*) FROM spans').fetchone()[0], 2)
                finally:
                    store.close()

    def test_competing_pumps_serialize_without_double_publication(self):
        self.append(1, 200*S, publish=False)
        self.append(200*S, 201*S, reason="unknown", publish=False)
        rendezvous = threading.Barrier(3)
        results = []
        def pump():
            rendezvous.wait()
            try:
                results.append(p.catch_up(self.path))
            except BaseException as error:
                results.append(error)
        workers = [threading.Thread(target=pump) for _ in range(2)]
        for worker in workers:
            worker.start()
        rendezvous.wait()
        for worker in workers:
            worker.join()
        self.assertEqual(len(results), 2)
        self.assertTrue(all(isinstance(result, p.Scope) for result in results), results)
        self.assertEqual(self.journal().scope().through_sequence, 1)

    def test_budget_failure_latches_while_raw_collection_export_and_views_continue(self):
        self.append(1, 2)
        with patch.object(p, 'MAX_ROWS', 1), redirect_stderr(io.StringIO()) as diagnostic:
            self.append(2, 3)
        self.assertIn('raw collection continues', diagnostic.getvalue())
        with self.assertRaises(p.PublicationBlocked):
            p.catch_up(self.path)
        with redirect_stderr(io.StringIO()):
            self.append(3, 4)
        out = io.BytesIO()
        raw_export(self.path, out, 1, 5)
        self.assertIn(b'"rows":3', out.getvalue())
        with patch('diurna.today.policy') as policy:
            policy.return_value.screensaver_grace_ns = 10*S
            # Pure view behavior is unchanged and does not read publication state.
            from diurna.today import summarize, rows
            summary = summarize(rows(self.path, 1, 4), 1, 4)
            self.assertEqual(summary['active_ns'], 3)

    def test_corruption_edits_deletion_rule_and_identity_loss_block(self):
        mutations = [
            "UPDATE spans SET app_class='edited' WHERE start_ns=1",
            "DELETE FROM spans WHERE start_ns=1",
            "UPDATE screen_use_journal SET payload='{}' WHERE sequence='1'",
            "DELETE FROM screen_use_journal WHERE sequence='1'",
            "DROP TABLE screen_use_journal",
            "DELETE FROM screen_use_state",
            "UPDATE screen_use_state SET digest='wrong'",
            "UPDATE diurna_identity SET screen_use_guard='future-rule'",
            "UPDATE diurna_identity SET machine='33333333-3333-4333-8333-333333333333'",
            "DELETE FROM diurna_identity",
        ]
        self.finished()
        for index, sql in enumerate(mutations):
            with self.subTest(sql=sql):
                path = Path(self.temporary.name) / f'corrupt-{index}.db'
                path.write_bytes(self.path.read_bytes())
                os.chmod(path, 0o600)
                db = sqlite3.connect(path)
                try:
                    with db:
                        db.execute(sql)
                    with self.assertRaises((ValueError, sqlite3.Error)):
                        p.Journal(path)
                    with self.assertRaises(p.PublicationBlocked):
                        p.advance(db)
                finally:
                    db.close()

    def test_invalid_application_boundaries_and_unknown_duplicate_observations_fail(self):
        for app in ('', 'x\n', 'x'*4097, '/private/path', 'some app'):
            with self.subTest(app=repr(app[:10])):
                period = Period(1, 2, app, ((app, 1),), 1, 2, 'inactive')
                with self.assertRaises(p.PublicationBlocked):
                    p.occurrence(period, 1, MACHINE, GENERATION)
        for start, end in ((0, 1), (1, 1), (2, 1)):
            with self.assertRaises(p.PublicationBlocked):
                p.occurrence(Period(start, end, 'a', (('a', end-start),), start, end, 'inactive'),
                             1, MACHINE, GENERATION)
        self.append(1, 2)
        db = self.connection()
        with db:
            db.execute('ALTER TABLE spans RENAME TO old_spans')
            db.execute('CREATE TABLE spans AS SELECT * FROM old_spans')
            db.execute('INSERT INTO spans SELECT * FROM old_spans')
        with self.assertRaises(p.PublicationBlocked):
            p.advance(db)

    def test_sequence_exhaustion_and_invalid_signals_block_without_skips(self):
        self.append(1, 200*S)
        self.append(200*S, 201*S, reason='unknown', publish=False)
        with patch.object(p, 'U64_MAX', 0):
            with self.assertRaises(p.PublicationBlocked):
                p.catch_up(self.path)
        self.assertEqual(self.connection().execute('SELECT COUNT(*) FROM screen_use_journal').fetchone()[0], 0)
        with self.assertRaises(p.PublicationBlocked):
            p.catch_up(self.path)

    def test_unsupported_rule_and_pending_state_corruption_are_not_restarted(self):
        self.append(1, 2, input_=0)
        db = self.connection()
        document = json.loads(db.execute('SELECT document FROM screen_use_state').fetchone()[0])
        document['rule'] = 'future-rule'
        encoded = p._json(document)
        with db:
            db.execute('UPDATE screen_use_state SET document=?,digest=?', (encoded, p._digest(encoded)))
        with self.assertRaises(p.PublicationBlocked):
            p.advance(db)
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 3)

    def test_uncommitted_future_cannot_supply_a_barrier(self):
        self.append(1, 60*S, input_=0)
        self.append(60*S, 90*S, reason='idle', input_=0)
        db = self.connection()
        db.execute('BEGIN IMMEDIATE')
        db.execute("""INSERT INTO spans(start_ns,end_ns,app_class,workspace,active,reason,input)
            VALUES(?,?, 'b','w',1,'active',1)""", (90*S, 91*S))
        with self.assertRaises(p.PublicationBlocked):
            p.advance(db)  # Requires a separate, committed-source transaction.
        with p.Journal(self.path) as journal:
            self.assertIsNone(journal.scope().watermark_ns)
            self.assertEqual(journal.entries(), ())
        db.rollback()
        self.assertEqual(p.catch_up(self.path).through_sequence, 0)

    def test_initial_budget_failure_is_durably_blocked_and_raw_remains_available(self):
        self.append(1, 2, publish=False)
        self.append(2, 3, publish=False)
        with patch.object(p, 'MAX_ROWS', 1):
            with self.assertRaises(p.PublicationBlocked):
                p.catch_up(self.path)
        # Restoring the normal budget never silently clears a failed stream.
        with self.assertRaises(p.PublicationBlocked):
            p.catch_up(self.path)
        output = io.BytesIO()
        raw_export(self.path, output, 1, 4)
        self.assertIn(b'"rows":2', output.getvalue())

    def test_corrupt_pending_state_and_regrouped_facts_require_intervention(self):
        self.finished()
        db = self.connection()
        document = json.loads(db.execute('SELECT document FROM screen_use_state').fetchone()[0])
        document['pending'] = []
        encoded = p._json(document)
        with db:
            db.execute('UPDATE screen_use_state SET document=?,digest=?', (encoded, p._digest(encoded)))
        with self.assertRaises(p.PublicationBlocked):
            p.advance(db)

    def test_regrouped_published_facts_are_detected(self):
        self.finished()
        with patch('diurna.screen_periods.FOLD_NS', 401*S):
            with self.assertRaises(p.PublicationBlocked):
                p.catch_up(self.path)
        with self.assertRaises(p.PublicationBlocked):
            p.catch_up(self.path)

    def test_invalid_stored_signal_and_unknown_reason_do_not_get_skipped(self):
        self.append(1, 2)
        for column, value in (('input', 2), ('reason', 'future-reason')):
            path = Path(self.temporary.name) / (column+'.db')
            path.write_bytes(self.path.read_bytes())
            os.chmod(path, 0o600)
            db = sqlite3.connect(path)
            try:
                with db:
                    db.execute('PRAGMA ignore_check_constraints=ON')
                    db.execute(f'UPDATE spans SET {column}=?', (value,))
                with self.assertRaises(p.PublicationBlocked):
                    p.advance(db)
            finally:
                db.close()

    def test_sql_budget_and_oversized_domain_fail_without_force_finishing(self):
        self.append(1, 2, input_=0)
        with patch.object(p, 'MAX_SQL_STEPS', 1):
            with self.assertRaises(p.PublicationBlocked):
                p.catch_up(self.path)
        self.assertEqual(self.connection().execute('SELECT COUNT(*) FROM screen_use_journal').fetchone()[0], 0)
        from diurna.screen_periods import DAY_NS, periods
        spans = [(1, 3*DAY_NS, 'a', 'w', 'active', None, 1, 0, 0)]
        with self.assertRaisesRegex(ValueError, 'budget'):
            periods(spans, None, max_domains=1)
