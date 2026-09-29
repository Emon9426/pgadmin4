##########################################################################
#
# pgAdmin 4 - PostgreSQL Tools
#
# Copyright (C) 2013 - 2026, The pgAdmin Development Team
# This software is released under the PostgreSQL Licence
#
##########################################################################

from flask_babel import gettext
from pgadmin.browser.server_groups.servers.types import ServerType


class Oracle(ServerType):
    def instance_of(self, ver=None):
        # Oracle servers are never auto-detected from a PostgreSQL
        # version string; the type is chosen explicitly in the server
        # dialog and set by the oracle driver on connect.
        return False


# Oracle Database server type
Oracle('oracle', gettext("Oracle Database"), 1)
