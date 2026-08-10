"""
Client for the Massive API.

The API key is never stored in code or app.yaml. MASSIVE_API_KEY takes
precedence: on Databricks Apps a secret *resource* injects it (see the
`valueFrom` entry in app.yaml), locally it comes from .env. Failing that, the key
is read from a Databricks secret scope (see setup_secrets.py) via the SDK.
"""

import base64
import logging
import os
from functools import lru_cache
from typing import Any

import requests
from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)

_SCOPE = os.environ.get("MASSIVE_SECRET_SCOPE", "massive")
_KEY = os.environ.get("MASSIVE_SECRET_KEY", "api-key")
_BASE_URL = os.environ.get("MASSIVE_API_BASE_URL", "https://api.massive.com")

_DEFAULT_TIMEOUT = 30


@lru_cache(maxsize=1)
def _client() -> WorkspaceClient:
    """Build the Databricks client on first use, so importing this module
    doesn't require Databricks auth when MASSIVE_API_KEY is set locally."""
    return WorkspaceClient()


@lru_cache(maxsize=1)
def _get_api_key() -> str:
    """Return the Massive API key from the env var or the Databricks secret scope.

    Cached because MassiveClient.__init__ calls this, and the routes construct a
    fresh client per request - so the secret-scope branch would otherwise cost a
    Databricks API round trip on every /sync, /news/sync and POST /watchlist.

    Logs which source won, never the key itself.
    """
    key = os.environ.get("MASSIVE_API_KEY")
    if key:
        logger.info("Massive API key resolved from the MASSIVE_API_KEY environment variable")
        return key
    logger.info("Massive API key resolved from secret scope %s/%s", _SCOPE, _KEY)
    secret = _client().secrets.get_secret(scope=_SCOPE, key=_KEY)
    return base64.b64decode(secret.value).decode("utf-8")


class MassiveClient:
    """Thin wrapper around the Massive API with auth + retry-friendly session."""

    def __init__(self, base_url: str | None = None, timeout: int = _DEFAULT_TIMEOUT):
        self.base_url = (base_url or _BASE_URL).rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {_get_api_key()}",
                "Content-Type": "application/json",
            }
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self._session.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        resp = self._session.post(f"{self.base_url}{path}", json=json, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def paginated_get(self, path: str, params: dict[str, Any] | None = None, page_size: int = 200):
        """
        Generator that yields items across all pages of a "massive" (large)
        paginated dataset. Assumes a cursor-based API shape:
        {"items": [...], "next_cursor": "..." | null}
        Adjust to match the real Massive API pagination contract.
        """
        cursor = None
        params = dict(params or {})
        params["page_size"] = page_size

        while True:
            if cursor:
                params["cursor"] = cursor
            data = self.get(path, params=params)
            items = data.get("items", [])
            for item in items:
                yield item

            cursor = data.get("next_cursor")
            if not cursor:
                break

    def get_latest_price(self, symbol: str) -> dict:
        """
        Fetch the latest traded price for a single symbol in a SINGLE API
        call (no pagination). Use this instead of paginated_get() whenever
        the caller needs to stay within tight API rate limits (e.g.
        classroom/student accounts), at the cost of only being able to
        request one symbol per request.
        """
        data = self.get(f"/v2/aggs/ticker/{symbol}/prev")
        return data

    def get_news(
        self,
        ticker: str,
        limit: int = 50,
        published_utc_gte: str | None = None,
    ) -> list[dict]:
        """
        Fetch recent news articles for a single ticker in a SINGLE API call
        (GET /v2/reference/news). Returns just the "results" list - each
        item has: id, title, description, author, article_url, publisher,
        tickers, keywords, insights (sentiment), published_utc.

        published_utc_gte: optional ISO date/datetime string to only fetch
        articles published on/after this date (maps to the API's
        "published_utc.gte" filter).
        """
        params: dict[str, Any] = {
            "ticker": ticker,
            "limit": limit,
            "order": "desc",
            "sort": "published_utc",
        }
        if published_utc_gte:
            params["published_utc.gte"] = published_utc_gte

        data = self.get("/v2/reference/news", params=params)
        return data.get("results", [])
