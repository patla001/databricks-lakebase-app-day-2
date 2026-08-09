-- RUN IN THE LAKEBASE SQL EDITOR (from the database instance page) - NOT in a
-- workspace SQL editor or `%sql` cell, which target Unity Catalog and cannot
-- see these Postgres tables.
-- Setup script for ticker_news_chunk_embeddings table
-- Run this manually in your Lakebase Postgres database before running the notebook

-- Enable pgvector extension (if not already enabled)
CREATE EXTENSION IF NOT EXISTS vector;

-- Create the chunk embeddings table
-- VECTOR(384) must match the dimension used in 02_setup_embeddings_table.sql
-- and the model in the job config (all-MiniLM-L6-v2 -> 384):
--   - sentence-transformers/all-MiniLM-L6-v2: 384
--   - sentence-transformers/all-mpnet-base-v2: 768
--   - BAAI/bge-small-en-v1.5: 384
--   - BAAI/bge-base-en-v1.5: 768
--   - BAAI/bge-large-en-v1.5: 1024
CREATE TABLE IF NOT EXISTS ticker_news_chunk_embeddings (
    id TEXT PRIMARY KEY,
    article_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    chunk_index INT NOT NULL,
    chunk_text TEXT NOT NULL,
    embedding VECTOR(384) NOT NULL,
    model_name TEXT NOT NULL,
    embedded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Create HNSW index for fast cosine similarity search
CREATE INDEX IF NOT EXISTS idx_ticker_news_chunk_embeddings_embedding
ON ticker_news_chunk_embeddings
USING hnsw (embedding vector_cosine_ops);

-- Verify the table was created
SELECT 
    table_name,
    column_name,
    data_type,
    udt_name
FROM information_schema.columns
WHERE table_name = 'ticker_news_chunk_embeddings'
ORDER BY ordinal_position;