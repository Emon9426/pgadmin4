/////////////////////////////////////////////////////////////
//
// pgAdmin 4 - PostgreSQL Tools
//
// Copyright (C) 2013 - 2026, The pgAdmin Development Team
// This software is released under the PostgreSQL Licence
//
//////////////////////////////////////////////////////////////

define('pgadmin.node.oracle_schema', [
  'sources/gettext', 'sources/url_for',
  'pgadmin.browser', 'pgadmin.browser.collection',
], function(
  gettext, url_for, pgBrowser
) {

  if (!pgBrowser.Nodes['coll-oracle_schema']) {
    pgBrowser.Nodes['coll-oracle_schema'] =
      pgBrowser.Collection.extend({
        node: 'oracle_schema',
        label: gettext('Schemas'),
        type: 'coll-oracle_schema',
        columns: ['name'],
      });
  }

  if (!pgBrowser.Nodes['oracle_schema']) {
    pgBrowser.Nodes['oracle_schema'] = pgBrowser.Node.extend({
      parent_type: 'server',
      type: 'oracle_schema',
      label: gettext('Schema'),
      hasSQL: false,
      canDrop: false,
      Init: function() {
        /* Avoid multiple registration of menus */
        if (this.initialized)
          return;

        this.initialized = true;
      },
    });
  }

  return pgBrowser.Nodes['coll-oracle_schema'];
});
