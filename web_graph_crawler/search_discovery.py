"""Dork-driven page discovery built on pluggable search providers.

A *dork* is just a search query that leans on operators (``site:``,
``inurl:``, ``intitle:``, ``filetype:``, ``"exact phrase"`` ...). Each dork is
sent to the configured :mod:`.search_providers` backend and the resulting URLs
are de-duplicated, capped per domain, and returned for crawling.
"""

from __future__ import annotations

import logging
import os
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .filters import dedup_key, is_excluded
from .links import normalized_host
from .progress import NULL_REPORTER, Reporter
from .search_providers import (
    DEFAULT_USER_AGENT,
    ProviderSettings,
    SearchError,
    SearchProvider,
)

LOGGER = logging.getLogger("web_graph_crawler.search")


def load_dorks(path: Path) -> list[str]:
    """Read one dork/query per line, ignoring blank lines and ``#`` comments."""

    dorks: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        dorks.append(stripped)
    if not dorks:
        raise ValueError(f"No dorks found in {path}")
    return dorks


# Backwards-compatible alias: the style collector historically called these "queries".
load_queries = load_dorks


def load_seed_urls(path: Path) -> list[str]:
    urls: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parsed = urlparse(stripped)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            urls.append(stripped)
        else:
            LOGGER.warning("Skipping invalid seed URL: %s", stripped)
    if not urls:
        raise ValueError(f"No valid seed URLs found in {path}")
    return urls


def load_single_proxy(proxy: str | None, proxies_file: Path | None) -> str | None:
    """Return a single configured proxy.

    Proxy rotation is intentionally not implemented. A proxy here is for normal
    routing through a lab, institution, VPN, or corporate network.
    """

    if proxy:
        return proxy.strip()
    if not proxies_file:
        return None

    proxies = [
        line.strip()
        for line in proxies_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not proxies:
        LOGGER.warning("Proxy file %s is empty; continuing without a proxy", proxies_file)
        return None
    if len(proxies) > 1:
        LOGGER.warning("Proxy rotation is disabled; using the first proxy from %s only", proxies_file)
    return proxies[0]


def resolve_api_key(explicit: str | None, *env_vars: str) -> str | None:
    """Return the first non-empty key from the flag or the given env vars."""

    if explicit:
        return explicit
    for env_var in env_vars:
        value = os.getenv(env_var)
        if value:
            return value
    return None


def get_bing_api_key(explicit_key: str | None) -> str:
    """Kept for backwards compatibility with older scripts."""

    api_key = resolve_api_key(explicit_key, "BING_SEARCH_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Bing discovery requires an API key. Set BING_SEARCH_API_KEY or pass --bing-api-key. "
            "Use --seed-urls to skip search discovery, or pick another --search-provider."
        )
    return api_key


def build_provider_settings(
    name: str,
    *,
    brave_key: str | None = None,
    google_key: str | None = None,
    google_cx: str | None = None,
    bing_key: str | None = None,
    bing_endpoint: str | None = None,
    searxng_url: str | None = None,
    browser_engine: str = "bing",
    proxy: str | None = None,
    user_agent: str | None = None,
    timeout: float = 20.0,
) -> ProviderSettings:
    """Resolve keys/instance URLs (flag first, then env vars) into settings."""

    api_key: str | None = None
    if name == "brave":
        api_key = resolve_api_key(brave_key, "BRAVE_SEARCH_API_KEY", "BRAVE_API_KEY")
    elif name == "google":
        api_key = resolve_api_key(google_key, "GOOGLE_API_KEY", "GOOGLE_SEARCH_API_KEY")
    elif name == "bing":
        api_key = resolve_api_key(bing_key, "BING_SEARCH_API_KEY")

    return ProviderSettings(
        name=name,
        api_key=api_key,
        endpoint=bing_endpoint,
        searxng_url=searxng_url or os.getenv("SEARXNG_URL"),
        google_cx=google_cx or os.getenv("GOOGLE_CX") or os.getenv("GOOGLE_SEARCH_CX"),
        browser_engine=browser_engine,
        proxy=proxy,
        user_agent=user_agent or DEFAULT_USER_AGENT,
        timeout=timeout,
    )


@dataclass
class DiscoveryConfig:
    dorks: list[str]
    provider: SearchProvider
    results_per_query: int = 20
    max_results: int = 200
    max_per_domain: int = 10
    delay_min: float = 2.0
    delay_max: float = 6.0
    dump_path: Path | None = None
    exclude_famous: bool = True
    exclude_domains: frozenset[str] = frozenset()
    _rng: random.Random = field(default_factory=random.Random, repr=False)


def discover_urls(config: DiscoveryConfig, reporter: Reporter = NULL_REPORTER) -> list[str]:
    """Run every dork through the provider and return de-duplicated result URLs."""

    discovered: list[str] = []
    seen: set[str] = set()
    total = len(config.dorks)
    reporter.discovery_start(total, config.provider.name)

    for index, dork in enumerate(config.dorks, start=1):
        LOGGER.info("Dork %d/%d via %s: %s", index, total, config.provider.name, dork)
        reporter.dork_start(index, total, dork)
        try:
            results = config.provider.search(dork, max(1, config.results_per_query))
        except SearchError:
            # Misconfiguration (missing key, bad instance): fail fast, do not spin.
            raise
        except Exception as exc:  # noqa: BLE001 - one bad dork should not abort the run
            LOGGER.warning("Dork failed (%r): %s", dork, exc)
            reporter.warn(f"dork failed: {exc}")
            results = []

        new_for_dork = 0
        for url in results:
            key = dedup_key(url)
            if key in seen:
                continue
            seen.add(key)
            discovered.append(url)
            new_for_dork += 1
        LOGGER.info("Dork produced %d new URL(s) (%d total so far)", new_for_dork, len(discovered))
        reporter.dork_result(index, total, dork, new_for_dork, len(discovered))

        if index < total:
            delay = config._rng.uniform(config.delay_min, config.delay_max)
            LOGGER.info("Search pacing: waiting %.2fs before next dork", delay)
            time.sleep(delay)

    selected = select_urls(
        discovered,
        config.max_results,
        config.max_per_domain,
        exclude_famous=config.exclude_famous,
        exclude_domains=config.exclude_domains,
    )
    LOGGER.info("Discovered %d URL(s); selected %d after caps/filters", len(discovered), len(selected))
    reporter.discovery_done(len(selected), len(discovered))

    if config.dump_path is not None:
        _dump_urls(config.dump_path, selected)

    return selected


def select_urls(
    urls: list[str],
    max_results: int,
    max_per_domain: int,
    *,
    exclude_famous: bool = True,
    exclude_domains: frozenset[str] = frozenset(),
) -> list[str]:
    """De-duplicate, drop excluded/famous hosts, cap per-domain, clamp to max.

    De-duplication is by canonical URL (so ``http`` vs ``https`` and trailing
    slashes collapse). ``0`` disables a cap. Order is preserved so the first
    (usually most relevant) results survive.
    """

    selected: list[str] = []
    seen: set[str] = set()
    per_domain: dict[str, int] = defaultdict(int)

    for url in urls:
        key = dedup_key(url)
        if key in seen:
            continue
        host = normalized_host(urlparse(url).hostname or "")
        if not host:
            continue
        if is_excluded(host, exclude_domains, exclude_famous):
            continue
        if max_per_domain and per_domain[host] >= max_per_domain:
            continue

        selected.append(url)
        seen.add(key)
        per_domain[host] += 1

        if max_results and len(selected) >= max_results:
            break

    return selected


def _dump_urls(path: Path, urls: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(urls) + ("\n" if urls else ""), encoding="utf-8")
    LOGGER.info("Wrote %d discovered URL(s) to %s", len(urls), path)
