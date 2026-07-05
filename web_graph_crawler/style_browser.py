"""Playwright collection of computed CSS styles for rendered links."""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .browser import parse_proxy
from .constants import COOKIE_TEXT_RE
from .links import classify_link, normalize_link_url
from .style_records import LinkStyleRecord

LOGGER = logging.getLogger("web_graph_crawler.styles")

CAPTCHA_PATTERNS = (
    "captcha",
    "verify you are human",
    "unusual traffic",
    "access denied",
    "checking your browser",
)

DESKTOP_VIEWPORTS = [(1366, 900), (1440, 900), (1536, 864), (1600, 900), (1920, 1080)]
MOBILE_VIEWPORTS = [(390, 844), (412, 915), (430, 932)]
TIMEZONE_POOL = ["UTC", "America/New_York", "America/Chicago", "Europe/London", "Europe/Paris"]
USER_AGENT_POOL = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
        "Gecko/20100101 Firefox/125.0"
    ),
    (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 "
        "Mobile/15E148 Safari/604.1"
    ),
]


@dataclass(frozen=True)
class StyleBrowserConfig:
    browser: str
    headless: bool
    storage_state: Path
    timeout_ms: int
    network_idle_ms: int
    selector_wait_ms: int
    max_scroll_rounds: int
    scroll_pause_min: float
    scroll_pause_max: float
    max_retries: int
    retry_backoff: float
    proxy: str | None
    user_agent: str | None
    viewport: tuple[int, int]
    timezone_id: str | None
    locale: str
    ignore_https_errors: bool


class LinkStyleCollector:
    """Playwright-backed collector for computed styles of rendered anchors."""

    def __init__(self, config: StyleBrowserConfig) -> None:
        self.config = config
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None

    async def __aenter__(self) -> "LinkStyleCollector":
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Run: python -m pip install -r requirements.txt"
            ) from exc

        self._playwright = await async_playwright().start()
        browser_type = getattr(self._playwright, self.config.browser)

        launch_options: dict[str, Any] = {"headless": self.config.headless}
        proxy_options = parse_proxy(self.config.proxy)
        if proxy_options:
            launch_options["proxy"] = proxy_options

        self._browser = await browser_type.launch(**launch_options)

        context_options: dict[str, Any] = {
            "viewport": {
                "width": self.config.viewport[0],
                "height": self.config.viewport[1],
            },
            "locale": self.config.locale,
            "ignore_https_errors": self.config.ignore_https_errors,
        }
        if self.config.user_agent:
            context_options["user_agent"] = self.config.user_agent
        if self.config.timezone_id:
            context_options["timezone_id"] = self.config.timezone_id
        if self.config.storage_state.exists():
            context_options["storage_state"] = str(self.config.storage_state)

        self._context = await self._browser.new_context(**context_options)
        self._context.set_default_timeout(self.config.timeout_ms)
        return self

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

    async def collect_with_retries(self, source_url: str) -> list[LinkStyleRecord]:
        last_error: Exception | None = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                return await self.collect_once(source_url)
            except RuntimeError as exc:
                if "HTTP 429" in str(exc) or "captcha" in str(exc).lower():
                    LOGGER.warning("Skipping %s: %s", source_url, exc)
                    return []
                last_error = exc
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
                last_error,
                delay,
            )
            await asyncio.sleep(delay)

        raise RuntimeError(f"Failed after {self.config.max_retries} attempts: {last_error}")

    async def collect_once(self, source_url: str) -> list[LinkStyleRecord]:
        if self._context is None:
            raise RuntimeError("Browser context is not open")

        page = await self._context.new_page()
        try:
            LOGGER.info("Opening %s", source_url)
            response = await page.goto(
                source_url,
                wait_until="domcontentloaded",
                timeout=self.config.timeout_ms,
            )

            status = response.status if response else None
            if status == 429:
                raise RuntimeError("HTTP 429 rate limited")
            if status and status >= 500:
                raise RuntimeError(f"server returned HTTP {status}")
            if status and status >= 400:
                LOGGER.warning("%s returned HTTP %s", source_url, status)

            await self._wait_for_links(page)
            await self._dismiss_cookie_banner(page)
            await self._scroll_for_lazy_content(page)
            await self._wait_for_links(page, after_scroll=True)
            await self._fail_if_captcha_like(page)

            raw_links = await self._extract_raw_link_styles(page)
            records = self._build_records(source_url, page.url, raw_links)
            LOGGER.info("Collected %d styled link row(s) from %s", len(records), source_url)
            return records
        finally:
            await self.save_storage_state()
            await page.close()

    async def _wait_for_links(self, page: Any, after_scroll: bool = False) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=self.config.network_idle_ms)
        except Exception:
            label = "after scroll" if after_scroll else "initial"
            LOGGER.debug("Network idle wait timed out during %s load", label)

        try:
            await page.wait_for_selector("a[href]", timeout=self.config.selector_wait_ms)
        except Exception:
            LOGGER.debug("No anchors appeared before selector timeout")

        await asyncio.sleep(random.uniform(0.3, 1.0))

    async def _dismiss_cookie_banner(self, page: Any) -> None:
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
                await asyncio.sleep(random.uniform(0.6, 1.4))
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

    async def _fail_if_captcha_like(self, page: Any) -> None:
        text = await page.evaluate(
            """() => [
                document.title || "",
                document.body ? document.body.innerText.slice(0, 2000) : ""
            ].join("\\n").toLowerCase()"""
        )
        if any(pattern in text for pattern in CAPTCHA_PATTERNS):
            raise RuntimeError("captcha or access-check page detected")

    async def _extract_raw_link_styles(self, page: Any) -> list[dict[str, Any]]:
        script = """
        () => {
          function borderFor(style) {
            const top = `${style.borderTopWidth} ${style.borderTopStyle} ${style.borderTopColor}`;
            const right = `${style.borderRightWidth} ${style.borderRightStyle} ${style.borderRightColor}`;
            const bottom = `${style.borderBottomWidth} ${style.borderBottomStyle} ${style.borderBottomColor}`;
            const left = `${style.borderLeftWidth} ${style.borderLeftStyle} ${style.borderLeftColor}`;
            if (top === right && right === bottom && bottom === left) {
              return top;
            }
            return `top:${top}; right:${right}; bottom:${bottom}; left:${left}`;
          }

          return Array.from(document.querySelectorAll("a[href]")).map((el, index) => {
            const style = window.getComputedStyle(el);
            return {
              href: el.getAttribute("href") || "",
              text: (el.innerText || "").trim().replace(/\\s+/g, " ").slice(0, 500),
              order: index + 1,
              color: style.color,
              fontSize: style.fontSize,
              fontWeight: style.fontWeight,
              textDecoration: style.textDecorationLine || style.textDecoration || "",
              backgroundColor: style.backgroundColor,
              border: borderFor(style)
            };
          });
        }
        """
        links = await page.evaluate(script)
        return links if isinstance(links, list) else []

    def _build_records(
        self,
        original_source_url: str,
        final_source_url: str,
        raw_links: list[dict[str, Any]],
    ) -> list[LinkStyleRecord]:
        records: list[LinkStyleRecord] = []
        for raw in raw_links:
            link_url = normalize_link_url(final_source_url, str(raw.get("href", "")))
            if not link_url:
                continue

            parsed = urlparse(link_url)
            if parsed.scheme not in {"http", "https"}:
                continue

            records.append(
                LinkStyleRecord(
                    source_url=original_source_url,
                    link_url=link_url,
                    link_text=str(raw.get("text") or ""),
                    link_order=int(raw.get("order") or len(records) + 1),
                    color=str(raw.get("color") or ""),
                    font_size=str(raw.get("fontSize") or ""),
                    font_weight=str(raw.get("fontWeight") or ""),
                    text_decoration=str(raw.get("textDecoration") or ""),
                    background_color=str(raw.get("backgroundColor") or ""),
                    border=str(raw.get("border") or ""),
                    is_external=classify_link(final_source_url, link_url) == "external",
                )
            )
        return records


def choose_random_profile() -> tuple[tuple[int, int], str, str]:
    viewport_pool = DESKTOP_VIEWPORTS + MOBILE_VIEWPORTS
    return random.choice(viewport_pool), random.choice(TIMEZONE_POOL), random.choice(USER_AGENT_POOL)
