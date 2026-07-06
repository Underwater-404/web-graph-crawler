"""Playwright browser rendering and DOM extraction."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any
from urllib.parse import unquote, urlparse

from .config import CrawlerConfig
from .constants import COOKIE_TEXT_RE
from .links import LinkRecord, build_link_records
from .progress import NULL_REPORTER, Reporter
from .proxies import ProxyPool

LOGGER = logging.getLogger("web_graph_crawler")


class RenderedLinkCrawler:
    """Playwright-backed browser crawler."""

    def __init__(self, config: CrawlerConfig) -> None:
        self.config = config
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._proxy_pool = ProxyPool(list(config.proxies)) if config.proxies else None

    async def __aenter__(self) -> "RenderedLinkCrawler":
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Run: python -m pip install -r requirements.txt"
            ) from exc

        self._playwright = await async_playwright().start()
        browser_type = getattr(self._playwright, self.config.browser)

        launch_options: dict[str, Any] = {"headless": self.config.headless}
        if self._proxy_pool:
            # Chromium requires a launch-time proxy to enable per-context proxy
            # overrides; this sentinel turns on the per-page rotation below.
            launch_options["proxy"] = {"server": "per-context"}
        else:
            proxy_options = parse_proxy(self.config.proxy)
            if proxy_options:
                launch_options["proxy"] = proxy_options

        self._browser = await browser_type.launch(**launch_options)

        if not self._proxy_pool:
            # One reused context with persisted cookies. When rotating proxies we
            # build a fresh context per page instead (see crawl_once).
            self._context = await self._browser.new_context(**self._context_options(use_storage=True))
            self._context.set_default_timeout(self.config.timeout_ms)
        return self

    def _context_options(self, proxy: str | None = None, use_storage: bool = True) -> dict[str, Any]:
        options: dict[str, Any] = {
            "viewport": {"width": self.config.viewport[0], "height": self.config.viewport[1]},
            "locale": self.config.locale,
            "ignore_https_errors": self.config.ignore_https_errors,
        }
        if self.config.user_agent:
            options["user_agent"] = self.config.user_agent
        if self.config.timezone_id:
            options["timezone_id"] = self.config.timezone_id
        if use_storage and self.config.storage_state.exists():
            options["storage_state"] = str(self.config.storage_state)
        if proxy:
            options["proxy"] = parse_proxy(proxy)
        return options

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.save_storage_state()
        if self._context is not None:
            await self._context.close()
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()

    async def save_storage_state(self) -> None:
        if self._context is None:
            return
        self.config.storage_state.parent.mkdir(parents=True, exist_ok=True)
        await self._context.storage_state(path=str(self.config.storage_state))

    async def crawl_with_retries(
        self, source_url: str, reporter: Reporter = NULL_REPORTER
    ) -> list[LinkRecord]:
        last_error: Exception | None = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                return await self.crawl_once(source_url)
            except Exception as exc:
                last_error = exc
                if attempt >= self.config.max_retries:
                    break
                delay = self.config.retry_backoff * (2 ** (attempt - 1))
                delay += random.uniform(0.0, min(2.0, self.config.retry_backoff))
                LOGGER.warning(
                    "Attempt %d/%d failed for %s: %s; retrying in %.2fs",
                    attempt,
                    self.config.max_retries,
                    source_url,
                    exc,
                    delay,
                )
                reporter.page_retry(source_url, attempt, self.config.max_retries, delay)
                await asyncio.sleep(delay)
        raise RuntimeError(f"Failed after {self.config.max_retries} attempts: {last_error}")

    async def crawl_once(self, source_url: str) -> list[LinkRecord]:
        if self._proxy_pool:
            return await self._crawl_rotating(source_url)
        if self._context is None:
            raise RuntimeError("Browser context is not open")
        page = await self._context.new_page()
        try:
            return await self._scrape(page, source_url)
        finally:
            await self.save_storage_state()
            await page.close()

    async def _crawl_rotating(self, source_url: str) -> list[LinkRecord]:
        assert self._proxy_pool is not None
        proxy = self._proxy_pool.pick()
        context = await self._browser.new_context(
            **self._context_options(proxy=proxy, use_storage=False)
        )
        context.set_default_timeout(self.config.timeout_ms)
        page = await context.new_page()
        try:
            return await self._scrape(page, source_url)
        except Exception as exc:
            # Only retire the proxy on proxy-level failures, not slow targets.
            message = str(exc)
            if any(tag in message for tag in ("ERR_PROXY", "ERR_SOCKS", "ERR_TUNNEL")):
                self._proxy_pool.mark_dead(proxy)
                LOGGER.debug("proxy %s failed for %s; rotating", proxy, source_url)
            raise
        finally:
            await context.close()

    async def _scrape(self, page: Any, source_url: str) -> list[LinkRecord]:
        LOGGER.info("Opening %s", source_url)
        response = await page.goto(
            source_url,
            wait_until="domcontentloaded",
            timeout=self.config.timeout_ms,
        )

        status = response.status if response else None
        if status and status >= 500:
            raise RuntimeError(f"server returned HTTP {status}")
        if status and status >= 400:
            LOGGER.warning("%s returned HTTP %s", source_url, status)

        await self._wait_for_dynamic_content(page)
        await self._dismiss_cookie_banner(page)
        await self._scroll_for_lazy_content(page)
        await self._wait_for_dynamic_content(page, after_scroll=True)

        raw_links = await self._extract_raw_links(page)
        records = build_link_records(
            source_url=source_url,
            final_source_url=page.url,
            raw_links=raw_links,
            include_non_http=self.config.include_non_http,
            dedupe_links=self.config.dedupe_links,
        )
        LOGGER.info(
            "Success: %s -> %d link rows from final URL %s",
            source_url,
            len(records),
            page.url,
        )
        return records

    async def _wait_for_dynamic_content(self, page: Any, after_scroll: bool = False) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=self.config.network_idle_ms)
        except Exception:
            label = "after scroll" if after_scroll else "initial"
            LOGGER.debug("Network idle wait timed out during %s load", label)

        try:
            await page.wait_for_selector(
                "a[href], area[href], [role='link'], button[formaction], "
                "[data-href], [data-url], [data-link], [routerlink]",
                timeout=self.config.selector_wait_ms,
            )
        except Exception:
            LOGGER.debug("No common link selectors appeared before selector timeout")

        await asyncio.sleep(random.uniform(0.4, 1.2))

    async def _dismiss_cookie_banner(self, page: Any) -> None:
        """Click a visible cookie/privacy button when its label is unambiguous."""

        script = """
        (patternSource) => {
          const pattern = new RegExp(patternSource, "i");
          const candidates = Array.from(document.querySelectorAll(
            "button, input[type='button'], input[type='submit'], [role='button']"
          ));
          for (const element of candidates) {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            if (
              style.visibility === "hidden" ||
              style.display === "none" ||
              rect.width === 0 ||
              rect.height === 0
            ) {
              continue;
            }
            const text = (
              element.innerText ||
              element.value ||
              element.getAttribute("aria-label") ||
              element.getAttribute("title") ||
              ""
            ).trim().replace(/\\s+/g, " ");
            if (text && pattern.test(text)) {
              element.click();
              return text;
            }
          }
          return null;
        }
        """
        try:
            clicked_label = await page.evaluate(script, COOKIE_TEXT_RE.pattern)
            if clicked_label:
                LOGGER.info("Dismissed cookie/privacy prompt using button: %s", clicked_label)
                await asyncio.sleep(random.uniform(0.8, 1.8))
        except Exception as exc:
            LOGGER.debug("Cookie prompt dismissal skipped: %s", exc)

    async def _scroll_for_lazy_content(self, page: Any) -> None:
        stable_rounds = 0
        last_height = 0

        for round_no in range(1, self.config.max_scroll_rounds + 1):
            metrics = await page.evaluate(
                """() => ({
                    y: window.scrollY,
                    innerHeight: window.innerHeight,
                    height: Math.max(
                      document.body.scrollHeight,
                      document.documentElement.scrollHeight
                    )
                })"""
            )
            current_y = int(metrics["y"])
            viewport_height = max(1, int(metrics["innerHeight"]))
            page_height = max(1, int(metrics["height"]))

            if page_height == last_height and current_y + viewport_height >= page_height - 8:
                stable_rounds += 1
            else:
                stable_rounds = 0
            last_height = page_height

            if stable_rounds >= 2:
                LOGGER.debug("Scrolling complete after %d rounds", round_no)
                break

            remaining = max(0, page_height - viewport_height - current_y)
            if remaining <= 8:
                delta = max(250, int(viewport_height * 0.45))
            else:
                max_step = max(300, int(viewport_height * 0.9))
                min_step = max(180, int(viewport_height * 0.35))
                delta = min(remaining, random.randint(min_step, max_step))

            await page.mouse.wheel(0, delta)
            await asyncio.sleep(
                random.uniform(self.config.scroll_pause_min, self.config.scroll_pause_max)
            )

    async def _extract_raw_links(self, page: Any) -> list[dict[str, Any]]:
        script = """
        () => {
          const records = [];
          const seenElements = new Set();
          const attrNames = [
            "href",
            "formaction",
            "data-href",
            "data-url",
            "data-link",
            "data-target",
            "to",
            "routerlink",
            "ng-reflect-router-link"
          ];

          function textFor(el) {
            const imgAlt = el.tagName === "IMG" ? el.getAttribute("alt") : "";
            return (
              el.innerText ||
              el.getAttribute("aria-label") ||
              el.getAttribute("title") ||
              imgAlt ||
              ""
            ).trim().replace(/\\s+/g, " ").slice(0, 500);
          }

          function add(el, url, attr) {
            if (!url || typeof url !== "string") return;
            const trimmed = url.trim();
            if (!trimmed) return;
            const rect = el.getBoundingClientRect();
            records.push({
              url: trimmed,
              text: textFor(el),
              tag: el.tagName.toLowerCase(),
              attr,
              x: Math.round(rect.left + window.scrollX),
              y: Math.round(rect.top + window.scrollY),
              width: Math.round(rect.width),
              height: Math.round(rect.height)
            });
          }

          document.querySelectorAll("a[href], area[href]").forEach((el) => {
            seenElements.add(el);
            add(el, el.getAttribute("href"), "href");
          });

          document.querySelectorAll("button[formaction], input[formaction]").forEach((el) => {
            seenElements.add(el);
            add(el, el.getAttribute("formaction"), "formaction");
          });

          document.querySelectorAll("[role='link']").forEach((el) => {
            for (const attr of attrNames) {
              const value = el.getAttribute(attr);
              if (value) {
                seenElements.add(el);
                add(el, value, attr);
                return;
              }
            }
          });

          const attrSelector = attrNames
            .filter((attr) => attr !== "href" && attr !== "formaction")
            .map((attr) => `[${attr}]`)
            .join(",");

          document.querySelectorAll(attrSelector).forEach((el) => {
            if (seenElements.has(el)) return;
            for (const attr of attrNames) {
              const value = el.getAttribute(attr);
              if (value) {
                add(el, value, attr);
                return;
              }
            }
          });

          document.querySelectorAll("[onclick]").forEach((el) => {
            if (seenElements.has(el)) return;
            const onclick = el.getAttribute("onclick") || "";
            const match = onclick.match(
              /(?:window\\.open|location(?:\\.href|\\.assign)?|document\\.location)\\s*\\(?\\s*=*\\s*['"]([^'"]+)['"]/i
            );
            if (match && match[1]) {
              add(el, match[1], "onclick");
            }
          });

          return records;
        }
        """
        links = await page.evaluate(script)
        return links if isinstance(links, list) else []


def parse_proxy(proxy: str | None) -> dict[str, str] | None:
    if not proxy:
        return None

    candidate = proxy.strip()
    if "://" not in candidate:
        candidate = f"http://{candidate}"

    parsed = urlparse(candidate)
    if not parsed.hostname:
        raise ValueError(f"Invalid proxy URL: {proxy}")

    host = parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    server = f"{parsed.scheme}://{host}"

    options = {"server": server}
    if parsed.username:
        options["username"] = unquote(parsed.username)
    if parsed.password:
        options["password"] = unquote(parsed.password)
    return options
