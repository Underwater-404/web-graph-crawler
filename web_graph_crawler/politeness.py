"""Robots.txt checks and per-domain crawl pacing."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

from .links import normalized_host

LOGGER = logging.getLogger("web_graph_crawler")

# Sentinels distinguish the three robots.txt outcomes that RFC 9309 defines:
#   ALLOW_ALL   -> robots.txt is "unavailable" (4xx); crawling is permitted.
#   UNREACHABLE -> robots.txt is "unreachable" (5xx / network); assume disallow.
# A real parser means rules were fetched and should be evaluated per-URL.
ALLOW_ALL = "allow_all"
UNREACHABLE = "unreachable"


class RobotsCache:
    """Caches robots.txt outcomes per origin, following RFC 9309 status rules."""

    def __init__(self, user_agent: str, timeout: float = 10.0) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self._cache: dict[str, RobotFileParser | str] = {}

    async def allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False

        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._cache:
            self._cache[origin] = await asyncio.to_thread(self._fetch_entry, origin)

        entry = self._cache[origin]
        if entry == ALLOW_ALL:
            return True
        if entry == UNREACHABLE:
            LOGGER.warning("robots.txt unreachable for %s; skipping by default", origin)
            return False
        return entry.can_fetch(self.user_agent, url)

    def _fetch_entry(self, origin: str) -> RobotFileParser | str:
        robots_url = f"{origin}/robots.txt"
        try:
            request = Request(
                robots_url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/plain,*/*;q=0.8",
                },
            )
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read(512_000).decode("utf-8", errors="replace")
        except HTTPError as exc:
            # 4xx (incl. 401/403/404): "unavailable" -> allow all. 5xx: unreachable.
            if 400 <= exc.code < 500:
                LOGGER.debug("robots.txt for %s returned HTTP %s; treating as allow-all", origin, exc.code)
                return ALLOW_ALL
            LOGGER.warning("robots.txt for %s returned HTTP %s; treating as unreachable", origin, exc.code)
            return UNREACHABLE
        except (URLError, TimeoutError, ValueError) as exc:
            LOGGER.warning("Failed to load %s: %s", robots_url, exc)
            return UNREACHABLE

        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(body.splitlines())
        LOGGER.debug("Loaded robots.txt from %s", robots_url)
        return parser


class DomainRateLimiter:
    """Sequential per-domain rate limiter."""

    def __init__(self, min_delay: float, max_delay: float) -> None:
        if min_delay < 0 or max_delay < min_delay:
            raise ValueError("delay range must satisfy 0 <= min_delay <= max_delay")
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._last_started: dict[str, float] = {}

    async def wait_before(self, url: str) -> None:
        host = normalized_host(urlparse(url).hostname or "")
        now = time.monotonic()
        base_delay = random.uniform(self.min_delay, self.max_delay)

        last_started = self._last_started.get(host)
        if last_started is None:
            delay = 0.0
        else:
            elapsed = now - last_started
            delay = max(0.0, base_delay - elapsed)

        if delay > 0:
            LOGGER.info("Rate limit: waiting %.2fs before next request to %s", delay, host)
            await asyncio.sleep(delay)

        self._last_started[host] = time.monotonic()
