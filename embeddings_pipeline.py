"""
Ticker-news embeddings pipeline.

Plain-Python stages shared by the Databricks notebook
(notebooks/lakebase_embeddings.py) and by local runs. Keeping the logic here
rather than inline in the notebook means it can be exercised outside Databricks:
every stage takes its embedder as a callable, so tests can pass a stub instead
of loading a model.

Connections reuse lakebase.py, which resolves LAKEBASE_URL from the environment
when set and otherwise from the `database/lakebase-url` secret - so the same
code runs locally off .env and on a cluster off the secret scope.
"""

from __future__ import annotations

import hashlib
import time
from typing import Callable, Iterable, Sequence

from psycopg2.extras import execute_values

import lakebase
from massive_client import MassiveClient

# An embedder takes a list of strings and returns one vector per string.
Embedder = Callable[[Sequence[str]], Sequence[Sequence[float]]]

DEFAULT_NEWS_TABLE = "ticker_news_documents"
DEFAULT_EMBEDDINGS_TABLE = "ticker_news_embeddings"
DEFAULT_CHUNK_TABLE = "ticker_news_chunk_embeddings"
DEFAULT_WATCHLIST_TABLE = "watchlist"


def to_vector_literal(values: Sequence[float]) -> str:
    """Render a vector in pgvector's text form: '[0.1,0.2,...]'.

    Paired with an explicit ::vector cast at the insert site. pgvector also
    accepts a double precision[] via an assignment cast, but the native literal
    is unambiguous and avoids depending on that cast existing.
    """
    return "[" + ",".join(f"{float(v):.7g}" for v in values) + "]"


def fetch_watchlist_tickers(watchlist_table: str = DEFAULT_WATCHLIST_TABLE) -> list[str]:
    """Distinct symbols the app's users are tracking."""
    rows = lakebase.run_query(
        f"SELECT DISTINCT symbol FROM {watchlist_table} ORDER BY symbol"
    )
    return [r["symbol"] for r in rows]


def sync_news(
    tickers: Iterable[str],
    limit: int = 50,
    max_requests_per_minute: int = 5,
    news_table: str = DEFAULT_NEWS_TABLE,
    log: Callable[[str], None] = print,
) -> int:
    """Fetch recent articles per ticker from Massive and upsert them.

    Serialised and rate-limited: the free Massive tier allows only a handful of
    requests per minute, and one 429 fails the whole run.
    """
    client = MassiveClient()
    interval = 60.0 / max_requests_per_minute if max_requests_per_minute else 0.0
    total = 0

    for i, ticker in enumerate(tickers):
        if i and interval:
            time.sleep(interval)
        articles = client.get_news(ticker, limit=limit)
        total += _upsert_articles(ticker, articles, news_table)
        log(f"  {ticker}: fetched {len(articles)} articles")

    return total


def _upsert_articles(ticker: str, articles: list[dict], news_table: str) -> int:
    """Insert/refresh one ticker's articles. Mirrors app.py's news upsert."""
    import json

    if not articles:
        return 0

    rows = []
    for a in articles:
        sentiment = reasoning = None
        for insight in a.get("insights") or []:
            if insight.get("ticker") == ticker:
                sentiment = insight.get("sentiment")
                reasoning = insight.get("sentiment_reasoning")
                break
        rows.append((
            str(a.get("id")), ticker, a.get("title", ""), a.get("description"),
            a.get("author"), a.get("article_url"), (a.get("publisher") or {}).get("name"),
            json.dumps(a.get("keywords", [])), sentiment, reasoning,
            a.get("published_utc"), json.dumps(a),
        ))

    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            execute_values(cur, f"""
                INSERT INTO {news_table} (
                    id, ticker, title, description, author, article_url,
                    publisher_name, keywords, sentiment, sentiment_reasoning,
                    published_utc, payload, synced_at
                ) VALUES %s
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    description = EXCLUDED.description,
                    sentiment = EXCLUDED.sentiment,
                    payload = EXCLUDED.payload,
                    synced_at = EXCLUDED.synced_at
            """, rows, template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())")
        conn.commit()
    return len(rows)


def pending_documents(
    news_table: str = DEFAULT_NEWS_TABLE,
    embeddings_table: str = DEFAULT_EMBEDDINGS_TABLE,
) -> list[dict]:
    """Articles that have no title/description embedding yet.

    An anti-join rather than a full re-embed, so re-running the pipeline only
    does the new work.
    """
    return lakebase.run_query(f"""
        SELECT d.id, d.ticker, d.title, d.description, d.article_url, d.published_utc
        FROM {news_table} d
        LEFT JOIN {embeddings_table} e ON e.id = d.id
        WHERE e.id IS NULL
        ORDER BY d.published_utc DESC NULLS LAST
    """)


def embed_documents(
    embed: Embedder,
    model_name: str,
    documents: list[dict],
    embeddings_table: str = DEFAULT_EMBEDDINGS_TABLE,
    batch_size: int = 64,
    log: Callable[[str], None] = print,
) -> int:
    """Embed each article's title + description and store one vector per article."""
    if not documents:
        log("  nothing to embed")
        return 0

    texts = [
        " ".join(filter(None, [d.get("title"), d.get("description")])).strip()
        for d in documents
    ]
    written = 0

    for start in range(0, len(documents), batch_size):
        chunk_docs = documents[start:start + batch_size]
        vectors = embed(texts[start:start + batch_size])
        rows = [
            (d["id"], d["ticker"], d["title"], d["published_utc"],
             to_vector_literal(v), model_name)
            for d, v in zip(chunk_docs, vectors)
        ]
        with lakebase.get_connection() as conn:
            with conn.cursor() as cur:
                execute_values(cur, f"""
                    INSERT INTO {embeddings_table}
                        (id, ticker, title, published_utc, embedding, model_name, embedded_at)
                    VALUES %s
                    ON CONFLICT (id) DO NOTHING
                """, rows, template="(%s,%s,%s,%s,%s::vector,%s,now())")
                written += cur.rowcount
            conn.commit()
        log(f"  embedded {min(start + batch_size, len(documents))}/{len(documents)}")

    return written


def chunk_text(text: str, size: int = 800, overlap: int = 100) -> list[str]:
    """Split text into overlapping fixed-width chunks.

    Overlap keeps a sentence that straddles a boundary retrievable from both
    sides. Character-based rather than token-based: cheap, and close enough
    when the chunks are this large.
    """
    text = " ".join((text or "").split())
    if not text:
        return []
    if size <= overlap:
        raise ValueError(f"chunk size ({size}) must exceed overlap ({overlap})")

    chunks, start = [], 0
    while start < len(text):
        chunks.append(text[start:start + size])
        if start + size >= len(text):
            break
        start += size - overlap
    return chunks


def fetch_article_text(url: str, timeout: int = 20) -> str | None:
    """Download and extract an article body. Returns None on any failure.

    Publisher pages fail in many uninteresting ways - paywalls, redirects,
    timeouts - and none should abort a batch run.
    """
    if not url:
        return None
    try:
        import trafilatura
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        return trafilatura.extract(downloaded, include_comments=False, include_tables=False)
    except Exception:
        return None


def chunk_id(article_id: str, index: int) -> str:
    """Stable id for a chunk, so re-runs collide on ON CONFLICT instead of duplicating."""
    return hashlib.sha256(f"{article_id}:{index}".encode()).hexdigest()[:32]


def embed_article_chunks(
    embed: Embedder,
    model_name: str,
    documents: list[dict],
    chunk_size: int = 800,
    chunk_overlap: int = 100,
    chunk_table: str = DEFAULT_CHUNK_TABLE,
    fetch: Callable[[str], str | None] = fetch_article_text,
    log: Callable[[str], None] = print,
) -> int:
    """Fetch each article's body, chunk it, embed the chunks, and store them."""
    written = 0

    for i, doc in enumerate(documents, 1):
        body = fetch(doc.get("article_url"))
        if not body:
            log(f"  [{i}/{len(documents)}] {doc['ticker']}: no body extracted, skipped")
            continue

        chunks = chunk_text(body, chunk_size, chunk_overlap)
        if not chunks:
            continue

        vectors = embed(chunks)
        rows = [
            (chunk_id(doc["id"], idx), doc["id"], doc["ticker"], idx, chunk,
             to_vector_literal(vec), model_name)
            for idx, (chunk, vec) in enumerate(zip(chunks, vectors))
        ]
        with lakebase.get_connection() as conn:
            with conn.cursor() as cur:
                execute_values(cur, f"""
                    INSERT INTO {chunk_table}
                        (id, article_id, ticker, chunk_index, chunk_text,
                         embedding, model_name, embedded_at)
                    VALUES %s
                    ON CONFLICT (id) DO NOTHING
                """, rows, template="(%s,%s,%s,%s,%s,%s::vector,%s,now())")
                written += cur.rowcount
            conn.commit()
        log(f"  [{i}/{len(documents)}] {doc['ticker']}: {len(chunks)} chunks")

    return written


def summarize(
    news_table: str = DEFAULT_NEWS_TABLE,
    embeddings_table: str = DEFAULT_EMBEDDINGS_TABLE,
    chunk_table: str = DEFAULT_CHUNK_TABLE,
) -> list[dict]:
    """Row counts for the three pipeline tables."""
    return lakebase.run_query(f"""
        SELECT '{news_table}' AS table_name, COUNT(*) AS rows FROM {news_table}
        UNION ALL SELECT '{embeddings_table}', COUNT(*) FROM {embeddings_table}
        UNION ALL SELECT '{chunk_table}', COUNT(*) FROM {chunk_table}
    """)
