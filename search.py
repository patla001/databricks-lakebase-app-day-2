"""
Semantic search over the ticker-news embeddings in Lakebase.

Query vectors MUST come from the same model that produced the stored vectors
(all-MiniLM-L6-v2, 384 dims) - a different model puts the query in a different
space and the cosine distances become meaningless.

That model is served here through `fastembed`, which runs the ONNX export on CPU,
rather than through `sentence-transformers`, which pulls in ~2.5GB of torch. The
two agree to cosine 0.99+ on the same input (measured against vectors this
project's notebook wrote), so rankings are equivalent while the web app stays
small enough to start quickly.
"""

from __future__ import annotations

import os
from functools import lru_cache

import lakebase
from embeddings_pipeline import to_vector_literal

EMBED_MODEL = os.environ.get(
    "SEARCH_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
)
EMBEDDINGS_TABLE = os.environ.get("EMBEDDINGS_TABLE_NAME", "ticker_news_embeddings")
CHUNK_TABLE = os.environ.get("CHUNK_EMBEDDINGS_TABLE_NAME", "ticker_news_chunk_embeddings")
NEWS_TABLE = os.environ.get("NEWS_TABLE_NAME", "ticker_news_documents")

# The model is downloaded on first use; point it somewhere writable. Databricks
# Apps and most containers only guarantee /tmp.
os.environ.setdefault("FASTEMBED_CACHE_PATH", "/tmp/.cache/fastembed")


@lru_cache(maxsize=1)
def _embedder():
    """Load the ONNX model once, on first search rather than at import.

    Keeps app startup fast and means an app that never searches never pays the
    download cost.
    """
    from fastembed import TextEmbedding

    return TextEmbedding(EMBED_MODEL)


def embed_query(text: str) -> list[float]:
    """Embed a search query into the same space as the stored vectors."""
    return list(_embedder().embed([text]))[0].tolist()


def search_articles(query: str, limit: int = 10, ticker: str | None = None) -> list[dict]:
    """Nearest articles by title+description embedding.

    Good for "which stories are about X". For passages inside an article, use
    search_chunks.
    """
    vec = to_vector_literal(embed_query(query))
    return lakebase.run_query(
        f"""
        SELECT e.id,
               e.ticker,
               e.title,
               d.article_url,
               d.publisher_name,
               d.sentiment,
               d.published_utc,
               ROUND((1 - (e.embedding <=> %(vec)s::vector))::numeric, 4) AS similarity
        FROM {EMBEDDINGS_TABLE} e
        JOIN {NEWS_TABLE} d ON d.id = e.id
        WHERE %(ticker)s::text IS NULL OR e.ticker = %(ticker)s
        ORDER BY e.embedding <=> %(vec)s::vector
        LIMIT %(limit)s
        """,
        {"vec": vec, "ticker": ticker, "limit": limit},
    )


def search_chunks(query: str, limit: int = 10, ticker: str | None = None) -> list[dict]:
    """Nearest passages from article bodies.

    Higher resolution than search_articles: it returns the specific span of text
    that matched, which is what a RAG prompt wants to quote.
    """
    vec = to_vector_literal(embed_query(query))
    return lakebase.run_query(
        f"""
        SELECT c.article_id,
               c.ticker,
               c.chunk_index,
               c.chunk_text,
               d.title,
               d.article_url,
               d.published_utc,
               ROUND((1 - (c.embedding <=> %(vec)s::vector))::numeric, 4) AS similarity
        FROM {CHUNK_TABLE} c
        JOIN {NEWS_TABLE} d ON d.id = c.article_id
        WHERE %(ticker)s::text IS NULL OR c.ticker = %(ticker)s
        ORDER BY c.embedding <=> %(vec)s::vector
        LIMIT %(limit)s
        """,
        {"vec": vec, "ticker": ticker, "limit": limit},
    )
