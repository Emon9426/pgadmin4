##########################################################################
#
# pgAdmin 4 - PostgreSQL Tools
#
# Copyright (C) 2013 - 2026, The pgAdmin Development Team
# This software is released under the PostgreSQL Licence
#
##########################################################################

"""
Implementation of Driver class for Oracle servers.
It is a wrapper around the python-oracledb driver, and connection
objects.

The Oracle driver is registered under the 'oracle' key in the driver
registry. Call sites that need to work with any server use
get_driver(PG_DEFAULT_DRIVER).connection_manager(sid) - the psycopg3
driver dispatches to this driver when the server row has
server_type == 'oracle'.
"""
import datetime

from flask import session
from flask_babel import gettext
from flask_login import current_user
from threading import Lock

import config
from pgadmin.model import Server
from pgadmin.utils.server_access import get_server, \
    get_user_server_query
from pgadmin.utils.exception import ObjectGone
from ..abstract import BaseDriver
from .server_manager import ServerManager

try:
    import oracledb
except ImportError:
    oracledb = None

connection_restore_lock = Lock()


class Driver(BaseDriver):
    """
    class Driver(BaseDriver)

    This driver acts as a wrapper around the python-oracledb driver
    implementation for Oracle Database servers.

    Methods:
    -------
    * connection_manager(sid)
    - It returns the Oracle server connection manager for this session.

    * get_connection(sid, database, conn_id, auto_reconnect)
    - It returns a Connection class object, which may/may not be connected
      to the database server for this session.

    * release_connection(sid, database, conn_id)
    - It releases the connection object for the given conn_id/database for
      this session.
    """

    def __init__(self, **kwargs):
        self.managers = dict()
        super(Driver, self).__init__()

    def _restore_connections_from_session(self):
        """
        Used internally by connection_manager to restore connections
        from sessions.
        """
        if session.sid not in self.managers:
            self.managers[session.sid] = managers = dict()
            if '__oracle_server_managers' in session:
                session_managers = \
                    session['__oracle_server_managers'].copy()
                servers = get_user_server_query().filter(
                    Server.is_adhoc == 0).filter(
                    Server.server_type == 'oracle')
                for server in servers:
                    manager = managers[str(server.id)] = \
                        ServerManager(server)
                    if config.SERVER_MODE and server.shared and \
                            server.user_id != current_user.id:
                        manager.passexec = None
                    if server.id in session_managers:
                        manager._restore(
                            session_managers[server.id])
                        manager.update_session()
            return managers

        return {}

    def connection_manager(self, sid=None):
        """
        connection_manager(...)

        Returns the ServerManager object for the current session. It will
        create a new ServerManager object (if necessary).

        Parameters:
            sid
            - Server ID
        """
        assert (sid is not None and isinstance(sid, int))
        managers = None

        # In server mode, verify the current user has access to this
        # server - mirrors the psycopg3 driver security boundary.
        if config.SERVER_MODE:
            if current_user and current_user.is_authenticated:
                server_data = get_server(sid)
            else:
                raise ObjectGone(
                    gettext("Server not found."))
            if server_data is None:
                raise ObjectGone(
                    gettext("Server not found."))
        else:
            server_data = Server.query.filter_by(id=sid).first()
            if server_data is None:
                return None

        if session.sid not in self.managers:
            with connection_restore_lock:
                managers = self._restore_connections_from_session()
        else:
            managers = self.managers[session.sid]
            if str(sid) in managers:
                manager = managers[str(sid)]
                with connection_restore_lock:
                    manager._restore_connections()
                    manager.update_session()

        managers['pinged'] = datetime.datetime.now()
        if str(sid) not in managers:
            manager = ServerManager(server_data)
            if config.SERVER_MODE and server_data.shared and \
                    server_data.user_id != current_user.id:
                manager.passexec = None
            managers[str(sid)] = manager

            return manager

        return managers[str(sid)]

    def version(self):
        """
        Returns the current version of the python-oracledb driver
        """
        if oracledb is not None and getattr(oracledb, '__version__', None):
            return oracledb.__version__

        raise Exception(
            "Driver Version information for oracledb is not available!"
        )

    def libpq_version(self):
        """
        Oracle connections do not use libpq; report the driver version.
        """
        return self.version()

    def get_connection(
            self, sid, database=None, conn_id=None, auto_reconnect=True,
            **kwargs
    ):
        """
        get_connection(...)

        Returns the connection object for the certain connection-id/database
        for the specific server, identified by sid.
        """
        manager = self.connection_manager(sid)

        return manager.connection(database=database, conn_id=conn_id,
                                  auto_reconnect=auto_reconnect)

    def release_connection(self, sid, database=None, conn_id=None):
        """
        Release the connection for the given connection-id/database in this
        session.
        """
        return self.connection_manager(sid).release(database, conn_id)

    def delete_manager(self, sid):
        """
        Delete manager for given server id.
        """
        manager = self.connection_manager(sid)
        if manager is not None:
            manager.release()
        if session.sid in self.managers and \
                str(sid) in self.managers[session.sid]:
            del self.managers[session.sid][str(sid)]

    def gc_timeout(self):
        """
        Release the connections for the sessions, which have not pinged the
        server for more than config.MAX_SESSION_IDLE_TIME.
        """
        max_idle_time = max(config.MAX_SESSION_IDLE_TIME or 60, 20)
        session_idle_timeout = datetime.timedelta(minutes=max_idle_time)

        curr_time = datetime.datetime.now()

        for sess in self.managers:
            sess_mgr = self.managers[sess]

            if sess == session.sid:
                sess_mgr['pinged'] = curr_time
                continue
            if curr_time - sess_mgr['pinged'] >= session_idle_timeout:
                for mgr in [
                    m for m in sess_mgr.values() if isinstance(m,
                                                               ServerManager)
                ]:
                    mgr.release()

    def gc_own(self):
        """
        Release the connections for the current session.
        """
        sess_mgr = self.managers.get(session.sid, None)

        if sess_mgr:
            for mgr in (
                m for m in sess_mgr.values() if isinstance(m, ServerManager)
            ):
                mgr.release()
