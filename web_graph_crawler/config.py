"""Configuration data and CLI value parsing."""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class CrawlerConfig:
    urls: list[str]
    output: Path
    browser: str
    headless: bool
    storage_state: Path
    respect_robots: bool
    robots_user_agent: str
    min_delay: float
    max_delay: float
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
    include_non_http: bool
    dedupe_links: bool
    ignore_https_errors: bool
    log_file: Path | None
    max_depth: int
    crawl_scope: str
    max_pages: int
    max_pages_per_domain: int
    proxies: tuple[str, ...] = ()


def load_urls(positional_urls: list[str], urls_file: Path | None) -> list[str]:
    urls: list[str] = []
    urls.extend(positional_urls)

    if urls_file:
        for line in urls_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            urls.append(stripped)

    normalized: list[str] = []
    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Only absolute http(s) URLs are supported: {url}")
        normalized.append(url)

    return normalized


def parse_viewport(raw: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{3,5})x(\d{3,5})", raw.lower().strip())
    if not match:
        raise argparse.ArgumentTypeError("viewport must look like 1366x900")
    width, height = int(match.group(1)), int(match.group(2))
    if width < 320 or height < 320:
        raise argparse.ArgumentTypeError("viewport dimensions must be at least 320x320")
    return width, height
