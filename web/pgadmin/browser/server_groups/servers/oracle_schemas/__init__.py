##########################################################################
#
# pgAdmin 4 - PostgreSQL Tools
#
# Copyright (C) 2013 - 2026, The pgAdmin Development Team
# This software is released under the PostgreSQL Licence
#
##########################################################################

"""Implements the Oracle Schemas node for Oracle connections.

Lists the users/schemas visible to the connected Oracle user. The nodes
are leaves - their main purpose is to give the Query Tool a place to
attach to.
"""

from functools import wraps

from pgadmin.browser.server_groups import servers
from flask import current_app, render_template
from flask_babel import gettext
from pgadmin.browser.collection import CollectionNodeModule
from pgadmin.browser.utils import PGChildNodeView
from pgadmin.utils.ajax import make_json_response, \
    internal_server_error, gone
from pgadmin.utils.driver import get_driver
from config import PG_DEFAULT_DRIVER

# Limit the tree to a sane number of schemas - Oracle databases can
# easily have thousands of users. ROWNUM filtering keeps this
# compatible with older Oracle releases.
_MAX_SCHEMAS = 500

# Limit the tree to a sane number of schemas - Oracle databases can
# easily have thousands of users. ROWNUM filtering keeps this
# compatible with older Oracle releases.
_LIST_USERS_SQL = """
SELECT username FROM (
    SELECT username FROM all_users ORDER BY username
) WHERE ROWNUM <= {limit}
""".format(limit=_MAX_SCHEMAS)

_USER_SQL = """
SELECT username FROM all_users WHERE username = UPPER(:1)
"""


class OracleSchemaModule(CollectionNodeModule):
    """
    Module for the Oracle schemas collection node.
    """
    _NODE_TYPE = 'oracle_schema'
    _COLLECTION_LABEL = gettext("Schemas")

    def __init__(self, import_name, **kwargs):
        super().__init__(import_name, **kwargs)

        self.min_ver = 0
        self.max_ver = None
        self.server_type = ['oracle']

    def get_nodes(self, gid, sid):
        """
        Generate the collection node
        """
        yield self.generate_browser_collection_node(sid)

    @property
    def script_load(self):
        """
        Load the module script for server, when any of the server-group node
        is initialized.
        """
        return servers.ServerModule.node_type

    @property
    def csssnippets(self):
        """
        Returns a snippet of css to include in the page
        """
        snippets = [
            render_template(
                self._COLLECTION_CSS,
                node_type=self.node_type
            ),
            render_template(
                "oracle_schemas/css/oracle_schemas.css",
                node_type=self.node_type
            )
        ]

        for submodule in self.submodules:
            snippets.extend(submodule.csssnippets)

        return snippets

    @property
    def module_use_template_javascript(self):
        """
        Returns whether Jinja2 template is used for generating the
        javascript module.
        """
        return False

    @property
    def node_inode(self):
        return False


# Register the module as a Blueprint
blueprint = OracleSchemaModule(__name__)


class OracleSchemaView(PGChildNodeView):
    node_type = blueprint.node_type

    parent_ids = [
        {'type': 'int', 'id': 'gid'},
        {'type': 'int', 'id': 'sid'}
    ]
    ids = [
        {'type': 'string', 'id': 'osc_id'}
    ]

    operations = dict({
        'obj': [
            {'get': 'properties'},
        ],
        'nodes': [{'get': 'node'}, {'get': 'nodes'}],
        'children': [{'get': 'children'}],
    })

    def check_precondition(f):
        """
        This function will behave as a decorator which will check the
        database connection before running the view.
        """

        @wraps(f)
        def wrap(*args, **kwargs):
            self = args[0]
            self.manager = get_driver(
                PG_DEFAULT_DRIVER
            ).connection_manager(
                kwargs['sid']
            )
            self.conn = self.manager.connection()

            if not self.conn.connected():
                current_app.logger.warning(
                    "Connection to the server has been lost."
                )
                from pgadmin.utils.ajax import precondition_required
                return precondition_required(
                    gettext(
                        "Connection to the server has been lost."
                    )
                )

            return f(*args, **kwargs)

        return wrap

    @check_precondition
    def node(self, gid, sid, osc_id):
        status, rset = self.conn.execute_dict(
            _USER_SQL, [osc_id])
        if not status:
            return internal_server_error(errormsg=rset)

        if len(rset['rows']) == 0:
            return gone(gettext("Could not find the schema."))

        row = rset['rows'][0]
        res = self.blueprint.generate_browser_node(
            row['username'],
            sid,
            row['username'],
            icon="icon-oracle_schema",
        )

        return make_json_response(data=res, status=200)

    @check_precondition
    def nodes(self, gid, sid, osc_id=None):
        res = []
        status, rset = self.conn.execute_dict(_LIST_USERS_SQL)
        if not status:
            return internal_server_error(errormsg=rset)

        for row in rset['rows']:
            res.append(
                self.blueprint.generate_browser_node(
                    row['username'],
                    sid,
                    row['username'],
                    icon="icon-oracle_schema",
                ))

        return make_json_response(data=res, status=200)

    @check_precondition
    def properties(self, gid, sid, osc_id):
        status, rset = self.conn.execute_dict(
            _USER_SQL, [osc_id])
        if not status:
            return internal_server_error(errormsg=rset)

        if len(rset['rows']) == 0:
            return gone(
                gettext("Could not find the schema information.")
            )

        return make_json_response(
            data=rset['rows'][0],
            status=200
        )


# Register the view with the blueprint
OracleSchemaView.register_node_view(blueprint)
