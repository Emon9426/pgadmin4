##########################################################################
#
# pgAdmin 4 - PostgreSQL Tools
#
# Copyright (C) 2013 - 2026, The pgAdmin Development Team
# This software is released under the PostgreSQL Licence
#
##########################################################################

"""
Implementation of ServerManager for Oracle servers.
"""
import datetime
import logging
import config

from flask import current_app
from flask_security import current_user
from flask_babel import gettext

from pgadmin.utils import get_complete_file_path
from pgadmin.utils.crypto import decrypt
from pgadmin.utils.exception import ConnectionLost, SSHTunnelConnectionLost, \
    CryptKeyMissing
from pgadmin.utils.master_password import get_crypt_key
from pgadmin.utils.passexec import PasswordExec
from pgadmin.model import Server, User
from .connection import Connection

if config.SUPPORT_SSH_TUNNEL:
    from sshtunnel import SSHTunnelForwarder, BaseSSHTunnelForwarderError

CONN_STRING = 'CONN:{0}'
DB_STRING = 'DB:{0}'


class ServerManager(object):
    """
    class ServerManager

    This class contains the information about the given Oracle server,
    and acts as the connection manager for that particular session.
    """
    _INFORMATION_MSG = gettext("Information is not available.")

    def __init__(self, server):
        self.connections = dict()
        self.local_bind_host = '127.0.0.1'
        self.local_bind_port = None
        self.tunnel_object = None
        self.tunnel_created = False
        self.display_connection_string = ''

        self.update(server)

    def update(self, server):
        assert (server is not None)
        assert (isinstance(server, Server))

        self.ver = None
        self.sversion = None
        # The server type is known upfront for Oracle servers, it cannot
        # be detected from a PostgreSQL version string.
        self.server_type = getattr(server, 'server_type', None) \
            if getattr(server, 'server_type', None) == 'oracle' else 'oracle'
        self.server_cls = None
        self.password = None
        self.tunnel_password = None

        self.sid = server.id
        self.host = server.host
        self.port = server.port
        self.db = server.maintenance_db
        self.shared = server.shared
        self.did = 0
        self.user = server.username
        self.password = server.password
        self.role = server.role
        self.pinged = datetime.datetime.now()
        self.db_info = dict()
        self.name = server.name
        self.passexec = \
            PasswordExec(server.passexec_cmd, server.host, server.port,
                         server.username, server.passexec_expiration) \
            if server.passexec_cmd else None
        self.service = server.service
        self.user_info = None

        if config.SUPPORT_SSH_TUNNEL:
            self.use_ssh_tunnel = server.use_ssh_tunnel
            self.tunnel_host = server.tunnel_host
            self.tunnel_port = \
                22 if server.tunnel_port is None else server.tunnel_port
            self.tunnel_username = server.tunnel_username
            self.tunnel_authentication = 0 \
                if server.tunnel_authentication is None \
                else server.tunnel_authentication
            self.tunnel_identity_file = server.tunnel_identity_file
            self.tunnel_prompt_password = server.tunnel_prompt_password
            self.tunnel_password = server.tunnel_password
            self.tunnel_keep_alive = server.tunnel_keep_alive
        else:
            self.use_ssh_tunnel = 0
            self.tunnel_host = None
            self.tunnel_port = 22
            self.tunnel_username = None
            self.tunnel_authentication = None
            self.tunnel_identity_file = None
            self.tunnel_prompt_password = 0
            self.tunnel_password = None
            self.tunnel_keep_alive = 0

        self.kerberos_conn = False
        self.gss_authenticated = False
        self.gss_encrypted = False
        self.connection_params = server.connection_params
        self.post_connection_sql = server.post_connection_sql

        # A DSN for display purposes only - password is never included.
        self.display_connection_string = \
            "{0}/{1}@{2}:{3}/{4}".format(
                self.user or '', 'xxxxxxx', self.host or 'localhost',
                self.port or 1521, self.db or '')

        for con in self.connections:
            self.connections[con]._release()

        self.update_session()

        self.connections = dict()

    def _set_password(self, res):
        if hasattr(self, 'password') and self.password:
            if hasattr(self.password, 'decode'):
                res['password'] = self.password.decode('utf-8')
            else:
                res['password'] = str(self.password)
        else:
            res['password'] = self.password

    def as_dict(self):
        """
        Returns a dictionary object representing the server manager.
        """
        if self.ver is None or len(self.connections) == 0:
            return None

        res = dict()
        res['sid'] = self.sid
        res['ver'] = self.ver
        res['sversion'] = self.sversion

        self._set_password(res)

        if self.use_ssh_tunnel:
            if hasattr(self, 'tunnel_password') and self.tunnel_password:
                if hasattr(self.tunnel_password, 'decode'):
                    res['tunnel_password'] = \
                        self.tunnel_password.decode('utf-8')
                else:
                    res['tunnel_password'] = str(self.tunnel_password)
            else:
                res['tunnel_password'] = self.tunnel_password

        connections = res['connections'] = dict()

        for conn_id in self.connections:
            conn = self.connections[conn_id].as_dict()

            if conn is not None:
                connections[conn_id] = conn

        return res

    def server_version(self):
        return self.ver

    @property
    def version(self):
        return self.sversion

    def major_version(self):
        if self.sversion is not None:
            return int(self.sversion / 10000)
        raise Exception(self._INFORMATION_MSG)

    def minor_version(self):
        if self.sversion:
            return int(int(self.sversion / 100) % 100)
        raise Exception(self._INFORMATION_MSG)

    def patch_version(self):
        if self.sversion:
            return int(int(self.sversion / 100) / 100)
        raise Exception(self._INFORMATION_MSG)

    def connection(self, **kwargs):
        database = kwargs.get('database', None)
        conn_id = kwargs.get('conn_id', None)
        auto_reconnect = kwargs.get('auto_reconnect', True)
        did = kwargs.get('did', None)

        # Oracle connects to a single service per server; the database
        # argument (if any) only selects the service name.
        if database is None:
            conn_str = CONN_STRING.format(conn_id)
            if did is None:
                database = self.db
            elif did in self.db_info:
                database = self.db_info[did].get('datname', self.db)
            elif conn_id and conn_str in self.connections:
                database = self.connections[conn_str].db
            else:
                database = self.db

        if not get_crypt_key()[0] and (
                config.SERVER_MODE or not config.USE_OS_SECRET_STORAGE):
            # the reason its not connected might be missing key
            raise CryptKeyMissing()

        if database is None:
            if self.use_ssh_tunnel == 1:
                self.check_ssh_tunnel_alive()
            else:
                raise ConnectionLost(self.sid, None, None)

        my_id = (CONN_STRING.format(conn_id)) if conn_id is not None else \
            (DB_STRING.format(database))

        self.pinged = datetime.datetime.now()

        if my_id in self.connections:
            return self.connections[my_id]

        self.connections[my_id] = Connection(
            self, my_id, database, auto_reconnect=auto_reconnect,
            async_=1 if conn_id is not None else 0
        )

        return self.connections[my_id]

    def _check_and_reconnect_server(self, conn, conn_info, data):
        if conn_info['wasConnected'] and conn_info['auto_reconnect']:
            try:
                if self.use_ssh_tunnel == 1 and \
                        not self.tunnel_created:
                    self.create_ssh_tunnel(data['tunnel_password'])
                    self.check_ssh_tunnel_alive()

                conn.connect(password=data['password'])
            except CryptKeyMissing:
                conn.wasConnected = conn_info['wasConnected']
                conn.auto_reconnect = conn_info['auto_reconnect']
            except Exception as e:
                current_app.logger.exception(e)
                self.connections.pop(conn_info['conn_id'], None)
                raise

    def _restore(self, data):
        """
        Restore auto-connect connections on app server restart.
        """
        if 'password' in data and data['password'] and \
                hasattr(data['password'], 'encode'):
            data['password'] = data['password'].encode('utf-8')
        if 'tunnel_password' in data and data['tunnel_password']:
            data['tunnel_password'] = \
                data['tunnel_password'].encode('utf-8')

        self.pinged = datetime.datetime.now()

        connections = data['connections']

        for conn_id in connections:
            conn_info = connections[conn_id]
            if conn_info['conn_id'] in self.connections:
                conn = self.connections[conn_info['conn_id']]
            else:
                conn = self.connections[conn_info['conn_id']] = Connection(
                    self, conn_info['conn_id'], conn_info['database'],
                    auto_reconnect=conn_info['auto_reconnect'],
                    async_=conn_info['async_'],
                )

            self._check_and_reconnect_server(conn, conn_info, data)

    def _restore_connections(self):
        for conn_id in self.connections:
            conn = self.connections[conn_id]
            was_connected = conn.wasConnected
            auto_reconnect = conn.auto_reconnect
            if conn.wasConnected and conn.auto_reconnect:
                try:
                    if self.use_ssh_tunnel == 1 and \
                       not self.tunnel_created:
                        self.create_ssh_tunnel(self.tunnel_password)
                        self.check_ssh_tunnel_alive()

                    conn.connect()
                except CryptKeyMissing:
                    conn.wasConnected = was_connected
                    conn.auto_reconnect = auto_reconnect
                except Exception as e:
                    self.connections.pop(conn_id, None)
                    current_app.logger.exception(e)
                    raise

    def release(self, database=None, conn_id=None, did=None):
        if database is None and conn_id is None and did is None:
            self.stop_ssh_tunnel()

        my_id = None
        if conn_id is not None:
            my_id = CONN_STRING.format(conn_id)
        elif database is not None:
            my_id = DB_STRING.format(database)

        if my_id is not None:
            if my_id in self.connections:
                self.connections[my_id]._release()
                del self.connections[my_id]
                if len(self.connections) == 0:
                    self.ver = None
                    self.sversion = None
                    self.password = None

                self.update_session()
                return True
            return False

        for con_key in list(self.connections.keys()):
            self.connections[con_key]._release()

        self.connections = dict()
        self.ver = None
        self.sversion = None
        self.password = None

        self.update_session()

        return True

    def _update_password(self, passwd):
        self.password = passwd
        for conn_id in self.connections:
            conn = self.connections[conn_id]
            if conn.conn is not None or conn.wasConnected is True:
                conn.password = passwd

    def update_session(self):
        from flask import session
        managers = session['__oracle_server_managers'] \
            if '__oracle_server_managers' in session else dict()
        updated_mgr = self.as_dict()

        if not updated_mgr:
            if self.sid in managers:
                managers.pop(self.sid)
        else:
            managers[self.sid] = updated_mgr
        session['__oracle_server_managers'] = managers
        session.force_write = True

    def utility(self, operation):
        """
        Oracle support does not provide external utilities (pg_dump etc).
        """
        return None

    def export_password_env(self, env):
        if self.password:
            crypt_key_present, crypt_key = get_crypt_key()
            if not crypt_key_present:
                return False, crypt_key
            password = decrypt(self.password, crypt_key).decode()
            import os
            os.environ[str(env)] = password
        elif self.passexec:
            password = self.passexec.get()
            import os
            os.environ[str(env)] = password

    def create_ssh_tunnel(self, tunnel_password):
        """
        Create an ssh tunnel and update the host/port to the local bind
        address - mirrors the psycopg3 server manager implementation.
        """
        user = User.query.filter_by(id=current_user.id).first()
        if user is None:
            return False, gettext("Unauthorized request.")

        if tunnel_password is not None and tunnel_password != '':
            crypt_key_present, crypt_key = get_crypt_key()
            if not crypt_key_present:
                raise CryptKeyMissing()

            try:
                tunnel_password = decrypt(tunnel_password, crypt_key)
                if isinstance(tunnel_password, bytes):
                    tunnel_password = tunnel_password.decode()
            except Exception as e:
                current_app.logger.exception(e)
                return False, gettext("Failed to decrypt the SSH tunnel "
                                      "password.\nError: {0}").format(str(e))

        try:
            ssh_logger = None
            if current_app.debug:
                ssh_logger = logging.getLogger('sshtunnel')
                ssh_logger.setLevel(logging.DEBUG)
                for h in current_app.logger.handlers:
                    ssh_logger.addHandler(h)
            if self.tunnel_authentication == 1:
                self.tunnel_object = SSHTunnelForwarder(
                    (self.tunnel_host, int(self.tunnel_port)),
                    ssh_username=self.tunnel_username,
                    ssh_pkey=get_complete_file_path(self.tunnel_identity_file),
                    ssh_private_key_password=tunnel_password,
                    remote_bind_address=(self.host, self.port),
                    logger=ssh_logger,
                    set_keepalive=int(self.tunnel_keep_alive)
                )
            else:
                self.tunnel_object = SSHTunnelForwarder(
                    (self.tunnel_host, int(self.tunnel_port)),
                    ssh_username=self.tunnel_username,
                    ssh_password=tunnel_password,
                    remote_bind_address=(self.host, self.port),
                    logger=ssh_logger,
                    set_keepalive=int(self.tunnel_keep_alive)
                )
            self.tunnel_object.daemon_forward_servers = True
            self.tunnel_object.start()
            self.tunnel_created = True
        except BaseSSHTunnelForwarderError as e:
            current_app.logger.exception(e)
            return False, gettext(
                "Failed to create the SSH tunnel. Possible causes:\n"
                "1. Enter the correct tunnel password (Clear saved password "
                "if it has changed).\n 2. If using an identity file that "
                "requires a password, enable “Prompt for Password?” in the "
                "server dialog. \n 3. Verify the host address.")

        self.local_bind_port = self.tunnel_object.local_bind_port

        return True, None

    def check_ssh_tunnel_alive(self):
        if self.tunnel_object is None or not self.tunnel_object.is_active:
            self.tunnel_created = False
            raise SSHTunnelConnectionLost(self.tunnel_host)

    def stop_ssh_tunnel(self):
        if self.tunnel_object and self.tunnel_object.is_active:
            self.tunnel_object.stop()
            self.local_bind_port = None
            self.tunnel_object = None
            self.tunnel_created = False

    def get_connection_param_value(self, param_name):
        value = None
        if self.connection_params and param_name in self.connection_params:
            value = self.connection_params[param_name]

        return value
