# SQL Setup Files for Lakebase

Run these against your Lakebase Postgres database before running the
`ingest_ticker_news_embeddings` notebook.

## Setup Order

### 0. `00_grant_app_role.sql`
Grants the app's Postgres role (`massive_app`) `CREATE` on schema `public`.

Must be run by a role with superuser rights — your own Databricks identity, which
belongs to `databricks_superuser`. The app role cannot grant this to itself.
Required because PostgreSQL 15+ no longer grants `CREATE` on `public` to every
role; without it, every `ensure_*_table()` call in `app.py` fails with
`permission denied for schema public`.

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
notebook's `match`/`case` block is the source of truth:

| model | dim |
|---|---|
| `sentence-transformers/all-MiniLM-L6-v2` | 384 |
| `sentence-transformers/all-mpnet-base-v2` | 768 |
| `BAAI/bge-small-en-v1.5` | 384 |
| `BAAI/bge-base-en-v1.5` | 768 |
| `BAAI/bge-large-en-v1.5` | 1024 |
| `text-embedding-3-small` | 1536 |
| `text-embedding-3-large` | 3072 |

## After the notebook: `04_verify_embeddings.sql`

Row counts, column type/dimension check, index check, and a similarity-search
smoke test.

**No cast step is needed.** Earlier revisions of this file instructed you to run
`UPDATE ticker_news_embeddings SET embedding = embedding::vector`. That is a
no-op: pgvector registers an *assignment* cast from `double precision[]` to
`vector`, so the notebook's `INSERT ... %s::double precision[]` into a
`VECTOR(384)` column already stores a genuine vector. Verified on PostgreSQL
17.10 / pgvector 0.8.0.

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
