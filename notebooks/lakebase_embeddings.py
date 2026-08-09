# Databricks notebook source
# MAGIC %md
# MAGIC # Ticker News -> Lakebase Embeddings
# MAGIC
# MAGIC Fetches news for the tickers on the `watchlist` table, embeds them, and
# MAGIC writes vectors into Lakebase (pgvector).
# MAGIC
# MAGIC **Run this in a workspace notebook on a classic cluster.** Lakebase's own
# MAGIC page only offers a SQL editor; this needs Python. Being "outside" Lakebase
# MAGIC is not a problem - Lakebase is a normal Postgres endpoint reached over TLS.
# MAGIC
# MAGIC The stages live in `embeddings_pipeline.py` at the repo root, so they can be
# MAGIC tested outside Databricks. This notebook is only a driver.
# MAGIC
# MAGIC Prerequisites, all one-time:
# MAGIC - Secrets `database/lakebase-url` and `massive/api-key` stored
# MAGIC - `sql/01`-`03` applied (tables + pgvector + HNSW indexes)
# MAGIC - The Postgres role can create/write these tables

# COMMAND ----------

# MAGIC %pip install --quiet psycopg2-binary sentence-transformers trafilatura 'databricks-sdk>=0.30.0' sqlalchemy

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Parameters
dbutils.widgets.text("embedding_model", "sentence-transformers/all-MiniLM-L6-v2", "Embedding model")
dbutils.widgets.text("news_fetch_limit", "50", "Articles to fetch per ticker")
dbutils.widgets.text("max_requests_per_minute", "5", "Massive rate limit")
dbutils.widgets.text("chunk_size", "800", "Chunk size (chars)")
dbutils.widgets.text("chunk_overlap", "100", "Chunk overlap (chars)")
dbutils.widgets.text("skip_news_sync", "false", "Skip fetching news (embed existing rows only)")
dbutils.widgets.text("skip_chunks", "false", "Skip article-body chunk embeddings")

EMBEDDING_MODEL = dbutils.widgets.get("embedding_model")
NEWS_FETCH_LIMIT = int(dbutils.widgets.get("news_fetch_limit"))
MAX_RPM = int(dbutils.widgets.get("max_requests_per_minute"))
CHUNK_SIZE = int(dbutils.widgets.get("chunk_size"))
CHUNK_OVERLAP = int(dbutils.widgets.get("chunk_overlap"))
SKIP_NEWS = dbutils.widgets.get("skip_news_sync").lower() == "true"
SKIP_CHUNKS = dbutils.widgets.get("skip_chunks").lower() == "true"

# Dimension must match the VECTOR(n) column created by sql/02 and sql/03.
MODEL_DIMS = {
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "sentence-transformers/all-MiniLM-L12-v2": 384,
    "sentence-transformers/all-mpnet-base-v2": 768,
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "BAAI/bge-large-en-v1.5": 1024,
}
if EMBEDDING_MODEL not in MODEL_DIMS:
    raise ValueError(
        f"Unknown model {EMBEDDING_MODEL!r}. Add its dimension to MODEL_DIMS, and make "
        f"sure the VECTOR(n) columns in sql/02 and sql/03 match."
    )
EMBEDDING_DIM = MODEL_DIMS[EMBEDDING_MODEL]
print(f"{EMBEDDING_MODEL} -> {EMBEDDING_DIM}-dim vectors")

# COMMAND ----------

# DBTITLE 1,Import the pipeline module from the repo root
import os
import sys

# In a Databricks Git folder the notebook's own directory is the cwd; the shared
# modules (embeddings_pipeline, lakebase, massive_client) sit one level up.
for candidate in (os.getcwd(), os.path.dirname(os.getcwd())):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

try:
    import embeddings_pipeline as ep
except ModuleNotFoundError as err:
    raise ModuleNotFoundError(
        f"{err}\n\nCould not find embeddings_pipeline.py. This notebook must run from "
        f"a Git folder containing the whole repo, not as a standalone imported file.\n"
        f"cwd={os.getcwd()}\nsys.path[:3]={sys.path[:3]}"
    ) from err

print("pipeline module loaded from", ep.__file__)

# COMMAND ----------

# DBTITLE 1,Preflight - fail fast with a clear reason
# Everything downstream depends on these, so check them before loading a model.
import lakebase

info = lakebase.run_query("SELECT current_user AS usr, current_database() AS db, version() AS v")[0]
print(f"connected as {info['usr']} to {info['db']}")
print(f"  {info['v'].split(' on ')[0]}")

missing = [
    t for t in ("watchlist", "ticker_news_documents",
                "ticker_news_embeddings", "ticker_news_chunk_embeddings")
    if not lakebase.run_query(
        "SELECT to_regclass(%s) IS NOT NULL AS ok", (f"public.{t}",))[0]["ok"]
]
if missing:
    raise RuntimeError(f"Missing tables: {missing}. Apply sql/01-03 first.")

for table in ("ticker_news_embeddings", "ticker_news_chunk_embeddings"):
    dim = lakebase.run_query(f"""
        SELECT atttypmod AS dim FROM pg_attribute
        WHERE attrelid = '{table}'::regclass AND attname = 'embedding'
    """)[0]["dim"]
    if dim != EMBEDDING_DIM:
        raise RuntimeError(
            f"{table}.embedding is VECTOR({dim}) but {EMBEDDING_MODEL} produces "
            f"{EMBEDDING_DIM} dims. Recreate the table or change the model."
        )
    print(f"  {table}: VECTOR({dim}) OK")

for r in ep.summarize():
    print(f"  {r['table_name']:<32} {r['rows']} rows")

# COMMAND ----------

# DBTITLE 1,Stage 1 - sync news for watchlist tickers
tickers = ep.fetch_watchlist_tickers()
print("watchlist tickers:", tickers or "(empty - add symbols in the app first)")

if not tickers:
    raise RuntimeError("watchlist is empty; nothing to fetch. Add tickers via the app UI.")

if SKIP_NEWS:
    print("skip_news_sync=true, using existing rows")
else:
    n = ep.sync_news(tickers, limit=NEWS_FETCH_LIMIT, max_requests_per_minute=MAX_RPM)
    print(f"upserted {n} articles")

# COMMAND ----------

# DBTITLE 1,Stage 2 - load the embedding model
from sentence_transformers import SentenceTransformer

# Keep the HF cache off the driver's small root volume.
os.environ.setdefault("HF_HOME", "/tmp/.cache/huggingface")

model = SentenceTransformer(EMBEDDING_MODEL)

def embed(texts):
    return model.encode(list(texts), batch_size=32, show_progress_bar=False,
                        convert_to_numpy=True).tolist()

probe = embed(["dimension check"])[0]
assert len(probe) == EMBEDDING_DIM, f"model returned {len(probe)} dims, expected {EMBEDDING_DIM}"
print(f"model ready, {len(probe)} dims")

# COMMAND ----------

# DBTITLE 1,Stage 3 - embed title + description
pending = ep.pending_documents()
print(f"{len(pending)} documents without embeddings")

written = ep.embed_documents(embed, EMBEDDING_MODEL, pending)
print(f"wrote {written} embedding rows")

# COMMAND ----------

# DBTITLE 1,Stage 4 - fetch article bodies, chunk, embed
# Slower than stage 3: one HTTP request per article to the publisher. Articles
# that are paywalled, redirected or slow are skipped rather than failing the run.
if SKIP_CHUNKS:
    print("skip_chunks=true")
else:
    chunk_rows = ep.embed_article_chunks(
        embed, EMBEDDING_MODEL, pending,
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP,
    )
    print(f"wrote {chunk_rows} chunk rows")

# COMMAND ----------

# DBTITLE 1,Verify
for r in ep.summarize():
    print(f"  {r['table_name']:<32} {r['rows']} rows")

sample = lakebase.run_query("""
    SELECT pg_typeof(embedding)::text AS type, vector_dims(embedding) AS dims, model_name
    FROM ticker_news_embeddings LIMIT 1
""")
print("\nstored vector:", sample[0] if sample else "(no rows)")

print("\nnearest neighbours to the first article:")
for r in lakebase.run_query("""
    WITH probe AS (SELECT embedding FROM ticker_news_embeddings LIMIT 1)
    SELECT e.ticker, LEFT(e.title, 60) AS title,
           ROUND((1 - (e.embedding <=> probe.embedding))::numeric, 4) AS similarity
    FROM ticker_news_embeddings e, probe
    ORDER BY e.embedding <=> probe.embedding LIMIT 5
"""):
    print(f"  [{r['ticker']}] {r['title']}  sim={r['similarity']}")
