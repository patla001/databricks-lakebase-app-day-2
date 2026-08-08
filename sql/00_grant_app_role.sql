-- Grant the app's Postgres role permission to create tables.
--
-- Run this ONCE, before anything else, as a role with superuser rights - i.e.
-- your own Databricks identity (which belongs to `databricks_superuser`), via
-- the Databricks SQL editor connected to your Lakebase instance. The app role
-- itself cannot run this.
--
-- Why it's needed: as of PostgreSQL 15, the `public` schema no longer grants
-- CREATE to every role. A freshly created Lakebase password role therefore gets
-- USAGE but not CREATE, and app.py's ensure_table() / ensure_watchlist_table() /
-- ensure_news_table() fail with "permission denied for schema public".
--
-- Replace massive_app if you named your role something else.

GRANT CREATE ON SCHEMA public TO massive_app;

-- Verify (expect: t, t)
SELECT has_schema_privilege('massive_app', 'public', 'USAGE')  AS has_usage,
       has_schema_privilege('massive_app', 'public', 'CREATE') AS has_create;
