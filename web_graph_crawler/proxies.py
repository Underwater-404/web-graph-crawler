"""Proxy pool loading and rotation for discovery and the crawl.

A ``proxies.txt`` holds one proxy per line, scheme-prefixed and auto-detected:

    socks5://127.0.0.1:9050        # Tor / no-auth SOCKS5
    socks5://10.0.0.9:1080
    http://user:pass@1.2.3.4:8080  # HTTP(S) proxies may carry auth
    9.9.9.9:8000                   # bare host:port is assumed http://

Rotation is random per request/page; a proxy that errors is marked dead and
skipped. If every proxy dies, the pool resets and gives them another chance
(transient failures shouldn't permanently empty a small pool).

SOCKS support in the stdlib discovery client requires PySocks
(``pip install PySocks`` / ``pip install -e '.[socks]'``); it's imported lazily
so HTTP-only setups need no extra dependency. Playwright supports SOCKS5 for the
crawl but **without** username/password auth (a Chromium limitation) — use
HTTP/HTTPS proxies when you need authentication.
"""

from __future__ import annotations

import random
from pathlib import Path
from urllib.parse import urlparse

VALID_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4", "socks4a")


def normalize_proxy(line: str) -> str | None:
    """Return a scheme-qualified proxy URL, or None for blanks/comments/invalid."""
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    if "://" not in text:
        text = "http://" + text  # bare host:port -> http
    parsed = urlparse(text)
    if parsed.scheme.lower() not in VALID_SCHEMES or not parsed.hostname:
        return None
    return text


def load_proxies(path: Path) -> list[str]:
    """Read and de-duplicate proxies from a file (order preserved)."""
    seen: set[str] = set()
    proxies: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        proxy = normalize_proxy(line)
        if proxy and proxy not in seen:
            seen.add(proxy)
            proxies.append(proxy)
    if not proxies:
        raise ValueError(f"No usable proxies found in {path}")
    return proxies


class ProxyPool:
    """Random-rotating proxy pool with dead-proxy skipping."""

    def __init__(self, proxies: list[str], rng: random.Random | None = None) -> None:
        self._all: list[str] = list(dict.fromkeys(proxies))
        self._dead: set[str] = set()
        self._rng = rng or random.Random()

    def pick(self) -> str | None:
        live = [p for p in self._all if p not in self._dead]
        if not live:
            self._dead.clear()  # all dead -> give them another chance
            live = self._all
        return self._rng.choice(live) if live else None

    def mark_dead(self, proxy: str | None) -> None:
        if proxy:
            self._dead.add(proxy)

    def __len__(self) -> int:
        return len(self._all)

    def __bool__(self) -> bool:
        return bool(self._all)
