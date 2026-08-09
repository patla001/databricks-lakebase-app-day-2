# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "5"
# ///
# DBTITLE 1,Overview
# MAGIC %md
# MAGIC ## Lakebase Verification Queries
# MAGIC
# MAGIC This notebook runs the verification queries from `sql/04_verify_embeddings.sql` directly against the Lakebase Postgres instance.
# MAGIC
# MAGIC **Why?** `.sql` files in the Databricks editor execute on the SQL warehouse, which cannot connect to Lakebase Postgres. This notebook uses the Databricks SDK to generate an OAuth token and `psycopg` to run the queries natively against the Postgres endpoint.

# COMMAND ----------

# DBTITLE 1,Install psycopg and connect to Lakebase
# No additional packages needed:
# - psycopg2 is pre-installed on this compute
# - pandas is pre-installed
# - dbutils.secrets is built-in

# COMMAND ----------

# DBTITLE 1,Establish Lakebase connection
import psycopg2
import pandas as pd

# Connection via massive_app role (username + password from secrets)
LAKEBASE_URL = dbutils.secrets.get(scope="database", key="lakebase-url")

conn = psycopg2.connect(LAKEBASE_URL)
print(f"Connected to Lakebase as massive_app")

# COMMAND ----------

# DBTITLE 1,Query 1: Row counts
query1 = """
SELECT 'ticker_news_documents' AS table_name, COUNT(*) AS total, NULL::bigint AS embedded
FROM ticker_news_documents
UNION ALL
SELECT 'ticker_news_embeddings', COUNT(*), COUNT(embedding)
FROM ticker_news_embeddings
UNION ALL
SELECT 'ticker_news_chunk_embeddings', COUNT(*), COUNT(embedding)
FROM ticker_news_chunk_embeddings;
"""

df1 = pd.read_sql(query1, conn)
print("=== Row Counts ===")
display(df1)

# COMMAND ----------

# DBTITLE 1,Query 2: Vector type check
query2 = """
SELECT
    'ticker_news_embeddings' AS table_name,
    pg_typeof(embedding)::text AS column_type,
    vector_dims(embedding) AS dims,
    model_name
FROM ticker_news_embeddings
LIMIT 1;
"""

df2 = pd.read_sql(query2, conn)
print("=== Vector Type Check ===")
if df2.empty:
    print("No embeddings found yet — run the embedding notebook first.")
else:
    display(df2)

# COMMAND ----------

# DBTITLE 1,Query 3: HNSW index check
query3 = """
SELECT tablename, indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname LIKE '%embedding%'
ORDER BY tablename;
"""

df3 = pd.read_sql(query3, conn)
print("=== HNSW Index Check ===")
display(df3)

# COMMAND ----------

# DBTITLE 1,Query 4: Cosine similarity smoke test
query4 = """
SELECT
    d.ticker,
    d.title,
    (e.embedding <=> (SELECT embedding FROM ticker_news_embeddings LIMIT 1))::numeric(10,4) AS cosine_distance
FROM ticker_news_embeddings e
JOIN ticker_news_documents d ON d.id = e.id
ORDER BY cosine_distance
LIMIT 5;
"""

# Reconnect if the connection was dropped (idle timeout)
try:
    conn.cursor().execute("SELECT 1")
except Exception:
    conn = psycopg2.connect(dbutils.secrets.get(scope="database", key="lakebase-url"))

import warnings
with warnings.catch_warnings():
    warnings.simplefilter("ignore", UserWarning)
    df4 = pd.read_sql(query4, conn)
print("=== Cosine Similarity Smoke Test (Top 5 nearest) ===")
if df4.empty:
    print("No embeddings found yet — run the embedding notebook first.")
else:
    display(df4)

# COMMAND ----------

# DBTITLE 1,Close connection
conn.close()
print("Connection closed.")