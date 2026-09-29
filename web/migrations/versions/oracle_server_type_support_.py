##########################################################################
#
# pgAdmin 4 - PostgreSQL Tools
#
# Copyright (C) 2013 - 2026, The pgAdmin Development Team
# This software is released under the PostgreSQL Licence
#
##########################################################################

"""Oracle server type support

Adds the server_type column to the server and sharedserver tables so a
server can be marked as an Oracle connection (driver key 'oracle').
Existing rows keep NULL which preserves the historical auto-detect
behaviour for PostgreSQL/EDB Advanced Server.

Revision ID: oracle_server_type_support
Revises: normalize_locked_text_default
Create Date: 2026-09-29

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'oracle_server_type_support'
down_revision = 'normalize_locked_text_default'
branch_labels = None
depends_on = None


def _add_column_if_missing(table, inspector):
    existing_cols = {c['name'] for c in inspector.get_columns(table)}
    if 'server_type' not in existing_cols:
        op.add_column(
            table,
            sa.Column('server_type', sa.String(length=32), nullable=True)
        )


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    for table in ('server', 'sharedserver'):
        if inspector.has_table(table):
            _add_column_if_missing(table, inspector)


def downgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    for table in ('server', 'sharedserver'):
        if inspector.has_table(table):
            existing_cols = {
                c['name'] for c in inspector.get_columns(table)
            }
            if 'server_type' in existing_cols:
                op.drop_column(table, 'server_type')
