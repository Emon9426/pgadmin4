##########################################################################
#
# pgAdmin 4 - PostgreSQL Tools
#
# Copyright (C) 2013 - 2026, The pgAdmin Development Team
# This software is released under the PostgreSQL Licence
#
##########################################################################

"""
Unit tests for the Oracle driver connection wrapper.

These tests run against fakes - no Oracle server is required.
"""

import unittest
from unittest.mock import MagicMock, patch

from flask import Flask

# pgadmin.model <-> config <-> pgadmin.utils have an import cycle that
# resolves only when config is loaded first (same order as pgAdmin4.py).
import config  # noqa: F401  pylint: disable=unused-import


class FakeOracleCursor:
    def __init__(self, rows=None, description=None):
        self.executed = []
        self._rows = rows or []
        self.description = description
        self.rowcount = -1 if description is not None else 3

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchone(self):
        if self._rows:
            return self._rows[0]
        return None

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class FakeOracleConnection:
    def __init__(self, cursor=None):
        self._cursor = cursor or FakeOracleCursor()
        self.autocommit = False
        self.version = '19.0.0.0.0'
        self.committed = 0
        self.rolled_back = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed += 1
        self.autocommit = False

    def rollback(self):
        self.rolled_back += 1
        self.autocommit = False

    def break_(self):
        pass

    def close(self):
        pass


def _make_connection(fake_oracle_conn):
    from pgadmin.utils.driver.oracle.connection import Connection

    manager = MagicMock()
    manager.sid = 1
    manager.db = 'orcl'
    manager.user = 'scott'
    manager.host = '127.0.0.1'
    manager.port = 1521
    manager.use_ssh_tunnel = 0
    manager.post_connection_sql = None

    conn = Connection(manager, 'CONN:12345', 'orcl', auto_reconnect=False)
    conn.wasConnected = True
    conn.conn = fake_oracle_conn
    return conn


class OracleConnectionTest(unittest.TestCase):
    """
    Tests for the Oracle driver Connection class.
    """

    def setUp(self):
        # Minimal flask app context for g/current_app.
        self.app = Flask(__name__)
        self.app.config['SERVER_MODE'] = False
        self.ctx = self.app.test_request_context()
        self.ctx.push()

        # The crypt key checks are not under test here.
        patcher = patch(
            'pgadmin.utils.driver.oracle.connection.get_crypt_key',
            return_value=(True, b'0' * 32))
        patcher.start()
        self.addCleanup(patcher.stop)

        # Translation strings need a babel-enabled app; run them raw.
        gettext_patcher = patch(
            'pgadmin.utils.driver.oracle.connection.gettext',
            side_effect=lambda s: s)
        gettext_patcher.start()
        self.addCleanup(gettext_patcher.stop)

    def tearDown(self):
        self.ctx.pop()

    def test_translate_params(self):
        from pgadmin.utils.driver.oracle.connection import Connection
        conn = _make_connection(FakeOracleConnection())

        query, params = conn._translate_params(
            "SELECT * FROM tab WHERE a = %s AND b = %s", ['x', 1])
        self.assertEqual(query,
                         "SELECT * FROM tab WHERE a = :1 AND b = :2")
        self.assertEqual(params, ['x', 1])

        query, params = conn._translate_params("SELECT 1", None)
        self.assertEqual(query, "SELECT 1")
        self.assertIsNone(params)

    def test_begin_maps_to_autocommit_off(self):
        conn = _make_connection(FakeOracleConnection())
        status, _ = conn.execute_void("BEGIN;")
        self.assertTrue(status)
        self.assertFalse(conn.conn.autocommit)
        # No statement was sent to the server
        self.assertEqual(conn.conn._cursor.executed, [])
        self.assertEqual(conn.transaction_status(), 2)

    def test_commit_and_rollback_mapping(self):
        conn = _make_connection(FakeOracleConnection())
        conn.execute_void("BEGIN;")
        status, _ = conn.execute_void("COMMIT;")
        self.assertTrue(status)
        self.assertEqual(conn.conn.committed, 1)
        self.assertTrue(conn.conn.autocommit)
        self.assertEqual(conn.transaction_status(), 0)

        conn.execute_void("BEGIN;")
        status, _ = conn.execute_void("ROLLBACK;")
        self.assertTrue(status)
        self.assertEqual(conn.conn.rolled_back, 1)

    def test_statement_error_marks_transaction_inerror(self):
        failing_cursor = FakeOracleCursor()
        failing_cursor.execute = MagicMock(
            side_effect=Exception("ORA-00942: table or view does not exist"))
        conn = _make_connection(FakeOracleConnection(failing_cursor))
        conn.execute_void("BEGIN;")
        status, errmsg = conn.execute_void("SELECT * FROM nope")
        self.assertFalse(status)
        self.assertIn("ORA-00942", errmsg)
        self.assertEqual(conn.transaction_status(), 3)

    def test_execute_async_and_poll(self):
        rows = [(1, 'one'), (2, 'two'), (3, 'three')]
        description = [
            ('ID', 'DB_TYPE_NUMBER', 22, 22, 0, 0, False),
            ('NAME', 'DB_TYPE_VARCHAR', 30, 30, None, None, True),
        ]
        cursor = FakeOracleCursor(rows=rows, description=description)
        conn = _make_connection(FakeOracleConnection(cursor))

        status, errmsg = conn.execute_async("SELECT * FROM dual")
        self.assertTrue(status)

        status, result = conn.poll(no_result=False)
        self.assertEqual(status, 1)
        self.assertEqual(result, rows)
        self.assertEqual(conn.rows_affected(), 3)
        self.assertEqual(conn.total_rows, 3)

        # Column info follows the psycopg3 shape.
        columns = conn.get_column_info()
        self.assertEqual(columns[0]['name'], 'ID')
        self.assertEqual(columns[0]['type_code'], 'DB_TYPE_NUMBER')
        self.assertIn('internal_size', columns[0])
        self.assertIn('display_size', columns[0])
        self.assertIn('precision', columns[0])
        self.assertIn('scale', columns[0])
        self.assertEqual([c['pos'] for c in columns], [0, 1])

    def test_async_fetchmany_slices(self):
        rows = [(i,) for i in range(10)]
        description = [('N', 'DB_TYPE_NUMBER', 22, 22, 0, 0, False)]
        cursor = FakeOracleCursor(rows=rows, description=description)
        conn = _make_connection(FakeOracleConnection(cursor))
        conn.execute_async("SELECT LEVEL FROM dual")

        status, result = conn.async_fetchmany_2darray(records=3)
        self.assertTrue(status)
        self.assertEqual(result, rows[:3])

        status, result = conn.async_fetchmany_2darray(records=-1)
        self.assertTrue(status)
        self.assertEqual(result, rows)

    def test_no_resultset_dml(self):
        cursor = FakeOracleCursor(rows=[], description=None)
        cursor.rowcount = 7
        conn = _make_connection(FakeOracleConnection(cursor))

        status, _ = conn.execute_async("UPDATE tab SET x = 1")
        self.assertTrue(status)

        status, result = conn.poll(no_result=True)
        self.assertEqual(status, 1)
        self.assertEqual(conn.rows_affected(), 7)
        self.assertIn('7 row(s) affected', conn.status_message())

        status, result = conn.async_fetchmany_2darray()
        self.assertTrue(status)
        self.assertIsNone(result)

    def test_ping(self):
        conn = _make_connection(FakeOracleConnection())
        self.assertTrue(conn.ping())

    def test_error_message_formatting(self):
        conn = _make_connection(FakeOracleConnection())
        err = Exception("ORA-01017: invalid username/password")
        msg = conn._formatted_exception_msg(err, True)
        self.assertTrue(msg.startswith('ERROR:'))
        self.assertIn('ORA-01017', msg)

    def test_disconnected_error_detection(self):
        conn = _make_connection(FakeOracleConnection())
        self.assertTrue(conn._is_disconnected_error("DPY-4011: lost"))
        self.assertFalse(conn._is_disconnected_error("ORA-00942"))


if __name__ == '__main__':
    unittest.main()
