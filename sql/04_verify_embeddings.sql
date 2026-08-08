-- Post-notebook verification.
--
-- NOTE: no cast step is required. Earlier docs said to run
--     UPDATE ticker_news_embeddings SET embedding = embedding::vector ...
-- That is a no-op. pgvector registers an ASSIGNMENT cast from
-- double precision[] to vector, so the notebook's
--     INSERT ... VALUES (..., %s::double precision[], ...)
-- into a VECTOR(384) column already stores a real vector. Verified against
-- PostgreSQL 17.10 / pgvector 0.8.0: stored pg_typeof is `vector`,
-- vector_dims is 384, and the <=> cosine operator works.
--
-- Run these after the notebook to confirm the pipeline landed data.

-- 1. Row counts. embedded should equal total in both tables.
SELECT 'ticker_news_documents'        AS table_name, COUNT(*) AS total, NULL::bigint AS embedded
FROM ticker_news_documents
UNION ALL
SELECT 'ticker_news_embeddings',       COUNT(*), COUNT(embedding) FROM ticker_news_embeddings
UNION ALL
SELECT 'ticker_news_chunk_embeddings', COUNT(*), COUNT(embedding) FROM ticker_news_chunk_embeddings;

-- 2. Confirm the column really is a vector of the expected width.
SELECT 'ticker_news_embeddings' AS table_name,
       pg_typeof(embedding)::text AS column_type,
       vector_dims(embedding)     AS dims,
       model_name
FROM ticker_news_embeddings LIMIT 1;

-- 3. Confirm the HNSW indexes exist (similarity search will be slow without them).
SELECT tablename, indexname
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname LIKE '%embedding%'
ORDER BY tablename;

-- 4. Smoke-test similarity search: nearest neighbours to the first article.
WITH probe AS (SELECT embedding FROM ticker_news_embeddings LIMIT 1)
SELECT e.ticker,
       LEFT(e.title, 60) AS title,
       ROUND((1 - (e.embedding <=> probe.embedding))::numeric, 4) AS cosine_similarity
FROM ticker_news_embeddings e, probe
ORDER BY e.embedding <=> probe.embedding
LIMIT 5;
