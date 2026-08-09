# SQL Setup Files for Lakebase

Run these against your Lakebase Postgres database before running the
`lakebase_embeddings` notebook.

## Setup Order

### 0. `00_grant_app_role.sql`
Grants the app's Postgres role (`massive_app`) `CREATE` on schema `public`.

Required because PostgreSQL 15+ no longer grants `CREATE` on `public` to every
role; without it, every `ensure_*_table()` call in `app.py` fails with
`permission denied for schema public`. Must be run by a role with superuser
rights — your own Databricks identity belongs to `databricks_superuser`. The app
role cannot grant this to itself.

**Skip it** if the role is already a member of `databricks_superuser`, which
confers `CREATE` on its own. Check with:

```sql
SELECT has_schema_privilege('massive_app', 'public', 'CREATE');
```

> ⚠️ **Do not run this in a notebook `%sql` cell.** Those execute against Unity
> Catalog, which reads `massive_app` as a Databricks principal instead of a
> Postgres role and fails with
> `ErrorClass=PRINCIPAL_DOES_NOT_EXIST ... Could not find principal with name massive_app`.
> The syntax is valid in both dialects, so Unity Catalog accepts it and only
> then fails to resolve the name. Run it against Lakebase Postgres instead —
> via the instance's own query editor, `psql`, or psycopg2 in a Python cell.
> See the header comment in the file for all three.

All the other files here are plain Postgres DDL and carry the same constraint:
they must reach the Lakebase database, not Unity Catalog.

### Run 01–03 as the app role, not as your own identity

The tables end up owned by whichever role creates them — normally `massive_app`,
since `app.py`'s `ensure_*_table()` helpers get there first. Re-running these
scripts from the Lakebase SQL Editor as your Databricks identity then fails:

```
ERROR: must be owner of table ticker_news_documents (SQLSTATE 42501)
```

That comes from the `CREATE INDEX IF NOT EXISTS` line. Postgres checks table
ownership *before* the `IF NOT EXISTS` short-circuit, so the statement is
rejected even when the index already exists. (The `CREATE TABLE IF NOT EXISTS`
above it does short-circuit first, which is why only part of the script errors.)

Note that `databricks_superuser` does **not** solve this — despite the name it
is not a Postgres superuser (`rolsuper = false`). It bundles `pg_read_all_data`,
`pg_write_all_data`, `pg_maintain` and `pg_monitor`, which allow reading and
writing every table but not owning or indexing one.

Options:

* **Nothing** — if the tables and indexes already exist, the error is cosmetic.
  Verify with `04_verify_embeddings.sql`.
* **Run as `massive_app`**, via psycopg2 in a notebook Python cell using the
  `database/lakebase-url` secret (the snippet in `00_grant_app_role.sql` shows
  the connection).
* **Take ownership**, as a role that can (`cloud_admin`):
  `ALTER TABLE ticker_news_documents OWNER TO "your-identity";`

### 1. `01_setup_news_table.sql`
Creates `ticker_news_documents`, the raw news article store, plus a ticker index.
(`app.py`'s `ensure_news_table()` creates the same table on demand, so this is
mainly for setting the schema up ahead of the notebook.)

### 2. `02_setup_embeddings_table.sql`
Enables pgvector and creates `ticker_news_embeddings` with an HNSW cosine index,
for title + description embeddings.

### 3. `03_setup_chunk_embeddings_table.sql`
Creates `ticker_news_chunk_embeddings` for chunks of the full article body, same
index strategy.

**Embedding dimension:** both files declare `VECTOR(384)`, matching the default
model `sentence-transformers/all-MiniLM-L6-v2`. If you change `embedding_model`
in the job config, change the dimension to match and recreate the tables. The
`MODEL_DIMS` map in `notebooks/lakebase_embeddings.py` is the source of truth,
and the notebook's preflight cell refuses to run on a mismatch:

| model | dim |
|---|---|
| `sentence-transformers/all-MiniLM-L6-v2` | 384 |
| `sentence-transformers/all-MiniLM-L12-v2` | 384 |
| `sentence-transformers/all-mpnet-base-v2` | 768 |
| `BAAI/bge-small-en-v1.5` | 384 |
| `BAAI/bge-base-en-v1.5` | 768 |
| `BAAI/bge-large-en-v1.5` | 1024 |

## After the notebook: `04_verify_embeddings.sql`

Row counts, column type/dimension check, index check, and a similarity-search
smoke test.

**No cast step is needed.** Earlier revisions of this file instructed you to run
`UPDATE ticker_news_embeddings SET embedding = embedding::vector`. That is a
no-op. `embeddings_pipeline.py` inserts pgvector's native literal with an
explicit `%s::vector` cast, so rows land as genuine vectors. (Even the older
`%s::double precision[]` form worked, because pgvector registers an *assignment*
cast from `double precision[]` to `vector`.) Verified on PostgreSQL 17.10 /
pgvector 0.8.0.

## Why Manual Setup?

The notebook connects with **psycopg2** and needs these tables and the `vector`
extension to already exist, because:

* `CREATE EXTENSION` and `CREATE INDEX` are one-time DDL that shouldn't run on
  every scheduled job execution.
* The HNSW indexes and `VECTOR` column types need to be right before the first
  bulk insert.

Running the setup SQL by hand gives you proper pgvector `VECTOR` columns, HNSW
indexes for fast similarity search, and idempotent notebook writes
(`ON CONFLICT (id) DO NOTHING`).
