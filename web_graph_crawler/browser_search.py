"""Keyless search discovery by driving a real Chromium (Playwright sync API).

The lightweight HTTP endpoints (DuckDuckGo html/lite) rate-limit and block
aggressively — a shared/NAT public IP gets throttled fast. Driving an actual
browser looks like a human session and survives those blocks far better, and it
can pull from **Bing**, a different service, so discovery still works when the IP
is throttled on DuckDuckGo.

Discovery runs *before* the async crawl starts, so Playwright's sync API is safe
here (there is no running event loop yet).
"""

from __future__ import annotations

import base64
import logging
from urllib.parse import parse_qs, urlencode, urlparse

from .proxies import ProxyPool
from .search_providers import SearchError, SearchProvider, _host, _is_http_url

LOGGER = logging.getLogger("web_graph_crawler.search")

BROWSER_ENGINES = ("bing", "duckduckgo", "mojeek")

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Result links pointing back at the engines/aggregators are never useful seeds.
_EXCLUDE_HOSTS = (
    "bing.com", "microsoft.com", "microsofttranslator.com", "msn.com", "go.microsoft.com",
    "duckduckgo.com", "google.com", "youtube.com", "mojeek.com",
)


def _excluded(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in _EXCLUDE_HOSTS)


def _unwrap_bing(url: str) -> str | None:
    """Bing wraps some results in a /ck/a?...&u=<base64> redirect; unwrap it."""
    parsed = urlparse(url)
    if "bing.com" not in (parsed.hostname or "") or not parsed.path.startswith("/ck/"):
        return url
    encoded = parse_qs(parsed.query).get("u", [None])[0]
    if not encoded:
        return None
    if encoded.startswith("a1"):
        encoded = encoded[2:]
    padding = "=" * (-len(encoded) % 4)
    try:
        decoded = base64.urlsafe_b64decode(encoded + padding).decode("utf-8", "replace")
    except Exception:
        return None
    return decoded if decoded.startswith("http") else None


class BrowserSearchProvider(SearchProvider):
    """Run searches through a headless Chromium and scrape organic result links."""

    name = "browser"

    def __init__(
        self,
        engine: str = "bing",
        *,
        headless: bool = True,
        proxies: list[str] | None = None,
        user_agent: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        engine = (engine or "bing").lower()
        if engine not in BROWSER_ENGINES:
            raise SearchError(
                f"Unknown browser engine {engine!r}. Choose from {', '.join(BROWSER_ENGINES)}"
            )
        # Intentionally does not call super().__init__: this provider needs no HttpClient.
        self.engine = engine
        self.headless = headless
        self.proxy_pool = ProxyPool(proxies) if proxies else None
        self.user_agent = user_agent or _UA
        self.timeout_ms = int(max(5.0, timeout) * 1000)

    def search(self, query: str, max_results: int) -> list[str]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise SearchError(
                "The browser provider needs Playwright. Run: pip install -e . && "
                "python -m playwright install chromium"
            ) from exc

        launch: dict = {"headless": self.headless}
        proxy = self._proxy_option()
        if proxy:
            launch["proxy"] = proxy
            LOGGER.debug("browser search via proxy %s", proxy.get("server"))

        with sync_playwright() as pw:
            browser = pw.chromium.launch(**launch)
            context = browser.new_context(user_agent=self.user_agent, locale="en-US")
            page = context.new_page()
            page.set_default_timeout(self.timeout_ms)
            try:
                if self.engine == "bing":
                    return self._bing(page, query, max_results)
                if self.engine == "mojeek":
                    return self._mojeek(page, query, max_results)
                return self._duckduckgo(page, query, max_results)
            finally:
                context.close()
                browser.close()

    def _proxy_option(self) -> dict | None:
        proxy = self.proxy_pool.pick() if self.proxy_pool else None
        if not proxy:
            return None
        from .browser import parse_proxy

        return parse_proxy(proxy)

    @staticmethod
    def _hrefs(page, selector: str) -> list[str]:
        out: list[str] = []
        for element in page.query_selector_all(selector):
            try:
                href = element.evaluate("e => e.href")
            except Exception:
                href = None
            if href:
                out.append(href)
        return out

    def _dismiss_consent(self, page) -> None:
        selectors = (
            "#bnp_btn_accept", "button#bnp_btn_accept", "#bnp_btn_accept a",
            "button:has-text('Accept')", "button:has-text('Accept all')",
            "button:has-text('I agree')", "button:has-text('Agree')",
        )
        for sel in selectors:
            try:
                element = page.query_selector(sel)
                if element and element.is_visible():
                    element.click()
                    page.wait_for_timeout(400)
                    return
            except Exception:
                continue

    def _add(self, raw_hrefs, seen, collected, max_results, unwrap=False) -> bool:
        for href in raw_hrefs:
            resolved = _unwrap_bing(href) if unwrap else href
            if not resolved or not _is_http_url(resolved) or resolved in seen:
                continue
            if _excluded(_host(resolved)):
                continue
            seen.add(resolved)
            collected.append(resolved)
            if len(collected) >= max_results:
                return True
        return False

    def _bing(self, page, query: str, max_results: int) -> list[str]:
        collected: list[str] = []
        seen: set[str] = set()
        for first in range(1, max(1, max_results) * 2, 10):
            params = {"q": query, "first": first, "mkt": "en-US", "setlang": "en"}
            url = "https://www.bing.com/search?" + urlencode(params)
            try:
                page.goto(url, wait_until="domcontentloaded")
            except Exception as exc:
                LOGGER.warning("Bing browser search failed for %r: %s", query, exc)
                break
            self._dismiss_consent(page)
            try:
                page.wait_for_selector("li.b_algo h2 a[href]", timeout=8000)
            except Exception:
                pass
            hrefs = self._hrefs(page, "li.b_algo h2 a[href], .b_algo h2 a[href]")
            LOGGER.debug("Bing page first=%d: %d raw link(s)", first, len(hrefs))
            done = self._add(hrefs, seen, collected, max_results, unwrap=True)
            if done or not hrefs:
                break
        return collected

    def _mojeek(self, page, query: str, max_results: int) -> list[str]:
        collected: list[str] = []
        seen: set[str] = set()
        for start in range(1, max(1, max_results) * 2, 10):
            params: dict = {"q": query}
            if start > 1:
                params["s"] = start  # Mojeek paginates via a 1-based start offset
            url = "https://www.mojeek.com/search?" + urlencode(params)
            try:
                page.goto(url, wait_until="domcontentloaded")
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Mojeek browser search failed for %r: %s", query, exc)
                break
            self._dismiss_consent(page)
            try:
                title = (page.title() or "").lower()
            except Exception:  # noqa: BLE001
                title = ""
            if "403" in title or "forbidden" in title:
                LOGGER.warning(
                    "Mojeek blocked the request (rate-limited automated queries). "
                    "Slow down, or use SearXNG's mojeek engine instead."
                )
                break
            try:
                page.wait_for_selector("ul.results-standard a[href], a.ob[href]", timeout=8000)
            except Exception:  # noqa: BLE001
                pass
            # Prefer the results container; fall back to any anchor (excludes filter noise).
            hrefs = self._hrefs(page, "ul.results-standard a[href], a.ob[href]")
            if not hrefs:
                hrefs = self._hrefs(page, "main a[href], a[href]")
            LOGGER.debug("Mojeek page start=%d: %d raw link(s)", start, len(hrefs))
            if self._add(hrefs, seen, collected, max_results):
                break
            if not hrefs:
                break
        return collected

    def _duckduckgo(self, page, query: str, max_results: int) -> list[str]:
        collected: list[str] = []
        seen: set[str] = set()
        url = "https://duckduckgo.com/?" + urlencode({"q": query, "ia": "web"})
        try:
            page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            LOGGER.warning("DuckDuckGo browser search failed for %r: %s", query, exc)
            return collected
        self._dismiss_consent(page)
        for _ in range(max(1, max_results // 10 + 1)):
            try:
                page.wait_for_selector("a[data-testid='result-title-a']", timeout=8000)
            except Exception:
                break
            hrefs = self._hrefs(page, "a[data-testid='result-title-a']")
            LOGGER.debug("DuckDuckGo browser page: %d raw link(s)", len(hrefs))
            if self._add(hrefs, seen, collected, max_results):
                break
            clicked = False
            for sel in ("#more-results", "button#more-results", "button:has-text('More results')"):
                more = page.query_selector(sel)
                if more and more.is_visible():
                    try:
                        more.click()
                        page.wait_for_timeout(1500)
                        clicked = True
                    except Exception:
                        clicked = False
                    break
            if not clicked:
                break
        return collected
