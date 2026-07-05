"""CLI for discovering pages and collecting computed hyperlink styles."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

from .config import parse_viewport
from .links import normalized_host
from .politeness import DomainRateLimiter, RobotsCache
from .search_discovery import (
    DiscoveryConfig,
    build_provider_settings,
    discover_urls,
    load_dorks,
    load_seed_urls,
    load_single_proxy,
)
from .search_providers import PROVIDER_NAMES, create_search_provider
from .style_browser import LinkStyleCollector, StyleBrowserConfig, choose_random_profile
from .style_records import StyleCsvSink

LOGGER = logging.getLogger("web_graph_crawler.styles")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover pages from dorks (or a seed list) and collect computed CSS "
        "styles for rendered links.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--dorks", type=Path, help="Text file with one dork/query per line")
    parser.add_argument(
        "--queries",
        type=Path,
        help="Alias of --dorks (kept for backwards compatibility)",
    )
    parser.add_argument(
        "--seed-urls",
        type=Path,
        help="Optional URL file to visit directly instead of using search discovery",
    )
    parser.add_argument("--output", type=Path, required=True, help="CSV output path")
    parser.add_argument(
        "--search-provider",
        choices=PROVIDER_NAMES,
        default="duckduckgo",
        help="Search backend used to turn dorks into URLs",
    )
    parser.add_argument("--searxng-url", help="Base URL of a SearXNG instance (searxng provider)")
    parser.add_argument("--brave-api-key", help="Brave Search API key; or set BRAVE_SEARCH_API_KEY")
    parser.add_argument("--google-api-key", help="Google API key; or set GOOGLE_API_KEY")
    parser.add_argument("--google-cx", help="Google Programmable Search engine id; or set GOOGLE_CX")
    parser.add_argument(
        "--bing-api-key",
        help="Legacy Bing Web Search API key; defaults to BING_SEARCH_API_KEY env var",
    )
    parser.add_argument(
        "--bing-endpoint",
        default="https://api.bing.microsoft.com/v7.0/search",
        help="Bing (or compatible) search endpoint",
    )
    parser.add_argument("--search-timeout", type=float, default=20.0, help="Search HTTP timeout")
    parser.add_argument(
        "--results-per-query",
        type=int,
        default=10,
        help="Search result URLs to request per dork",
    )
    parser.add_argument("--max-pages", type=int, default=200, help="Maximum pages to visit")
    parser.add_argument(
        "--max-pages-per-domain",
        type=int,
        default=10,
        help="Maximum discovered pages to visit per domain",
    )
    parser.add_argument(
        "--search-delay-min",
        type=float,
        default=2.0,
        help="Minimum delay between search API queries",
    )
    parser.add_argument(
        "--search-delay-max",
        type=float,
        default=7.0,
        help="Maximum delay between search API queries",
    )
    parser.add_argument(
        "--browser",
        choices=["chromium", "firefox", "webkit"],
        default="chromium",
        help="Playwright browser engine",
    )
    parser.add_argument("--headful", action="store_true", help="Show the browser window")
    parser.add_argument(
        "--storage-state",
        type=Path,
        default=Path("data/style_storage_state.json"),
        help="JSON file for cookies/local storage persistence",
    )
    parser.add_argument(
        "--ignore-robots",
        action="store_true",
        help="Do not check robots.txt before visiting a discovered URL",
    )
    parser.add_argument(
        "--robots-user-agent",
        default="AcademicLinkStyleCollector/1.0",
        help="User-Agent token used for robots.txt checks",
    )
    parser.add_argument("--min-delay", type=float, default=2.0, help="Minimum per-host delay")
    parser.add_argument("--max-delay", type=float, default=7.0, help="Maximum per-host delay")
    parser.add_argument("--timeout-ms", type=int, default=45_000, help="Page navigation timeout")
    parser.add_argument(
        "--network-idle-ms",
        type=int,
        default=8_000,
        help="How long to wait for network idle after DOMContentLoaded",
    )
    parser.add_argument(
        "--selector-wait-ms",
        type=int,
        default=5_000,
        help="How long to wait for at least one rendered anchor",
    )
    parser.add_argument(
        "--max-scroll-rounds",
        type=int,
        default=24,
        help="Maximum incremental scroll steps for lazy content",
    )
    parser.add_argument("--scroll-pause-min", type=float, default=0.35)
    parser.add_argument("--scroll-pause-max", type=float, default=1.15)
    parser.add_argument("--max-retries", type=int, default=2, help="Attempts per page URL")
    parser.add_argument("--retry-backoff", type=float, default=2.0)
    parser.add_argument(
        "--proxy",
        help="Optional single HTTP/HTTPS/SOCKS proxy for normal network routing",
    )
    parser.add_argument(
        "--proxies",
        type=Path,
        help="Optional proxy file; first non-comment proxy is used, rotation is disabled",
    )
    parser.add_argument("--user-agent", help="Optional fixed browser User-Agent")
    parser.add_argument(
        "--viewport",
        type=parse_viewport,
        help="Browser viewport as WIDTHxHEIGHT; default is 1366x900 unless --random-profile is used",
    )
    parser.add_argument("--timezone-id", help="Optional browser timezone, e.g. UTC")
    parser.add_argument("--locale", default="en-US", help="Browser locale")
    parser.add_argument(
        "--random-profile",
        action="store_true",
        help="Randomly choose one viewport/timezone/User-Agent sampling profile for the run",
    )
    parser.add_argument("--ignore-https-errors", action="store_true")
    parser.add_argument(
        "--log-file",
        type=Path,
        default=Path("data/link_styles.log"),
        help="Log file path; use --no-log-file to disable",
    )
    parser.add_argument("--no-log-file", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def configure_logging(log_file: Path | None, verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def discover_or_load_urls(args: argparse.Namespace, proxy: str | None) -> list[str]:
    if args.seed_urls:
        LOGGER.info("Loading seed URLs from %s", args.seed_urls)
        return load_seed_urls(args.seed_urls)

    dork_source = args.dorks or args.queries
    if not dork_source:
        raise ValueError(
            "Provide --dorks (or --queries) for search discovery, or --seed-urls for direct collection"
        )

    dorks = load_dorks(dork_source)
    settings = build_provider_settings(
        args.search_provider,
        brave_key=args.brave_api_key,
        google_key=args.google_api_key,
        google_cx=args.google_cx,
        bing_key=args.bing_api_key,
        bing_endpoint=args.bing_endpoint,
        searxng_url=args.searxng_url,
        proxy=proxy,
        user_agent=args.user_agent,
        timeout=args.search_timeout,
    )
    provider = create_search_provider(settings)
    # Final per-domain and total caps are applied by select_visit_urls in main().
    return discover_urls(
        DiscoveryConfig(
            dorks=dorks,
            provider=provider,
            results_per_query=max(1, args.results_per_query),
            max_results=0,
            max_per_domain=0,
            delay_min=args.search_delay_min,
            delay_max=args.search_delay_max,
        )
    )


def select_visit_urls(
    urls: list[str],
    max_pages: int,
    max_pages_per_domain: int,
) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    per_domain: dict[str, int] = defaultdict(int)

    for url in urls:
        if url in seen:
            continue

        host = normalized_host(urlparse(url).hostname or "")
        if not host:
            continue
        if per_domain[host] >= max_pages_per_domain:
            continue

        selected.append(url)
        seen.add(url)
        per_domain[host] += 1

        if len(selected) >= max_pages:
            break

    LOGGER.info(
        "Selected %d page URL(s) after de-duplication and per-domain caps",
        len(selected),
    )
    return selected


def make_browser_config(args: argparse.Namespace, proxy: str | None) -> StyleBrowserConfig:
    viewport = args.viewport or (1366, 900)
    timezone_id = args.timezone_id
    user_agent = args.user_agent

    if args.random_profile:
        random_viewport, random_timezone, random_user_agent = choose_random_profile()
        viewport = args.viewport or random_viewport
        timezone_id = args.timezone_id or random_timezone
        user_agent = args.user_agent or random_user_agent
        LOGGER.info(
            "Using random sampling profile: viewport=%sx%s timezone=%s",
            viewport[0],
            viewport[1],
            timezone_id,
        )

    return StyleBrowserConfig(
        browser=args.browser,
        headless=not args.headful,
        storage_state=args.storage_state,
        timeout_ms=args.timeout_ms,
        network_idle_ms=args.network_idle_ms,
        selector_wait_ms=args.selector_wait_ms,
        max_scroll_rounds=args.max_scroll_rounds,
        scroll_pause_min=args.scroll_pause_min,
        scroll_pause_max=args.scroll_pause_max,
        max_retries=args.max_retries,
        retry_backoff=args.retry_backoff,
        proxy=proxy,
        user_agent=user_agent,
        viewport=viewport,
        timezone_id=timezone_id,
        locale=args.locale,
        ignore_https_errors=args.ignore_https_errors,
    )


async def collect_styles(
    urls: list[str],
    output: Path,
    browser_config: StyleBrowserConfig,
    respect_robots: bool,
    robots_user_agent: str,
    min_delay: float,
    max_delay: float,
) -> int:
    sink = StyleCsvSink(output)
    robots = RobotsCache(robots_user_agent)
    rate_limiter = DomainRateLimiter(min_delay, max_delay)

    success_count = 0
    failure_count = 0
    skipped_count = 0
    row_count = 0

    try:
        async with LinkStyleCollector(browser_config) as collector:
            for source_url in urls:
                if respect_robots:
                    allowed = await robots.allowed(source_url)
                    if not allowed:
                        LOGGER.warning("robots.txt disallows or could not verify %s; skipped", source_url)
                        skipped_count += 1
                        continue

                await rate_limiter.wait_before(source_url)
                try:
                    records = await collector.collect_with_retries(source_url)
                    rows = sink.write_many(records)
                    row_count += rows
                    success_count += 1
                    LOGGER.info("Wrote %d style row(s) for %s", rows, source_url)
                except Exception as exc:
                    failure_count += 1
                    LOGGER.error("Failed: %s: %s", source_url, exc)
    finally:
        sink.close()

    LOGGER.info(
        "Done: %d succeeded, %d failed, %d skipped, %d CSV rows; output: %s",
        success_count,
        failure_count,
        skipped_count,
        row_count,
        output,
    )
    return 1 if failure_count else 0


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    configure_logging(None if args.no_log_file else args.log_file, args.verbose)

    try:
        proxy = load_single_proxy(args.proxy, args.proxies)
        urls = discover_or_load_urls(args, proxy)
        urls = select_visit_urls(
            urls,
            max_pages=max(1, args.max_pages),
            max_pages_per_domain=max(1, args.max_pages_per_domain),
        )
        if not urls:
            raise RuntimeError("No URLs selected for style collection")

        browser_config = make_browser_config(args, proxy)
        return asyncio.run(
            collect_styles(
                urls=urls,
                output=args.output,
                browser_config=browser_config,
                respect_robots=not args.ignore_robots,
                robots_user_agent=args.robots_user_agent,
                min_delay=args.min_delay,
                max_delay=args.max_delay,
            )
        )
    except KeyboardInterrupt:
        LOGGER.warning("Interrupted")
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        LOGGER.error("%s", exc)
        return 2
