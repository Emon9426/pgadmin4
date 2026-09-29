##########################################################################
#
# pgAdmin 4 - PostgreSQL Tools
#
# Copyright (C) 2013 - 2026, The pgAdmin Development Team
# This software is released under the PostgreSQL Licence
#
##########################################################################

"""
Implementation of Connection for Oracle databases.
It is a wrapper around the python-oracledb driver (thin mode by default).
"""

import re
import datetime
from flask import current_app, g
from flask_babel import gettext
from flask_security import current_user

import config
from pgadmin.model import User
from pgadmin.utils.crypto import decrypt
from pgadmin.utils.exception import ConnectionLost, CryptKeyMissing
from pgadmin.utils.master_password import get_crypt_key
from ..abstract import BaseConnection

try:
    import oracledb
except ImportError:
    oracledb = None

_ = gettext

# Statements pgAdmin issues internally to manage transactions on the
# query tool connection. Oracle starts transactions implicitly, so BEGIN
# only flips the autocommit flag, while COMMIT/ROLLBACK are mapped onto
# the driver connection.
_BEGIN_RE = re.compile(r'^\s*BEGIN\s*;?\s*$', re.IGNORECASE)
_COMMIT_RE = re.compile(r'^\s*COMMIT\s*;?\s*$', re.IGNORECASE)
_ROLLBACK_RE = re.compile(r'^\s*ROLLBACK\s*;?\s*$', re.IGNORECASE)

# psycopg-style positional placeholders pgAdmin code may pass in; Oracle
# expects :1, :2, ...
_PLACEHOLDER_RE = re.compile(r'%s')

# ORA error codes that indicate the connection to the server was lost.
_DISCONNECT_ERRORS = (
    'DPY-4011', 'DPY-4012', 'DPY-6007', 'DPY-4023',
    'ORA-03113', 'ORA-03114', 'ORA-03135', 'ORA-12170', 'ORA-12547',
)


def _oracledb_available():
    if oracledb is None:
        raise ImportError(
            gettext("The python-oracledb driver is not installed. Install "
                    "it with 'pip install oracledb' to connect to Oracle "
                    "servers.")
        )


class OracleCursorWrapper(object):
    """
    A small wrapper over oracledb cursor to expose the attributes pgAdmin
    expects (last executed query as bytes like psycopg, rowcount,
    description, ...).
    """
    def __init__(self, cursor):
        self._cursor = cursor
        self._query = ''

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    @property
    def query(self):
        # psycopg's cursor.query returns the last statement as bytes;
        # pgAdmin code calls .decode() on it.
        if isinstance(self._query, bytes):
            return self._query
        return self._query.encode('utf-8') if self._query else b''


class Connection(BaseConnection):
    """
    class Connection(object)

        A wrapper class, which wraps the oracledb connection object, and
        delegates execution to it when required.
    """
    UNAUTHORIZED_REQUEST = gettext("Unauthorized request.")
    CURSOR_NOT_FOUND = \
        gettext("Cursor could not be found for the async connection.")
    ARGS_STR = "{0}#{1}"

    def __init__(self, manager, conn_id, db, **kwargs):
        assert (manager is not None)
        assert (conn_id is not None)

        auto_reconnect = kwargs.get('auto_reconnect', True)
        async_ = kwargs.get('async_', 0)

        self.conn_id = conn_id
        self.manager = manager
        self.db = db if db is not None else manager.db
        self.conn = None
        self.auto_reconnect = auto_reconnect
        self.async_ = async_
        self.__async_cursor = None
        self.__async_query_error = None
        self.execution_aborted = False
        self.row_count = 0
        self.column_info = None
        self.password = None
        self.wasConnected = False
        self.reconnecting = False
        self._autocommit = True
        # Local view of the transaction state for the query tool buttons:
        # 0 idle, 2 in transaction, 3 in failed transaction (matches the
        # libpq TRANSACTION_STATUS values pgAdmin compares against).
        self._transaction_status = 0

        super(Connection, self).__init__()

    def as_dict(self):
        if not self.auto_reconnect and not self.conn:
            return None

        res = dict()
        res['conn_id'] = self.conn_id
        res['database'] = self.db
        res['async_'] = self.async_
        res['wasConnected'] = self.wasConnected
        res['auto_reconnect'] = self.auto_reconnect

        return res

    def __repr__(self):
        return "Oracle Connection: {0} ({1}) -> {2}".format(
            self.conn_id, self.db,
            'Connected' if self.conn is not None else "Disconnected"
        )

    def __str__(self):
        return self.__repr__()

    def _check_user_password(self, kwargs):
        """
        Check user and password - mirrors the psycopg3 connection logic.
        """
        password = None
        encpass = None
        is_update_password = True

        if 'user' in kwargs and kwargs['password']:
            password = kwargs['password']
            kwargs.pop('password')
            is_update_password = False
        else:
            if 'encpass' in kwargs:
                encpass = kwargs['encpass']
            else:
                encpass = kwargs['password'] if 'password' in kwargs else None

        return password, encpass, is_update_password

    def _decode_password(self, encpass, manager, password, crypt_key):
        if encpass:
            user = User.query.filter_by(id=current_user.id).first()

            if user is None:
                return True, self.UNAUTHORIZED_REQUEST, password

            try:
                password = decrypt(encpass, crypt_key)
                if isinstance(password, bytes):
                    password = password.decode()
            except Exception as e:
                manager.stop_ssh_tunnel()
                current_app.logger.exception(e)
                return True, \
                    _(
                        "Failed to decrypt the saved password.\nError: {0}"
                    ).format(str(e)), password
        return False, '', password

    def _get_dsn(self):
        """
        Build the oracledb DSN from the manager's host/port/service.
        """
        host = self.manager.host
        port = self.manager.port if self.manager.port else 1521
        if self.manager.use_ssh_tunnel:
            host = self.manager.local_bind_host
            port = self.manager.local_bind_port
        service = self.db
        if not host:
            return service
        return "{0}:{1}/{2}".format(host, port, service)

    def connect(self, **kwargs):
        _oracledb_available()

        if self.conn is not None:
            return True, None

        manager = self.manager

        crypt_key_present, crypt_key = get_crypt_key()
        if not crypt_key_present:
            raise CryptKeyMissing()
        password, encpass, is_update_password = \
            self._check_user_password(kwargs)

        tunnel_password = kwargs['tunnel_password'] if 'tunnel_password' in \
                                                       kwargs else ''

        # Check SSH Tunnel needs to be created
        if manager.use_ssh_tunnel == 1 and not manager.tunnel_created:
            status, error = manager.create_ssh_tunnel(tunnel_password)
            if not status:
                return False, error

        if manager.use_ssh_tunnel == 1:
            manager.check_ssh_tunnel_alive()

        if is_update_password:
            if encpass is None:
                encpass = self.password or getattr(manager, 'password', None)
            self.password = encpass

        if self.reconnecting is not False:
            self.password = None

        if not crypt_key_present:
            raise CryptKeyMissing()

        is_error, errmsg, password = self._decode_password(
            encpass, manager, password, crypt_key)
        if is_error:
            return False, errmsg

        # Fall back to passexec like the psycopg3 driver does.
        if not password and not encpass and manager.passexec:
            password = manager.passexec.get()

        try:
            if 'user' in kwargs and kwargs['user']:
                user = kwargs['user']
            else:
                user = manager.user

            dsn = self._get_dsn()
            conn = oracledb.connect(
                user=user,
                password=password if password else '',
                dsn=dsn,
            )
            # The query tool relies on explicit COMMIT/ROLLBACK handling;
            # autocommit mirrors the psycopg async connection behaviour
            # where statements commit immediately unless a transaction
            # was opened with BEGIN.
            conn.autocommit = self._autocommit
            conn.call_timeout = 0
        except Exception as e:
            manager.stop_ssh_tunnel()
            errmsg = self._formatted_exception_msg(e, False)
            current_app.logger.info(
                "Failed to connect to the Oracle server (#{server_id}) for "
                "connection ({conn_id}) with error message as below"
                ":{msg}".format(
                    server_id=self.manager.sid,
                    conn_id=self.conn_id,
                    msg=errmsg
                )
            )
            return False, errmsg

        self.conn = conn
        self.wasConnected = True

        try:
            status, msg = self._initialize(conn_id=self.conn_id, **kwargs)
        except Exception as e:
            manager.stop_ssh_tunnel()
            current_app.logger.exception(e)
            self._close_connection()
            if not self.reconnecting:
                self.wasConnected = False
            raise e

        if status and is_update_password:
            manager._update_password(encpass)
        else:
            if not self.reconnecting and is_update_password:
                self.wasConnected = False

        return status, msg

    def _close_connection(self):
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:
            pass
        self.conn = None

    def _initialize(self, conn_id, **kwargs):
        self.execution_aborted = False
        self._transaction_status = 0
        self._autocommit = True
        if self.conn is not None:
            self.conn.autocommit = True

        setattr(g, self.ARGS_STR.format(
            self.manager.sid,
            self.conn_id.encode('utf-8')
        ), None)

        manager = self.manager

        # Fetch the version information - fall back to the driver's
        # version property if the view is not readable.
        status, ver = self.execute_scalar(
            "SELECT banner FROM v$version WHERE banner LIKE 'Oracle%'")
        if not status or not ver:
            try:
                ver = "Oracle Database {0}".format(self.conn.version)
            except Exception:
                ver = "Oracle Database"
        manager.ver = ver
        try:
            full_version = self.conn.version  # e.g. 19.0.0.0.0
            major = int(str(full_version).split('.')[0])
        except Exception:
            major = 0
        manager.sversion = major * 10000

        # Identify the server type explicitly - it can never be detected
        # from a PostgreSQL version string.
        manager.server_type = 'oracle'
        from pgadmin.browser.server_groups.servers.types import ServerType
        manager.server_cls = ServerType.registry.get('oracle', None)

        # Populate a minimal user info block for the UI.
        status, user_name = self.execute_scalar(
            "SELECT username FROM all_users WHERE username = "
            "UPPER(:1)", [manager.user])
        manager.user_info = {
            'id': None,
            'name': user_name if user_name else manager.user,
            'is_superuser': False,
            'can_create_role': False,
            'can_create_db': False,
            'can_signal_backend': False,
        }

        # Execute post connection SQL if provided.
        errmsg = None
        if manager.post_connection_sql and manager.post_connection_sql != '':
            status, res = self.execute_void(manager.post_connection_sql)
            if not status:
                errmsg = gettext(
                    "Failed to execute the post connection SQL "
                    "with below error message:\n{0}").format(res)

        manager.update_session()

        return True, errmsg

    def __cursor(self, server_cursor=False, scrollable=False):
        """
        Return the (wrapped) cursor for this connection, mirroring the
        psycopg3 driver's caching behaviour on the flask g object.
        """
        if not get_crypt_key()[0] and config.SERVER_MODE:
            raise CryptKeyMissing()

        if self.manager.use_ssh_tunnel == 1:
            self.manager.check_ssh_tunnel_alive()

        if self.wasConnected is False:
            raise ConnectionLost(
                self.manager.sid,
                self.db,
                None if self.conn_id[0:3] == 'DB:' else self.conn_id[5:]
            )

        cur = getattr(g, self.ARGS_STR.format(
            self.manager.sid,
            self.conn_id.encode('utf-8')
        ), None)

        if self.connected() and cur is not None:
            return True, cur

        if not self.connected():
            current_app.logger.warning(
                "Connection to Oracle server (#{server_id}) for the "
                "connection - '{conn_id}' has been lost.".format(
                    server_id=self.manager.sid,
                    conn_id=self.conn_id
                )
            )

            if self.auto_reconnect and not self.reconnecting:
                return self.__attempt_execution_reconnect(self.__cursor)

            raise ConnectionLost(
                self.manager.sid,
                self.db,
                None if self.conn_id[0:3] == 'DB:' else self.conn_id[5:]
            )

        try:
            cur = OracleCursorWrapper(self.conn.cursor())
        except Exception as e:
            current_app.logger.exception(e)
            errmsg = gettext(
                "Failed to create cursor for oracledb connection with error "
                "message for the server#{1}:{2}:\n{0}"
            ).format(
                str(e), self.manager.sid, self.db
            )
            current_app.logger.error(errmsg)
            raise ConnectionLost(
                self.manager.sid,
                self.db,
                None if self.conn_id[0:3] == 'DB:' else self.conn_id[5:]
            )

        setattr(
            g, self.ARGS_STR.format(
                self.manager.sid, self.conn_id.encode('utf-8')
            ), cur
        )

        return True, cur

    def _translate_params(self, query, params):
        """
        Convert psycopg style %s placeholders to oracledb :n placeholders
        and lists/dicts to oracledb friendly parameters.
        """
        if params is None:
            return query, None

        if isinstance(params, dict):
            return query, params

        count = [0]

        def _sub(_m):
            count[0] += 1
            return ':{0}'.format(count[0])

        query = _PLACEHOLDER_RE.sub(_sub, query)
        return query, list(params)

    def _execute_internal(self, cur, query, params=None):
        """
        Execute handling pgAdmin's internal transaction statements and
        placeholder translation. Returns None on success or an error
        message string.
        """
        try:
            query, params = self._translate_params(query, params)

            if _BEGIN_RE.match(query):
                # Oracle starts transactions implicitly; turning autocommit
                # off is the equivalent of BEGIN.
                self._autocommit = False
                if self.conn is not None:
                    self.conn.autocommit = False
                self._transaction_status = 2
                cur._query = query
                return None

            if _COMMIT_RE.match(query) or _ROLLBACK_RE.match(query):
                if self.conn is not None and not self.conn.autocommit:
                    if _COMMIT_RE.match(query):
                        self.conn.commit()
                    else:
                        self.conn.rollback()
                self._autocommit = True
                if self.conn is not None:
                    self.conn.autocommit = True
                self._transaction_status = 0
                cur._query = query
                return None

            cur.execute(query, params or {})
            cur._query = query
            return None
        except Exception as e:
            if self._transaction_status == 2:
                self._transaction_status = 3
            return self._formatted_exception_msg(e, False)

    def execute_scalar(self, query, params=None,
                       formatted_exception_msg=False):
        status, cur = self.__cursor()
        self.row_count = 0

        if not status:
            return False, str(cur)

        current_app.logger.log(
            25,
            "Execute (scalar) on Oracle #{server_id} - {conn_id}:\n{query}"
            .format(
                server_id=self.manager.sid,
                conn_id=self.conn_id,
                query=query
            )
        )

        errmsg = self._execute_internal(cur, query, params)
        if errmsg is not None:
            if not self.connected():
                if self.auto_reconnect and not self.reconnecting:
                    return self.__attempt_execution_reconnect(
                        self.execute_scalar, query, params,
                        formatted_exception_msg
                    )
                raise ConnectionLost(
                    self.manager.sid,
                    self.db,
                    None if self.conn_id[0:3] == 'DB:' else self.conn_id[5:]
                )
            current_app.logger.error(
                "Failed to execute query (execute_scalar) on Oracle server "
                "#{server_id} - {conn_id}:\nError Message:{errmsg}".format(
                    server_id=self.manager.sid,
                    conn_id=self.conn_id,
                    errmsg=errmsg
                )
            )
            return False, errmsg

        if cur.description is not None:
            row = cur.fetchone()
            self.row_count = 1 if row is not None else 0
            if row is not None and len(row) > 0:
                return True, row[0]

        return True, None

    def execute_void(self, query, params=None, formatted_exception_msg=False):
        status, cur = self.__cursor()

        if not status:
            return False, str(cur)

        current_app.logger.log(
            25,
            "Execute (void) on Oracle #{server_id} - {conn_id}:\n{query}"
            .format(
                server_id=self.manager.sid,
                conn_id=self.conn_id,
                query=query
            )
        )

        errmsg = self._execute_internal(cur, query, params)
        if errmsg is not None:
            if not self.connected():
                if self.auto_reconnect and not self.reconnecting:
                    return self.__attempt_execution_reconnect(
                        self.execute_void, query, params,
                        formatted_exception_msg
                    )
                raise ConnectionLost(
                    self.manager.sid,
                    self.db,
                    None if self.conn_id[0:3] == 'DB:' else self.conn_id[5:]
                )
            current_app.logger.error(
                "Failed to execute query (execute_void) on Oracle server "
                "#{server_id} - {conn_id}:\nError Message:{errmsg}".format(
                    server_id=self.manager.sid,
                    conn_id=self.conn_id,
                    errmsg=errmsg
                )
            )
            return False, errmsg

        return True, None

    def __attempt_execution_reconnect(self, fn, *args, **kwargs):
        self.reconnecting = True
        setattr(g, self.ARGS_STR.format(
            self.manager.sid,
            self.conn_id.encode('utf-8')
        ), None)
        try:
            status, res = self.connect()
            if status:
                if fn:
                    status, res = fn(*args, **kwargs)
                    self.reconnecting = False
                return status, res
        except Exception as e:
            current_app.logger.exception(e)
            self.reconnecting = False

            current_app.logger.warning(
                "Failed to reconnect the Oracle server "
                "(Server #{server_id}, Connection #{conn_id})".format(
                    server_id=self.manager.sid,
                    conn_id=self.conn_id
                )
            )
        self.reconnecting = False
        raise ConnectionLost(
            self.manager.sid,
            self.db,
            None if self.conn_id[0:3] == 'DB:' else self.conn_id[5:]
        )

    def _columns_from_description(self, cur):
        """
        Convert an oracledb description into the column info shape the
        pgAdmin frontend expects.
        """
        columns = []
        if cur.description is None:
            return columns
        for d in cur.description:
            type_code = str(d[1])  # oracledb DbType, e.g. DB_TYPE_NUMBER
            columns.append({
                'name': d[0],
                'type_code': type_code,
                'display_size': d[2],
                'internal_size': d[3],
                'precision': d[4],
                'scale': d[5],
                'null_ok': d[6],
            })
        return columns

    def execute_2darray(self, query, params=None,
                        formatted_exception_msg=False, prepare=None):
        status, cur = self.__cursor()
        self.row_count = 0

        if not status:
            return False, str(cur)

        current_app.logger.log(
            25,
            "Execute (2darray) on Oracle #{server_id} - {conn_id}:\n{query}"
            .format(
                server_id=self.manager.sid,
                conn_id=self.conn_id,
                query=query
            )
        )

        errmsg = self._execute_internal(cur, query, params)
        if errmsg is not None:
            if not self.connected() and self.auto_reconnect and \
                    not self.reconnecting:
                return self.__attempt_execution_reconnect(
                    self.execute_2darray, query, params,
                    formatted_exception_msg
                )
            current_app.logger.error(
                "Failed to execute query (execute_2darray) on Oracle "
                "server #{server_id} - {conn_id}:\nError "
                "Message:{errmsg}".format(
                    server_id=self.manager.sid,
                    conn_id=self.conn_id,
                    errmsg=errmsg
                )
            )
            return False, errmsg

        columns = self._columns_from_description(cur)
        rows = []
        if cur.description is not None:
            rows = [tuple(r) for r in cur.fetchall()]
            self.row_count = len(rows)
        else:
            self.row_count = cur.rowcount if cur.rowcount >= 0 else 0

        return True, {'columns': columns, 'rows': rows}

    def execute_dict(self, query, params=None, formatted_exception_msg=False):
        status, cur = self.__cursor()
        self.row_count = 0

        if not status:
            return False, str(cur)

        current_app.logger.log(
            25,
            "Execute (dict) on Oracle #{server_id} - {conn_id}:\n{query}"
            .format(
                server_id=self.manager.sid,
                conn_id=self.conn_id,
                query=query
            )
        )

        errmsg = self._execute_internal(cur, query, params)
        if errmsg is not None:
            if not self.connected():
                if self.auto_reconnect and not self.reconnecting:
                    return self.__attempt_execution_reconnect(
                        self.execute_dict, query, params,
                        formatted_exception_msg
                    )
                raise ConnectionLost(
                    self.manager.sid,
                    self.db,
                    None if self.conn_id[0:3] == 'DB:' else self.conn_id[5:]
                )
            current_app.logger.error(
                "Failed to execute query (execute_dict) on Oracle server "
                "#{server_id} - {conn_id}:\nError Message:{errmsg}".format(
                    server_id=self.manager.sid,
                    conn_id=self.conn_id,
                    errmsg=errmsg
                )
            )
            return False, errmsg

        columns = self._columns_from_description(cur)

        rows = []
        if cur.description is not None:
            col_names = [c['name'].lower() for c in columns]
            for r in cur.fetchall():
                rows.append(dict(zip(col_names, r)))
            self.row_count = len(rows)
        else:
            self.row_count = cur.rowcount if cur.rowcount >= 0 else 0

        return True, {'columns': columns, 'rows': rows}

    def execute_async(self, query, params=None, formatted_exception_msg=True,
                      server_cursor=False):
        """
        The query tool runs the SQL in a background thread managed by
        pgAdmin itself, so 'async' here means: execute synchronously and
        buffer the results for poll()/async_fetchmany_2darray().
        """
        self.__async_cursor = None
        self.__async_query_error = None
        self.__async_result = None
        self.__async_rows_affected = 0
        self.execution_aborted = False

        status, cur = self.__cursor(scrollable=True,
                                    server_cursor=server_cursor)

        if not status:
            return False, str(cur)

        current_app.logger.log(
            25,
            "Execute (async) on Oracle #{server_id} - {conn_id}:\n{query}"
            .format(
                server_id=self.manager.sid,
                conn_id=self.conn_id,
                query=query
            )
        )

        self.__async_cursor = cur

        errmsg = self._execute_internal(cur, query, params)
        if errmsg is not None:
            self.__async_query_error = errmsg
            current_app.logger.error(
                "Failed to execute query (execute_async) on Oracle server "
                "#{server_id} - {conn_id}:\nError Message:{errmsg}".format(
                    server_id=self.manager.sid,
                    conn_id=self.conn_id,
                    errmsg=errmsg
                )
            )
            if not self.connected() or self._is_disconnected_error(
                    self.__async_query_error):
                raise ConnectionLost(
                    self.manager.sid,
                    self.db,
                    None if self.conn_id[0:3] == 'DB:' else self.conn_id[5:]
                )
            return False, errmsg

        # Buffer the result set now; fetches later slice from memory.
        try:
            if cur.description is not None:
                self.__async_result = [tuple(r) for r in cur.fetchall()]
                self.__async_rows_affected = len(self.__async_result)
            else:
                self.__async_result = None
                self.__async_rows_affected = \
                    cur.rowcount if cur.rowcount >= 0 else 0
        except Exception as e:
            errmsg = self._formatted_exception_msg(e, formatted_exception_msg)
            self.__async_query_error = errmsg
            return False, errmsg

        self.row_count = self.__async_rows_affected

        return True, None

    def async_fetchmany_2darray(self, records=2000,
                                from_rownum=0, to_rownum=0,
                                formatted_exception_msg=False):
        cur = self.__async_cursor
        if not cur:
            return False, self.CURSOR_NOT_FOUND

        if not self.conn:
            raise ConnectionLost(
                self.manager.sid,
                self.db,
                None if self.conn_id[0:3] == 'DB:' else self.conn_id[5:]
            )

        if self.__async_result is None:
            # Operation which does not produce a result set (DDL/DML).
            return True, None

        try:
            if records == -1 or records is None:
                return True, self.__async_result[from_rownum:to_rownum + 1] \
                    if records is None else self.__async_result
            return True, self.__async_result[:records]
        except Exception as e:
            return False, self._formatted_exception_msg(e, False)

    def connected(self):
        return self.conn is not None

    def _decrypt_password(self, manager):
        password = getattr(manager, 'password', None)
        if password:
            user = User.query.filter_by(id=current_user.id).first()

            if user is None:
                return False, self.UNAUTHORIZED_REQUEST, password

            crypt_key_present, crypt_key = get_crypt_key()
            if not crypt_key_present:
                return False, crypt_key, password

            password = decrypt(password, crypt_key).decode()
        return True, '', password

    def reset(self):
        if self.conn is not None:
            self._close_connection()

        manager = self.manager

        is_return, return_value, password = self._decrypt_password(manager)
        if is_return:
            return False, return_value

        try:
            conn = oracledb.connect(
                user=manager.user,
                password=password if password else '',
                dsn=self._get_dsn(),
            )
        except Exception as e:
            msg = self._formatted_exception_msg(e, False)
            current_app.logger.error(
                gettext("Failed to reset the connection to the Oracle "
                        "server due to following error:\n{0}").format(msg)
            )
            return False, msg

        conn.autocommit = True
        self.conn = conn

        return True, None

    def transaction_status(self):
        return self._transaction_status

    def async_query_error(self):
        return self.__async_query_error

    def ping(self):
        if not self.connected():
            return False

        if self._transaction_status != 0:
            return True

        try:
            cur = self.conn.cursor()
            cur.execute("SELECT 1 FROM DUAL")
            cur.close()
            return True
        except Exception:
            self._close_connection()
            return False

    def _release(self):
        if self.wasConnected:
            self._close_connection()
            self.password = None
            self.wasConnected = False

    def _wait(self, conn):
        pass

    def _wait_timeout(self, conn, time):
        pass

    def poll(self, formatted_exception_msg=False, no_result=False):
        cur = self.__async_cursor

        if self.__async_query_error:
            return False, self.__async_query_error

        if not cur:
            return False, self.CURSOR_NOT_FOUND

        result = None
        self.row_count = 0
        self.column_info = self._columns_from_description(cur)

        pos = 0
        for col in self.column_info:
            col['pos'] = pos
            pos += 1

        self.row_count = self.__async_rows_affected

        if not no_result and self.__async_result is not None:
            result = self.__async_result

        return 1, result

    def status_message(self):
        cur = self.__async_cursor
        if not cur:
            return self.CURSOR_NOT_FOUND

        if self.__async_result is not None:
            return gettext("{0} rows selected").format(
                len(self.__async_result))

        if self.__async_rows_affected and self.__async_rows_affected > 0:
            return gettext("{0} row(s) affected").format(
                self.__async_rows_affected)

        return None

    def rows_affected(self):
        return self.row_count

    @property
    def total_rows(self):
        if self.__async_result is None:
            return self.__async_rows_affected
        return len(self.__async_result)

    def get_column_info(self):
        return self.column_info

    def reset_cursor_at(self, position):
        # Results are buffered in memory; nothing to reset.
        pass

    def release_async_cursor(self):
        self.__async_cursor = None

    def cancel_transaction(self, conn_id, did=None):
        """
        Cancel the running statement using oracledb's connection break.
        """
        status = True
        msg = ''
        try:
            if self.conn is not None:
                self.conn.break_()
                self.execution_aborted = True
            else:
                status = False
                msg = gettext("Not connected to the database server.")
        except Exception as e:
            status = False
            msg = self._formatted_exception_msg(e, False)

        return status, msg

    def messages(self):
        """
        Oracle does not send asynchronous notices; return any buffered
        messages (none for now).
        """
        return []

    def get_notifies(self):
        return None

    def get_notices(self):
        return ''

    def check_notifies(self, n=None):
        pass

    def _is_disconnected_error(self, errmsg):
        for code in _DISCONNECT_ERRORS:
            if code in str(errmsg):
                return True
        return False

    def is_disconnected(self, err):
        if self.conn is None:
            return True
        return self._is_disconnected_error(str(err))

    def _formatted_exception_msg(self, exception_obj, formatted_msg):
        """
        oracledb exceptions already carry ORA-xxxxx codes and messages.
        """
        errmsg = str(exception_obj)
        try:
            # oracledb errors expose .message with the readable text.
            if hasattr(exception_obj, 'message') and exception_obj.message:
                errmsg = str(exception_obj.message)
                if getattr(exception_obj, 'code', None):
                    errmsg = "{0} ({1})".format(errmsg, exception_obj.code)
        except Exception:
            pass

        if not formatted_msg:
            return errmsg

        if not errmsg.startswith('ERROR:'):
            errmsg = gettext('ERROR:  ') + errmsg + ' \n\n'
        return errmsg
