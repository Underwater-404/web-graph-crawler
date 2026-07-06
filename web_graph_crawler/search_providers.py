"""Pluggable search backends that turn a dork/query into result URLs.

Every provider takes a single search string (which may contain search
operators such as ``site:``, ``inurl:``, ``intitle:``, or ``filetype:``) and
returns a list of absolute ``http(s)`` result URLs.

Only the Python standard library is used so the crawler keeps a single runtime
dependency (Playwright). Networking goes through :func:`urllib.request` and is
proxy aware, retries transient failures, and backs off on HTTP 429.

Providers
---------
``duckduckgo``  Keyless HTML endpoint. Works out of the box, best for light use.
``searxng``     JSON API of any SearXNG instance (``--searxng-url``). Best for
                heavy dorking when you run your own instance.
``brave``       Brave Search API (``--brave-api-key``). Free tier, real operators.
``google``      Google Programmable Search / Custom Search JSON API
                (``--google-api-key`` + ``--google-cx``).
``bing``        Legacy Bing Web Search API. Microsoft retired this service in
                August 2025; kept only for private/compatible endpoints.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import OpenerDirector, ProxyHandler, Request, build_opener

from .proxies import ProxyPool

LOGGER = logging.getLogger("web_graph_crawler.search")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

PROVIDER_NAMES = (
    "duckduckgo", "searxng", "brave", "google", "bing", "browser", "commoncrawl", "serper",
)


class SearchError(RuntimeError):
    """Raised when a provider is misconfigured (e.g. missing API key)."""


class HttpClient:
    """Proxy-aware HTTP helper with retry/backoff and proxy rotation.

    A :class:`~web_graph_crawler.proxies.ProxyPool` (if given) is consulted per
    attempt; a proxy that yields a connection error is marked dead and the next
    attempt rotates to another. Openers are cached per proxy.
    """

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 20.0,
        max_attempts: int = 3,
        backoff: float = 2.0,
        proxy_pool: "ProxyPool | None" = None,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.backoff = backoff
        self.proxy_pool = proxy_pool
        self._openers: dict[str | None, OpenerDirector] = {}

    def _opener_for(self, proxy: str | None) -> OpenerDirector:
        if proxy not in self._openers:
            self._openers[proxy] = _build_opener(proxy)
        return self._openers[proxy]

    def _request(self, method: str, url: str, *, headers: dict[str, str], data: bytes | None) -> bytes:
        merged = {"User-Agent": self.user_agent, "Accept-Language": "en-US,en;q=0.9"}
        merged.update(headers)
        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            proxy = self.proxy_pool.pick() if self.proxy_pool else None
            opener = self._opener_for(proxy)  # may raise SearchError (SOCKS w/o PySocks)
            request = Request(url, data=data, headers=merged, method=method)
            try:
                with opener.open(request, timeout=self.timeout) as response:
                    return response.read()
            except HTTPError as exc:
                last_error = exc
                # A response means the proxy worked; auth/quota won't fix on retry.
                if exc.code in {400, 401, 403}:
                    raise
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt >= self.max_attempts:
                    raise
                LOGGER.warning("HTTP %s from %s; retry %d/%d", exc.code, _host(url), attempt, self.max_attempts)
            except (URLError, TimeoutError) as exc:
                last_error = exc
                if self.proxy_pool and proxy:
                    self.proxy_pool.mark_dead(proxy)
                    LOGGER.debug("proxy %s failed (%s); rotating", proxy, exc)
                if attempt >= self.max_attempts:
                    raise
                LOGGER.warning("Request to %s failed (%s); retry %d/%d", _host(url), exc, attempt, self.max_attempts)

            time.sleep(self.backoff * (2 ** (attempt - 1)) + random.uniform(0.0, 1.0))

        raise RuntimeError(f"request failed: {last_error}")

    def get_json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        raw = self._request("GET", url, headers={"Accept": "application/json", **(headers or {})}, data=None)
        return json.loads(raw.decode("utf-8", errors="replace"))

    def get_text(self, url: str, headers: dict[str, str] | None = None) -> str:
        raw = self._request("GET", url, headers=headers or {}, data=None)
        return raw.decode("utf-8", errors="replace")

    def post_text(self, url: str, form: dict[str, str], headers: dict[str, str] | None = None) -> str:
        body = urlencode(form).encode("utf-8")
        merged = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
        raw = self._request("POST", url, headers=merged, data=body)
        return raw.decode("utf-8", errors="replace")

    def post_json(self, url: str, obj: Any, headers: dict[str, str] | None = None) -> Any:
        body = json.dumps(obj).encode("utf-8")
        merged = {"Content-Type": "application/json", "Accept": "application/json", **(headers or {})}
        raw = self._request("POST", url, headers=merged, data=body)
        return json.loads(raw.decode("utf-8", errors="replace"))


class SearchProvider:
    """Base class: subclasses implement :meth:`search`."""

    name = "base"

    def __init__(self, client: HttpClient) -> None:
        self.client = client

    def search(self, query: str, max_results: int) -> list[str]:  # pragma: no cover - interface
        raise NotImplementedError


class DuckDuckGoProvider(SearchProvider):
    """Keyless discovery via the DuckDuckGo HTML endpoint.

    DuckDuckGo honours ``site:``, ``inurl:``, ``intitle:``, and ``filetype:``.
    It rate-limits aggressive automated use, so keep query volume modest or
    switch to ``searxng``/``brave`` for larger sweeps.
    """

    name = "duckduckgo"
    ENDPOINT = "https://html.duckduckgo.com/html/"
    LITE_ENDPOINT = "https://lite.duckduckgo.com/lite/"
    PAGE_STEP = 30

    def search(self, query: str, max_results: int) -> list[str]:
        # The two keyless endpoints throttle independently, so if one returns an
        # empty (often rate-limited) page, the other frequently still answers.
        urls = self._search_endpoint(self.ENDPOINT, query, max_results)
        if not urls:
            LOGGER.info("DuckDuckGo html endpoint was empty for %r; trying lite endpoint", query)
            urls = self._search_endpoint(self.LITE_ENDPOINT, query, max_results)
        if not urls:
            LOGGER.warning(
                "DuckDuckGo returned no results for %r. This is usually temporary rate-limiting "
                "rather than zero matches — wait a minute, lower --results-per-dork, or switch "
                "--search-provider to searxng/brave/google.",
                query,
            )
        return urls

    def _search_endpoint(self, endpoint: str, query: str, max_results: int) -> list[str]:
        collected: list[str] = []
        seen: set[str] = set()

        for offset in range(0, max(1, max_results), self.PAGE_STEP):
            form = {"q": query, "kl": "us-en"}
            if offset:
                form["s"] = str(offset)
                form["dc"] = str(offset + 1)
            try:
                html = self.client.post_text(endpoint, form, headers={"Referer": endpoint})
            except Exception as exc:
                LOGGER.warning("DuckDuckGo request failed for %r: %s", query, exc)
                break

            page_urls = extract_duckduckgo_urls(html)
            fresh = [u for u in page_urls if u not in seen]
            for url in fresh:
                seen.add(url)
                collected.append(url)
                if len(collected) >= max_results:
                    return collected

            if not fresh:
                break

        return collected


class SearxngProvider(SearchProvider):
    """JSON search against a SearXNG instance base URL."""

    name = "searxng"

    def __init__(self, client: HttpClient, base_url: str) -> None:
        super().__init__(client)
        if not base_url:
            raise SearchError("SearXNG provider requires --searxng-url (e.g. https://searx.example)")
        self.base_url = base_url.rstrip("/")

    def search(self, query: str, max_results: int) -> list[str]:
        collected: list[str] = []
        seen: set[str] = set()

        for page in range(1, 11):
            params = {"q": query, "format": "json", "pageno": str(page), "safesearch": "0"}
            url = f"{self.base_url}/search?{urlencode(params)}"
            try:
                payload = self.client.get_json(url)
            except Exception as exc:
                LOGGER.warning("SearXNG request failed for %r: %s", query, exc)
                break

            results = payload.get("results", []) if isinstance(payload, dict) else []
            fresh = 0
            for item in results:
                candidate = item.get("url") if isinstance(item, dict) else None
                if _is_http_url(candidate) and candidate not in seen:
                    seen.add(candidate)
                    collected.append(candidate)
                    fresh += 1
                    if len(collected) >= max_results:
                        return collected

            if not fresh:
                break

        return collected


class BraveProvider(SearchProvider):
    """Brave Search API (https://brave.com/search/api/)."""

    name = "brave"
    ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
    PAGE_SIZE = 20

    def __init__(self, client: HttpClient, api_key: str) -> None:
        super().__init__(client)
        if not api_key:
            raise SearchError("Brave provider requires --brave-api-key or BRAVE_SEARCH_API_KEY")
        self.api_key = api_key

    def search(self, query: str, max_results: int) -> list[str]:
        collected: list[str] = []
        seen: set[str] = set()
        headers = {"X-Subscription-Token": self.api_key}

        for offset in range(0, 10):
            if len(collected) >= max_results:
                break
            count = min(self.PAGE_SIZE, max_results - len(collected))
            params = {"q": query, "count": str(count), "offset": str(offset)}
            url = f"{self.ENDPOINT}?{urlencode(params)}"
            try:
                payload = self.client.get_json(url, headers=headers)
            except HTTPError as exc:
                if exc.code in {401, 403}:
                    raise SearchError("Brave API rejected the key") from exc
                LOGGER.warning("Brave request failed for %r: %s", query, exc)
                break
            except Exception as exc:
                LOGGER.warning("Brave request failed for %r: %s", query, exc)
                break

            results = (payload.get("web", {}) or {}).get("results", []) if isinstance(payload, dict) else []
            fresh = 0
            for item in results:
                candidate = item.get("url") if isinstance(item, dict) else None
                if _is_http_url(candidate) and candidate not in seen:
                    seen.add(candidate)
                    collected.append(candidate)
                    fresh += 1
                    if len(collected) >= max_results:
                        return collected

            if not fresh:
                break

        return collected


class GoogleCseProvider(SearchProvider):
    """Google Programmable Search / Custom Search JSON API."""

    name = "google"
    ENDPOINT = "https://www.googleapis.com/customsearch/v1"

    def __init__(self, client: HttpClient, api_key: str, cx: str) -> None:
        super().__init__(client)
        if not api_key or not cx:
            raise SearchError("Google provider requires --google-api-key and --google-cx")
        self.api_key = api_key
        self.cx = cx

    def search(self, query: str, max_results: int) -> list[str]:
        collected: list[str] = []
        seen: set[str] = set()

        # The API returns at most 10 per call and 100 overall (start is 1-based).
        for start in range(1, min(max_results, 100) + 1, 10):
            num = min(10, max_results - len(collected))
            params = {"key": self.api_key, "cx": self.cx, "q": query, "num": str(num), "start": str(start)}
            url = f"{self.ENDPOINT}?{urlencode(params)}"
            try:
                payload = self.client.get_json(url)
            except HTTPError as exc:
                if exc.code in {400, 403}:
                    raise SearchError("Google API rejected the key/cx or exceeded quota") from exc
                LOGGER.warning("Google request failed for %r: %s", query, exc)
                break
            except Exception as exc:
                LOGGER.warning("Google request failed for %r: %s", query, exc)
                break

            items = payload.get("items", []) if isinstance(payload, dict) else []
            fresh = 0
            for item in items:
                candidate = item.get("link") if isinstance(item, dict) else None
                if _is_http_url(candidate) and candidate not in seen:
                    seen.add(candidate)
                    collected.append(candidate)
                    fresh += 1
                    if len(collected) >= max_results:
                        return collected

            if not fresh:
                break

        return collected


class BingProvider(SearchProvider):
    """Legacy Bing Web Search API (retired by Microsoft in August 2025)."""

    name = "bing"
    DEFAULT_ENDPOINT = "https://api.bing.microsoft.com/v7.0/search"
    PAGE_SIZE = 50

    def __init__(self, client: HttpClient, api_key: str, endpoint: str | None = None) -> None:
        super().__init__(client)
        if not api_key:
            raise SearchError("Bing provider requires --bing-api-key or BING_SEARCH_API_KEY")
        self.api_key = api_key
        self.endpoint = endpoint or self.DEFAULT_ENDPOINT
        LOGGER.warning(
            "The public Bing Web Search API was retired in August 2025; the 'bing' "
            "provider only works against a private or compatible endpoint."
        )

    def search(self, query: str, max_results: int) -> list[str]:
        collected: list[str] = []
        seen: set[str] = set()
        headers = {"Ocp-Apim-Subscription-Key": self.api_key}

        for offset in range(0, max(1, max_results), self.PAGE_SIZE):
            count = min(self.PAGE_SIZE, max_results - offset)
            params = {
                "q": query,
                "count": str(count),
                "offset": str(offset),
                "responseFilter": "Webpages",
                "textDecorations": "false",
                "textFormat": "Raw",
            }
            url = f"{self.endpoint}?{urlencode(params)}"
            try:
                payload = self.client.get_json(url, headers=headers)
            except HTTPError as exc:
                if exc.code in {401, 403}:
                    raise SearchError("Bing API rejected the key or endpoint access") from exc
                LOGGER.warning("Bing request failed for %r: %s", query, exc)
                break
            except Exception as exc:
                LOGGER.warning("Bing request failed for %r: %s", query, exc)
                break

            values = (payload.get("webPages", {}) or {}).get("value", []) if isinstance(payload, dict) else []
            fresh = 0
            for item in values:
                candidate = item.get("url") if isinstance(item, dict) else None
                if _is_http_url(candidate) and candidate not in seen:
                    seen.add(candidate)
                    collected.append(candidate)
                    fresh += 1
                    if len(collected) >= max_results:
                        return collected

            if not fresh:
                break

        return collected


class SerperProvider(SearchProvider):
    """Google results via Serper.dev — a SERP API that scrapes Google for you.

    You get Google's actual results (full operator support: site:/inurl:/
    filetype:/intitle:) through a clean JSON API, so your IP never touches Google
    and never gets captcha'd/banned. Free tier ~2,500 queries (https://serper.dev).
    """

    name = "serper"
    ENDPOINT = "https://google.serper.dev/search"

    def __init__(self, client: HttpClient, api_key: str) -> None:
        super().__init__(client)
        if not api_key:
            raise SearchError(
                "serper provider requires --serper-api-key or SERPER_API_KEY "
                "(free key at https://serper.dev)"
            )
        self.api_key = api_key

    # Serper's free tier caps `num` at 10 (num>10 => HTTP 400 "not allowed for
    # free accounts"); pagination via `page` is allowed, so we page in 10s.
    PAGE_SIZE = 10
    MAX_PAGES = 20

    def search(self, query: str, max_results: int) -> list[str]:
        collected: list[str] = []
        seen: set[str] = set()
        headers = {"X-API-KEY": self.api_key}
        per_page = min(self.PAGE_SIZE, max(1, max_results))

        for page in range(1, self.MAX_PAGES + 1):
            if len(collected) >= max_results:
                break
            body = {"q": query, "num": per_page, "page": page}
            try:
                payload = self.client.post_json(self.ENDPOINT, body, headers=headers)
            except HTTPError as exc:
                if exc.code in {401, 403}:
                    raise SearchError("Serper API rejected the key") from exc
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:200]
                except Exception:  # noqa: BLE001
                    pass
                LOGGER.warning("Serper request failed for %r: HTTP %s %s", query, exc.code, detail)
                break
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Serper request failed for %r: %s", query, exc)
                break

            organic = payload.get("organic", []) if isinstance(payload, dict) else []
            fresh = 0
            for item in organic:
                link = item.get("link") if isinstance(item, dict) else None
                if _is_http_url(link) and link not in seen:
                    seen.add(link)
                    collected.append(link)
                    fresh += 1
                    if len(collected) >= max_results:
                        return collected

            if not fresh:
                break

        return collected


@dataclass(frozen=True)
class ProviderSettings:
    """Everything needed to construct any provider."""

    name: str = "duckduckgo"
    api_key: str | None = None
    endpoint: str | None = None
    searxng_url: str | None = None
    google_cx: str | None = None
    browser_engine: str = "bing"
    cc_index: str | None = None
    cc_max_records: int = 10000
    proxy: str | None = None
    proxies: tuple[str, ...] = ()
    user_agent: str = DEFAULT_USER_AGENT
    timeout: float = 20.0


def build_http_client(settings: ProviderSettings) -> HttpClient:
    proxies = list(settings.proxies) or ([settings.proxy] if settings.proxy else [])
    pool = ProxyPool(proxies) if proxies else None
    return HttpClient(
        user_agent=settings.user_agent, timeout=settings.timeout, proxy_pool=pool
    )


def create_search_provider(settings: ProviderSettings) -> SearchProvider:
    name = (settings.name or "duckduckgo").lower()
    if name not in PROVIDER_NAMES:
        raise SearchError(f"Unknown search provider: {settings.name!r}. Choose from {', '.join(PROVIDER_NAMES)}")

    client = build_http_client(settings)
    if name == "duckduckgo":
        return DuckDuckGoProvider(client)
    if name == "searxng":
        return SearxngProvider(client, settings.searxng_url or "")
    if name == "brave":
        return BraveProvider(client, settings.api_key or "")
    if name == "google":
        return GoogleCseProvider(client, settings.api_key or "", settings.google_cx or "")
    if name == "bing":
        return BingProvider(client, settings.api_key or "", settings.endpoint)
    if name == "serper":
        return SerperProvider(client, settings.api_key or "")
    if name == "browser":
        # Lazy import keeps Playwright optional for the HTTP-only providers.
        from .browser_search import BrowserSearchProvider

        return BrowserSearchProvider(
            engine=settings.browser_engine,
            headless=True,
            proxies=list(settings.proxies) or ([settings.proxy] if settings.proxy else []),
            user_agent=None,  # use a real desktop UA, not the stdlib client UA
            timeout=max(30.0, settings.timeout),
        )
    if name == "commoncrawl":
        from .commoncrawl import CommonCrawlProvider

        return CommonCrawlProvider(
            client, index_id=settings.cc_index, max_index_records=settings.cc_max_records
        )
    raise SearchError(f"Unsupported provider: {name}")  # pragma: no cover


# Anchor href scanner. Kept permissive so it survives DuckDuckGo markup tweaks;
# extract_duckduckgo_urls filters the matches down to organic result links.
_HREF_RE = re.compile(r'href="([^"]+)"', re.IGNORECASE)


def extract_duckduckgo_urls(html: str) -> list[str]:
    """Parse organic result URLs out of a DuckDuckGo HTML results page."""

    urls: list[str] = []
    seen: set[str] = set()
    # Organic results are wrapped in a /l/?uddg=<encoded> redirect; ads use y.js.
    for match in _HREF_RE.finditer(html):
        decoded = _decode_ddg_href(match.group(1))
        if decoded and decoded not in seen and _is_http_url(decoded):
            host = _host(decoded)
            if host.endswith("duckduckgo.com"):
                continue
            seen.add(decoded)
            urls.append(decoded)
    return urls


def _decode_ddg_href(href: str) -> str | None:
    href = href.replace("&amp;", "&").strip()
    if not href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "uddg=" in (parsed.query or ""):
        target = parse_qs(parsed.query).get("uddg", [None])[0]
        return target or None
    if parsed.scheme in {"http", "https"}:
        return href
    return None


def _is_http_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _build_opener(proxy: str | None) -> OpenerDirector:
    if not proxy:
        return build_opener()
    parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    scheme = parsed.scheme.lower()
    if scheme.startswith("socks"):
        try:
            import socks  # PySocks
            from sockshandler import SocksiPyHandler
        except ImportError as exc:
            raise SearchError(
                "SOCKS proxy support needs PySocks. Install it with: "
                "pip install PySocks   (or: pip install -e '.[socks]')"
            ) from exc
        socks_type = socks.SOCKS4 if scheme.startswith("socks4") else socks.SOCKS5
        return build_opener(
            SocksiPyHandler(
                socks_type,
                parsed.hostname,
                parsed.port or 1080,
                rdns=True,  # resolve DNS through the proxy
                username=parsed.username,
                password=parsed.password,
            )
        )
    return build_opener(ProxyHandler({"http": proxy, "https": proxy}))
