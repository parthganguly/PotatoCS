from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from odysseus_desktop_backend.cancellation import check_cancelled


BRAVE_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
PROVIDER_RESPONSE_LIMIT_BYTES = 1024 * 1024
BRAVE_MIN_REQUEST_INTERVAL_SECONDS = 1.0
BRAVE_MAX_RETRIES = 1
BRAVE_MAX_RETRY_AFTER_SECONDS = 5.0


class SearchProviderError(RuntimeError):
    code = "search_provider_failed"


class SearchProviderConfigurationError(SearchProviderError):
    code = "search_provider_unconfigured"


@dataclass(frozen=True)
class ProviderSearchResult:
    url: str
    title: str
    snippet: str
    position: int
    provider: str
    query: str


class SearchProvider(Protocol):
    name: str

    def search(self, query: str, *, limit: int, timeout: float) -> list[ProviderSearchResult]: ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class BraveSearchProvider:
    """Minimal Brave Web Search adapter.

    The endpoint is fixed in software. Only the public search query and bounded
    result count leave the machine; local Sources and retrieved passages are
    never provided to this adapter.
    """

    name = "brave"

    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = BRAVE_SEARCH_ENDPOINT,
        opener: Any | None = None,
        clock: Any = time.monotonic,
        sleeper: Any = time.sleep,
        min_request_interval: float = BRAVE_MIN_REQUEST_INTERVAL_SECONDS,
        max_retries: int = BRAVE_MAX_RETRIES,
    ):
        clean_key = str(api_key or "").strip()
        if not clean_key:
            raise SearchProviderConfigurationError(
                "Set ODYSSEUS_BRAVE_SEARCH_API_KEY to enable web Search."
            )
        if endpoint != BRAVE_SEARCH_ENDPOINT:
            raise ValueError("the Brave Search endpoint is fixed")
        self._api_key = clean_key
        self._endpoint = endpoint
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )
        self._clock = clock
        self._sleeper = sleeper
        self._min_request_interval = max(0.0, float(min_request_interval))
        self._max_retries = max(0, min(int(max_retries), BRAVE_MAX_RETRIES))
        self._throttle_lock = threading.Lock()
        self._next_request_at = 0.0

    def search(self, query: str, *, limit: int, timeout: float) -> list[ProviderSearchResult]:
        clean_query = " ".join(str(query or "").split()).strip()
        if not clean_query:
            return []
        check_cancelled()
        count = max(1, min(int(limit), 20))
        url = f"{self._endpoint}?{urllib.parse.urlencode({'q': clean_query, 'count': count})}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": "PotatoCS-Search/0",
                "X-Subscription-Token": self._api_key,
            },
            method="GET",
        )
        retry_delay = 0.0
        raw = b""
        for attempt in range(self._max_retries + 1):
            self._reserve_request_slot(retry_delay)
            try:
                with self._opener.open(request, timeout=max(1.0, float(timeout))) as response:
                    content_type = str(response.headers.get("Content-Type") or "").lower()
                    if "application/json" not in content_type:
                        raise SearchProviderError("search provider returned a non-JSON response")
                    raw = response.read(PROVIDER_RESPONSE_LIMIT_BYTES + 1)
                break
            except (TimeoutError, socket.timeout) as exc:
                raise SearchProviderError("search provider request timed out") from exc
            except urllib.error.HTTPError as exc:
                if exc.code in {401, 403}:
                    raise SearchProviderConfigurationError("Brave Search rejected the configured API key") from exc
                if exc.code == 429 and attempt < self._max_retries:
                    retry_delay = retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None)
                    continue
                raise SearchProviderError(f"search provider returned HTTP {exc.code}") from exc
            except urllib.error.URLError as exc:
                raise SearchProviderError("search provider is unreachable") from exc
        check_cancelled()
        if len(raw) > PROVIDER_RESPONSE_LIMIT_BYTES:
            raise SearchProviderError("search provider response exceeded the size limit")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SearchProviderError("search provider returned malformed JSON") from exc
        web = payload.get("web") if isinstance(payload, dict) else None
        rows = web.get("results") if isinstance(web, dict) else None
        if not isinstance(rows, list):
            return []
        results: list[ProviderSearchResult] = []
        for position, row in enumerate(rows[:count], start=1):
            if not isinstance(row, dict):
                continue
            result_url = str(row.get("url") or "").strip()
            if not result_url:
                continue
            results.append(
                ProviderSearchResult(
                    url=result_url,
                    title=clean_provider_text(row.get("title"), max_chars=300),
                    snippet=clean_provider_text(row.get("description"), max_chars=1000),
                    position=position,
                    provider=self.name,
                    query=clean_query,
                )
            )
        return results

    def _reserve_request_slot(self, minimum_delay: float = 0.0) -> None:
        with self._throttle_lock:
            target = max(self._next_request_at, self._clock() + max(0.0, minimum_delay))
            while True:
                check_cancelled()
                remaining = target - self._clock()
                if remaining <= 0:
                    break
                self._sleeper(min(0.05, remaining))
            self._next_request_at = self._clock() + self._min_request_interval


class FixtureSearchProvider:
    """Deterministic provider used by tests and the offline proof harness."""

    name = "fixture"

    def __init__(self, results_by_query: dict[str, list[ProviderSearchResult]]):
        self.results_by_query = dict(results_by_query)
        self.queries: list[str] = []

    def search(self, query: str, *, limit: int, timeout: float) -> list[ProviderSearchResult]:
        del timeout
        check_cancelled()
        clean_query = " ".join(str(query or "").split()).strip()
        self.queries.append(clean_query)
        return list(self.results_by_query.get(clean_query, []))[: max(0, int(limit))]


def configured_search_provider(environ: dict[str, str] | None = None) -> SearchProvider:
    values = os.environ if environ is None else environ
    provider_name = str(values.get("ODYSSEUS_SEARCH_PROVIDER") or "brave").strip().lower()
    if provider_name != "brave":
        raise SearchProviderConfigurationError(f"Unsupported Search provider: {provider_name}")
    return BraveSearchProvider(str(values.get("ODYSSEUS_BRAVE_SEARCH_API_KEY") or ""))


def clean_provider_text(value: Any, *, max_chars: int) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split()).strip()[:max_chars]


def retry_after_seconds(value: Any) -> float:
    try:
        seconds = float(str(value or "").strip())
    except (TypeError, ValueError):
        seconds = BRAVE_MIN_REQUEST_INTERVAL_SECONDS
    return max(0.0, min(seconds, BRAVE_MAX_RETRY_AFTER_SECONDS))
