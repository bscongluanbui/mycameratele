"""Offline SQLite migration tests; no media or Telegram requests."""
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from archive_app.channel_index_schema import ensure_index_message_schema


OLD_CREATE = '''CREATE TABLE IF NOT EXISTS channel_index_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id TEXT NOT NULL, channel_chat_id INTEGER NOT NULL,
    index_type TEXT NOT NULL CHECK(index_type IN ('root','year','month','day')),
    period_key TEXT NOT NULL, tg_message_id INTEGER, tg_message_url TEXT,
    state TEXT NOT NULL DEFAULT 'pending', revision INTEGER NOT NULL DEFAULT 0,
    last_render_hash TEXT, pinned INTEGER NOT NULL DEFAULT 0,
    attempt_id TEXT, last_error TEXT, updated_at REAL NOT NULL,
    UNIQUE(camera_id,channel_chat_id,index_type,period_key))'''
NEW_CREATE = OLD_CREATE.replace("'root','year','month','day'", "'root','years','year','month','day','hour'")


class ChannelIndexSchemaTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parent if os.name == 'nt' else Path(tempfile.gettempdir())
        self.root = parent / ('.tmp-channel-index-schema-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.path = self.root / 'archive.db'
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.execute(OLD_CREATE)
        self.conn.execute('CREATE TABLE unrelated (value TEXT)')
        # Real existing leases/job rows are intentionally outside the rebuilt table.
        self.conn.execute('CREATE TABLE channel_index_leases (camera_id TEXT, owner TEXT, lease_until REAL)')
        self.conn.execute("INSERT INTO channel_index_leases VALUES ('cam', 'worker-a', 9999999999)")
        self.conn.execute('CREATE TABLE channel_index_jobs (id INTEGER PRIMARY KEY, status TEXT, lease_owner TEXT)')
        self.conn.execute("INSERT INTO channel_index_jobs VALUES (7, 'running', 'worker-a')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.root)

    def insert(self, kind='day', period='2026-10-11', **overrides):
        values = dict(camera_id='cam', channel_chat_id=-1001234567890, index_type=kind,
                      period_key=period, tg_message_id=123, tg_message_url='https://t.me/c/1234567890/123',
                      state='ready', revision=9, last_render_hash='exact-original-hash', pinned=1,
                      attempt_id='original-attempt', last_error=None, updated_at=1234567890.125)
        values.update(overrides)
        return self.conn.execute('INSERT INTO channel_index_messages (' + ','.join(values) + ') VALUES ('
                                 + ','.join('?' for _ in values) + ')', tuple(values.values())).lastrowid

    def ddl(self):
        return self.conn.execute("SELECT sql FROM sqlite_schema WHERE name='channel_index_messages'").fetchone()[0]

    def snapshot(self):
        return self.conn.execute('SELECT * FROM channel_index_messages ORDER BY id').fetchall()

    def test_preserves_references_states_ids_hashes_pins_jobs_and_leases(self):
        self.insert(id=2, kind='root', period='root', state='pending', tg_message_id=None, pinned=0)
        self.insert(id=7, state='ready')
        self.insert(id=11, kind='month', period='2026-10', state='needs_reconcile',
                    attempt_id='uncertain-send', last_error='unknown send result')
        self.insert(id=99, kind='year', period='2026')
        self.conn.execute('DELETE FROM channel_index_messages WHERE id=99')
        self.conn.commit()
        rows = self.snapshot()
        jobs = self.conn.execute('SELECT * FROM channel_index_jobs').fetchall()
        leases = self.conn.execute('SELECT * FROM channel_index_leases').fetchall()
        self.assertTrue(ensure_index_message_schema(self.conn, NEW_CREATE))
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.snapshot(), rows)
        self.assertEqual(self.conn.execute('SELECT * FROM channel_index_jobs').fetchall(), jobs)
        self.assertEqual(self.conn.execute('SELECT * FROM channel_index_leases').fetchall(), leases)
        self.assertEqual(self.conn.execute("SELECT seq FROM sqlite_sequence WHERE name='channel_index_messages'").fetchone(), (99,))
        self.assertEqual(self.insert(kind='years', period='years'), 100)
        self.insert(kind='hour', period='2026-10-11T08')
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert(kind='unexpected', period='x')
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert(kind='hour', period='2026-10-11T08')

    def test_open_transaction_can_rollback_schema_and_other_pending_writes(self):
        self.insert(id=17)
        self.conn.commit()
        before, ddl = self.snapshot(), self.ddl()
        self.conn.execute("INSERT INTO unrelated VALUES ('pending-caller-write')")
        self.assertTrue(self.conn.in_transaction)
        self.assertTrue(ensure_index_message_schema(self.conn, NEW_CREATE))
        self.assertTrue(self.conn.in_transaction)
        self.insert(kind='hour', period='2026-10-11T08')
        self.conn.rollback()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.ddl(), ddl)
        self.assertEqual(self.conn.execute('SELECT * FROM unrelated').fetchall(), [])
        self.assertEqual(self.conn.execute("SELECT seq FROM sqlite_sequence WHERE name='channel_index_messages'").fetchone(), (17,))

    def test_open_transaction_can_commit_migration_with_caller_writes(self):
        self.conn.execute("INSERT INTO unrelated VALUES ('caller-write')")
        self.assertTrue(ensure_index_message_schema(self.conn, NEW_CREATE))
        self.assertTrue(self.conn.in_transaction)
        self.conn.commit()
        self.assertEqual(self.conn.execute('SELECT * FROM unrelated').fetchall(), [('caller-write',)])
        self.insert(kind='hour', period='2026-10-11T08')

    def test_explicit_partial_index_and_trigger_preserved(self):
        self.conn.execute('CREATE UNIQUE INDEX unique_ready_message ON channel_index_messages(tg_message_id) WHERE state=\'ready\'')
        self.conn.execute('CREATE TABLE change_audit (message_id INTEGER)')
        self.conn.execute('''CREATE TRIGGER audit_pin AFTER UPDATE OF pinned ON channel_index_messages
            BEGIN INSERT INTO change_audit VALUES (new.tg_message_id); END''')
        self.insert()
        self.conn.commit()
        before = self.conn.execute("SELECT type,name,sql FROM sqlite_schema WHERE tbl_name='channel_index_messages' AND sql IS NOT NULL AND type<>'table' ORDER BY name").fetchall()
        self.assertTrue(ensure_index_message_schema(self.conn, NEW_CREATE))
        after = self.conn.execute("SELECT type,name,sql FROM sqlite_schema WHERE tbl_name='channel_index_messages' AND sql IS NOT NULL AND type<>'table' ORDER BY name").fetchall()
        self.assertEqual(before, after)
        self.conn.execute('UPDATE channel_index_messages SET pinned=0')
        self.assertEqual(self.conn.execute('SELECT * FROM change_audit').fetchall(), [(123,)])
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert(kind='hour', period='2026-10-11T08')

    def test_already_new_is_noop_in_pending_transaction(self):
        ensure_index_message_schema(self.conn, NEW_CREATE)
        self.insert(kind='years', period='years')
        self.conn.commit()
        ddl, rows = self.ddl(), self.snapshot()
        version = self.conn.execute('PRAGMA schema_version').fetchone()
        self.conn.execute("INSERT INTO unrelated VALUES ('pending')")
        changes = self.conn.total_changes
        self.assertFalse(ensure_index_message_schema(self.conn, NEW_CREATE))
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.conn.total_changes, changes)
        self.assertEqual(self.conn.execute('PRAGMA schema_version').fetchone(), version)
        self.assertEqual(self.ddl(), ddl)
        self.assertEqual(self.snapshot(), rows)
        self.conn.rollback()
        self.assertEqual(self.conn.execute('SELECT * FROM unrelated').fetchall(), [])

    def test_concurrent_connections_only_one_migrates(self):
        self.insert(id=50)
        self.conn.commit()
        rows = self.snapshot()
        barrier = threading.Barrier(4)
        results, errors = [], []

        def constructor():
            conn = sqlite3.connect(self.path, timeout=30)
            try:
                barrier.wait(timeout=30)
                results.append(ensure_index_message_schema(conn, NEW_CREATE))
            except BaseException as exc:
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=constructor) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=35)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [False, False, False, True])
        self.assertEqual(self.snapshot(), rows)

    def test_unexpected_extra_column_rejected_without_committing(self):
        self.conn.execute('ALTER TABLE channel_index_messages ADD COLUMN future_metadata TEXT')
        self.conn.commit()
        ddl = self.ddl()
        self.conn.execute("INSERT INTO unrelated VALUES ('pending')")
        with self.assertRaisesRegex(ValueError, 'columns or constraints'):
            ensure_index_message_schema(self.conn, NEW_CREATE)
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.ddl(), ddl)
        self.assertEqual(self.conn.execute('SELECT * FROM unrelated').fetchall(), [('pending',)])
        self.conn.rollback()

    def test_extra_constraint_rejected_without_changes(self):
        self.conn.execute('DROP TABLE channel_index_messages')
        self.conn.execute(OLD_CREATE.replace('updated_at REAL NOT NULL', 'updated_at REAL NOT NULL CHECK(updated_at>0)'))
        ddl = self.ddl()
        with self.assertRaisesRegex(ValueError, 'columns or constraints'):
            ensure_index_message_schema(self.conn, NEW_CREATE)
        self.assertEqual(self.ddl(), ddl)
        self.assertFalse(self.conn.in_transaction)

    def test_unexpected_check_kind_rejected(self):
        self.conn.execute('DROP TABLE channel_index_messages')
        self.conn.execute(OLD_CREATE.replace("'day'", "'day','mystery'"))
        ddl = self.ddl()
        with self.assertRaisesRegex(ValueError, 'CHECK kinds'):
            ensure_index_message_schema(self.conn, NEW_CREATE)
        self.assertEqual(self.ddl(), ddl)

    def test_external_foreign_key_rejected_even_when_fk_disabled(self):
        self.conn.execute('CREATE TABLE linked (node INTEGER REFERENCES channel_index_messages(id))')
        self.conn.commit()
        ddl = self.ddl()
        with self.assertRaisesRegex(ValueError, 'foreign-key'):
            ensure_index_message_schema(self.conn, NEW_CREATE)
        self.assertEqual(self.ddl(), ddl)

    def test_failure_after_copy_is_atomic_and_keeps_caller_transaction(self):
        self.insert(id=31)
        self.conn.commit()
        rows, ddl = self.snapshot(), self.ddl()
        self.conn.execute("INSERT INTO unrelated VALUES ('pending')")
        real_conn = self.conn

        class FailRename:
            def execute(self, statement, *args):
                if statement.startswith('ALTER TABLE'):
                    raise sqlite3.OperationalError('injected rename failure')
                return real_conn.execute(statement, *args)

        with self.assertRaisesRegex(sqlite3.OperationalError, 'injected rename'):
            ensure_index_message_schema(FailRename(), NEW_CREATE)
        self.assertEqual(self.snapshot(), rows)
        self.assertEqual(self.ddl(), ddl)
        self.assertEqual(self.conn.execute('SELECT * FROM unrelated').fetchall(), [('pending',)])
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT name FROM sqlite_schema WHERE name LIKE 'channel_index_messages_migration_%'").fetchall(), [])

    def test_empty_table_with_deleted_sequence_retains_high_water(self):
        self.insert(id=1234)
        self.conn.execute('DELETE FROM channel_index_messages')
        self.conn.commit()
        self.assertTrue(ensure_index_message_schema(self.conn, NEW_CREATE))
        self.assertEqual(self.insert(kind='hour', period='2026-10-11T08'), 1235)

    def test_missing_table_fails_without_committing_caller_writes(self):
        self.conn.execute('DROP TABLE channel_index_messages')
        self.conn.execute("INSERT INTO unrelated VALUES ('pending')")
        with self.assertRaisesRegex(sqlite3.OperationalError, 'no such table'):
            ensure_index_message_schema(self.conn, NEW_CREATE)
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.conn.execute('SELECT * FROM unrelated').fetchall(), [('pending',)])


if __name__ == '__main__':
    unittest.main()
