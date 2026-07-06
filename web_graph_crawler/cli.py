"""Command line interface and crawl orchestration."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlparse

from .browser import RenderedLinkCrawler
from .config import CrawlerConfig, load_urls, parse_viewport
from .constants import DEFAULT_LOG_FILE, DEFAULT_OUTPUT, DEFAULT_STORAGE_STATE
from .filters import parse_domain_list
from .links import LinkRecord, canonical_url, normalized_host
from .output import CsvSink
from .politeness import DomainRateLimiter, RobotsCache
from .progress import NULL_REPORTER, Reporter
from .proxies import load_proxies
from .search_discovery import (
    DiscoveryConfig,
    build_provider_settings,
    discover_urls,
    load_dorks,
)
from .search_providers import PROVIDER_NAMES, ProviderSettings, create_search_provider
from .ui import Console, TerminalReporter, make_console

LOGGER = logging.getLogger("web_graph_crawler")

CRAWL_SCOPES = ("seed-hosts", "same-host", "any")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="web_graph_crawler",
        description="Discover pages from dorks (or take URLs directly), render them with "
        "Playwright, and collect the hyperlink graph into CSV.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("urls", nargs="*", help="Starting URL(s), e.g. https://example.org")
    parser.add_argument("--urls-file", type=Path, help="Text file with one URL per line")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT, help="CSV output path")

    discovery = parser.add_argument_group("dork discovery")
    discovery.add_argument(
        "--dork",
        action="append",
        metavar="QUERY",
        help='Search dork to discover seed URLs; repeatable, e.g. --dork "site:gov.uk inurl:login"',
    )
    discovery.add_argument("--dorks", type=Path, help="Text file with one dork per line")
    discovery.add_argument(
        "--search-provider",
        choices=PROVIDER_NAMES,
        default="duckduckgo",
        help="Search backend used to turn dorks into URLs",
    )
    discovery.add_argument(
        "--results-per-dork",
        type=int,
        default=50,
        help="Result URLs to request per dork (engines cap the real max per query)",
    )
    discovery.add_argument(
        "--include-famous",
        action="store_true",
        help="Keep mainstream sites (Google/YouTube/Facebook/Wikipedia/...); they are dropped by default",
    )
    discovery.add_argument(
        "--exclude-domains",
        metavar="d1,d2,...",
        help="Extra domains to drop from discovery results (comma/space separated)",
    )
    discovery.add_argument(
        "--search-delay-min",
        type=float,
        default=2.0,
        help="Minimum delay between dork queries",
    )
    discovery.add_argument(
        "--search-delay-max",
        type=float,
        default=6.0,
        help="Maximum delay between dork queries",
    )
    discovery.add_argument("--search-timeout", type=float, default=20.0, help="Search HTTP timeout")
    discovery.add_argument(
        "--search-proxy",
        help="Proxy for search requests only; defaults to --proxy when omitted",
    )
    discovery.add_argument("--searxng-url", help="Base URL of a SearXNG instance (searxng provider)")
    discovery.add_argument(
        "--searxng-engines",
        metavar="e1,e2",
        help="Restrict SearXNG to these engines, e.g. 'mojeek' or 'mojeek,brave,duckduckgo' "
        "(skips throttled google/bing)",
    )
    discovery.add_argument(
        "--browser-engine",
        choices=("bing", "duckduckgo", "mojeek"),
        default="bing",
        help="Which engine the keyless 'browser' provider drives (default: bing)",
    )
    discovery.add_argument(
        "--cc-index",
        help="Common Crawl index id to query (default: latest, e.g. CC-MAIN-2024-33)",
    )
    discovery.add_argument(
        "--cc-max-records",
        type=int,
        default=10000,
        help="Max CDX records to scan per dork for the commoncrawl provider",
    )
    discovery.add_argument("--brave-api-key", help="Brave Search API key; or set BRAVE_SEARCH_API_KEY")
    discovery.add_argument("--google-api-key", help="Google API key; or set GOOGLE_API_KEY")
    discovery.add_argument("--google-cx", help="Google Programmable Search engine id; or set GOOGLE_CX")
    discovery.add_argument("--bing-api-key", help="Legacy Bing key; or set BING_SEARCH_API_KEY")
    discovery.add_argument(
        "--serper-api-key",
        help="Serper.dev key (Google results via SERP API); or set SERPER_API_KEY",
    )
    discovery.add_argument(
        "--bing-endpoint",
        default="https://api.bing.microsoft.com/v7.0/search",
        help="Bing (or compatible) search endpoint",
    )
    discovery.add_argument(
        "--discovered-urls-out",
        type=Path,
        help="Write the selected discovered URLs to this file for review/reuse",
    )

    graph = parser.add_argument_group("graph crawl")
    graph.add_argument(
        "--max-depth",
        type=int,
        default=0,
        help="Follow links up to this depth (0 = only the seed/discovered pages)",
    )
    graph.add_argument(
        "--crawl-scope",
        choices=CRAWL_SCOPES,
        default="seed-hosts",
        help="Which links to follow when --max-depth > 0",
    )
    graph.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Maximum pages to fetch in total (0 = no limit)",
    )
    graph.add_argument(
        "--max-pages-per-domain",
        type=int,
        default=0,
        help="Maximum pages to fetch per domain (0 = no limit)",
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
        default=DEFAULT_STORAGE_STATE,
        help="JSON file for cookies/local storage persistence",
    )
    parser.add_argument(
        "--ignore-robots",
        action="store_true",
        help="Do not check robots.txt before visiting a URL",
    )
    parser.add_argument(
        "--robots-user-agent",
        default="AcademicWebGraphCrawler/1.0",
        help="User-Agent token used for robots.txt checks",
    )
    parser.add_argument("--min-delay", type=float, default=2.0, help="Minimum per-host delay")
    parser.add_argument("--max-delay", type=float, default=5.0, help="Maximum per-host delay")
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
        help="How long to wait for common link selectors",
    )
    parser.add_argument(
        "--max-scroll-rounds",
        type=int,
        default=24,
        help="Maximum incremental scroll steps for lazy content",
    )
    parser.add_argument(
        "--scroll-pause-min",
        type=float,
        default=0.35,
        help="Minimum pause after each scroll step",
    )
    parser.add_argument(
        "--scroll-pause-max",
        type=float,
        default=1.15,
        help="Maximum pause after each scroll step",
    )
    parser.add_argument("--max-retries", type=int, default=3, help="Attempts per URL")
    parser.add_argument(
        "--retry-backoff",
        type=float,
        default=2.0,
        help="Base seconds for exponential retry backoff",
    )
    parser.add_argument(
        "--proxy",
        help="Optional HTTP/HTTPS/SOCKS proxy, e.g. http://user:pass@127.0.0.1:8080",
    )
    parser.add_argument(
        "--proxies",
        type=Path,
        metavar="FILE",
        help="File of proxies (one per line, scheme-prefixed) to rotate across "
        "BOTH discovery and the crawl; SOCKS needs PySocks (pip install -e '.[socks]')",
    )
    parser.add_argument(
        "--crawl-proxies",
        type=Path,
        metavar="FILE",
        help="Proxies to rotate for the CRAWL only (discovery stays direct). Use with "
        "an API provider like serper to keep discovery keyed but hide the crawl IP",
    )
    parser.add_argument(
        "--user-agent",
        help="Optional fixed browser User-Agent for compatibility testing",
    )
    parser.add_argument(
        "--viewport",
        type=parse_viewport,
        default=(1366, 900),
        help="Browser viewport as WIDTHxHEIGHT",
    )
    parser.add_argument("--timezone-id", help="Optional browser timezone, e.g. UTC")
    parser.add_argument("--locale", default="en-US", help="Browser locale")
    parser.add_argument(
        "--include-non-http",
        action="store_true",
        help="Keep mailto:, tel:, and other non-http links in the CSV",
    )
    parser.add_argument(
        "--dedupe-links",
        action="store_true",
        help="Write each resolved link URL once per source page",
    )
    parser.add_argument(
        "--ignore-https-errors",
        action="store_true",
        help="Allow pages with certificate errors",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=DEFAULT_LOG_FILE,
        help="Optional log file path; use --no-log-file to disable",
    )
    parser.add_argument("--no-log-file", action="store_true", help="Disable file logging")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")

    ui = parser.add_argument_group("terminal UI")
    ui.add_argument(
        "--no-ui",
        action="store_true",
        help="Disable the interactive UI; stream plain log lines instead",
    )
    ui.add_argument("--no-color", action="store_true", help="Disable ANSI colours")
    ui.add_argument(
        "--no-input",
        action="store_true",
        help="Never prompt; fail if no dorks/URLs were provided",
    )
    return parser


def collect_dorks(args: argparse.Namespace) -> list[str]:
    dorks: list[str] = list(args.dork or [])
    if args.dorks:
        dorks.extend(load_dorks(args.dorks))

    ordered: list[str] = []
    seen: set[str] = set()
    for dork in dorks:
        stripped = dork.strip()
        if stripped and stripped not in seen:
            seen.add(stripped)
            ordered.append(stripped)
    return ordered


def provider_settings_from_args(args: argparse.Namespace, proxy: str | None) -> ProviderSettings:
    return build_provider_settings(
        args.search_provider,
        brave_key=args.brave_api_key,
        google_key=args.google_api_key,
        google_cx=args.google_cx,
        bing_key=args.bing_api_key,
        bing_endpoint=args.bing_endpoint,
        serper_key=args.serper_api_key,
        searxng_url=args.searxng_url,
        searxng_engines=args.searxng_engines,
        browser_engine=args.browser_engine,
        cc_index=args.cc_index,
        cc_max_records=args.cc_max_records,
        proxy=proxy,
        proxies=getattr(args, "proxies_list", ()),
        user_agent=args.user_agent,
        timeout=args.search_timeout,
    )


def gather_seed_urls(args: argparse.Namespace, reporter: Reporter = NULL_REPORTER) -> list[str]:
    """Combine positional URLs, --urls-file, and dork-discovered URLs."""

    dorks = collect_dorks(args)
    if not args.urls and not args.urls_file and not dorks:
        raise ValueError("Provide URLs, --urls-file, or --dork/--dorks")

    seeds: list[str] = load_urls(args.urls, args.urls_file)

    if dorks:
        search_proxy = args.search_proxy or args.proxy
        provider = create_search_provider(provider_settings_from_args(args, search_proxy))
        LOGGER.info("Discovering seed URLs from %d dork(s) via %s", len(dorks), provider.name)
        discovered = discover_urls(
            DiscoveryConfig(
                dorks=dorks,
                provider=provider,
                results_per_query=args.results_per_dork,
                max_results=args.max_pages if args.max_depth == 0 else 0,
                max_per_domain=args.max_pages_per_domain,
                delay_min=args.search_delay_min,
                delay_max=args.search_delay_max,
                dump_path=args.discovered_urls_out,
                exclude_famous=not args.include_famous,
                exclude_domains=parse_domain_list(args.exclude_domains),
            ),
            reporter=reporter,
        )
        seeds.extend(discovered)

    ordered: list[str] = []
    seen: set[str] = set()
    for url in seeds:
        key = canonical_url(url)
        if key not in seen:
            seen.add(key)
            ordered.append(url)
    return ordered


def config_from_args(args: argparse.Namespace, urls: list[str]) -> CrawlerConfig:
    return CrawlerConfig(
        urls=urls,
        output=args.out,
        browser=args.browser,
        headless=not args.headful,
        storage_state=args.storage_state,
        respect_robots=not args.ignore_robots,
        robots_user_agent=args.robots_user_agent,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        timeout_ms=args.timeout_ms,
        network_idle_ms=args.network_idle_ms,
        selector_wait_ms=args.selector_wait_ms,
        max_scroll_rounds=args.max_scroll_rounds,
        scroll_pause_min=args.scroll_pause_min,
        scroll_pause_max=args.scroll_pause_max,
        max_retries=args.max_retries,
        retry_backoff=args.retry_backoff,
        proxy=args.proxy,
        user_agent=args.user_agent,
        viewport=args.viewport,
        timezone_id=args.timezone_id,
        locale=args.locale,
        include_non_http=args.include_non_http,
        dedupe_links=args.dedupe_links,
        ignore_https_errors=args.ignore_https_errors,
        log_file=None if args.no_log_file else args.log_file,
        max_depth=max(0, args.max_depth),
        crawl_scope=args.crawl_scope,
        max_pages=max(0, args.max_pages),
        max_pages_per_domain=max(0, args.max_pages_per_domain),
        # --crawl-proxies (crawl only) overrides --proxies for the crawl.
        proxies=getattr(args, "crawl_proxies_list", ()) or getattr(args, "proxies_list", ()),
    )


def configure_logging(log_file: Path | None, verbose: bool, to_stdout: bool = True) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = []
    if to_stdout:
        handlers.append(logging.StreamHandler(sys.stdout))
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    if not handlers:
        handlers.append(logging.NullHandler())

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def _eligible_child(link_url: str, from_host: str, scope: str, seed_hosts: set[str]) -> bool:
    parsed = urlparse(link_url)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = normalized_host(parsed.hostname or "")
    if not host:
        return False
    if scope == "any":
        return True
    if scope == "same-host":
        return host == from_host
    return host in seed_hosts  # seed-hosts


def _enqueue_children(
    records: list[LinkRecord],
    from_host: str,
    depth: int,
    config: CrawlerConfig,
    seed_hosts: set[str],
    visited: set[str],
    queued: set[str],
    frontier: deque[tuple[str, int]],
) -> int:
    added = 0
    for record in records:
        child = record.link_url
        if not _eligible_child(child, from_host, config.crawl_scope, seed_hosts):
            continue
        key = canonical_url(child)
        if key in visited or key in queued:
            continue
        queued.add(key)
        frontier.append((child, depth + 1))
        added += 1
    return added


async def run(config: CrawlerConfig, reporter: Reporter = NULL_REPORTER) -> int:
    sink = CsvSink(config.output)
    robots = RobotsCache(config.robots_user_agent)
    rate_limiter = DomainRateLimiter(config.min_delay, config.max_delay)

    seed_hosts = {normalized_host(urlparse(u).hostname or "") for u in config.urls}
    seed_hosts.discard("")

    frontier: deque[tuple[str, int]] = deque((url, 0) for url in config.urls)
    visited: set[str] = set()
    queued: set[str] = {canonical_url(url) for url in config.urls}
    per_domain: dict[str, int] = defaultdict(int)
    planned_total = len(config.urls)

    fetched = success = failure = skipped = 0
    rows_total = 0

    reporter.crawl_start(planned_total, config.max_depth, config.crawl_scope)

    try:
        async with RenderedLinkCrawler(config) as crawler:
            while frontier:
                if config.max_pages and fetched >= config.max_pages:
                    LOGGER.info("Reached max-pages limit (%d); stopping", config.max_pages)
                    reporter.info(f"reached max-pages limit ({config.max_pages}); stopping")
                    break

                url, depth = frontier.popleft()
                canon = canonical_url(url)
                if canon in visited:
                    continue
                visited.add(canon)

                host = normalized_host(urlparse(url).hostname or "")
                if not host:
                    continue

                if config.respect_robots and not await robots.allowed(url):
                    LOGGER.warning("robots.txt disallows or blocks %s; skipped", url)
                    reporter.page_skipped(url, "robots.txt disallow")
                    skipped += 1
                    continue

                if config.max_pages_per_domain and per_domain[host] >= config.max_pages_per_domain:
                    LOGGER.debug("Per-domain cap reached for %s; skipping %s", host, url)
                    reporter.page_skipped(url, "per-domain cap")
                    skipped += 1
                    continue

                await rate_limiter.wait_before(url)
                fetched += 1
                per_domain[host] += 1
                reporter.page_start(url, depth, fetched, planned_total)

                try:
                    records = await crawler.crawl_with_retries(url, reporter=reporter)
                    rows = sink.write_many(records)
                    rows_total += rows
                    success += 1
                    LOGGER.info(
                        "Wrote %d rows for %s (depth %d) to %s", rows, url, depth, config.output
                    )
                    reporter.page_done(url, rows, len(records), depth)
                    if depth < config.max_depth:
                        added = _enqueue_children(
                            records, host, depth, config, seed_hosts, visited, queued, frontier
                        )
                        if added:
                            planned_total += added
                            LOGGER.info("Queued %d new link(s) at depth %d", added, depth + 1)
                            reporter.queued(added, depth + 1)
                except Exception as exc:
                    LOGGER.error("Failed: %s: %s", url, exc)
                    reporter.page_failed(url, str(exc))
                    failure += 1
    finally:
        sink.close()

    stats = {
        "fetched": fetched,
        "success": success,
        "failure": failure,
        "skipped": skipped,
        "rows": rows_total,
        "output": config.output,
    }
    LOGGER.info(
        "Done: %d fetched, %d succeeded, %d failed, %d skipped, %d rows; CSV: %s",
        fetched,
        success,
        failure,
        skipped,
        rows_total,
        config.output,
    )
    reporter.crawl_done(stats)
    return 1 if failure else 0


def _prompt(console: Console, label: str, default: str = "") -> str:
    suffix = console.paint(f" [{default}]", "dim") if default else ""
    marker = console.paint("?", "green", "bold")
    question = f"  {marker} {console.paint(label, 'bold')}{suffix}: "
    try:
        answer = input(question).strip()
    except EOFError:
        answer = ""
    return answer or default


def run_wizard(args: argparse.Namespace, console: Console) -> None:
    """Prompt for dorks/URLs and a few options when the CLI is run bare."""

    console.rule("web-graph-crawler")
    console.log(console.paint("  Feed dorks (search queries) or paste URLs directly.", "white"))
    console.log(
        console.paint('  Dork operators: site:  inurl:  intitle:  filetype:  "exact phrase"', "dim")
    )
    console.log(console.paint("  One entry per line; press Enter on a blank line to start.", "dim"))
    console.log("")

    dorks: list[str] = []
    urls: list[str] = []
    while True:
        try:
            line = input(console.paint(f"  {console.sym.arrow} ", "cyan")).strip()
        except EOFError:
            break
        if not line:
            break
        if line.lower().startswith(("http://", "https://")):
            urls.append(line)
        else:
            dorks.append(line)

    if dorks:
        args.dork = list(args.dork or []) + dorks
    if urls:
        args.urls = list(args.urls or []) + urls
    if not dorks and not urls:
        return

    if dorks:
        provider = _prompt(
            console, f"search engine ({'/'.join(PROVIDER_NAMES)})", args.search_provider
        )
        if provider in PROVIDER_NAMES:
            args.search_provider = provider

    depth = _prompt(console, "crawl depth (0 = only discovered pages)", str(args.max_depth))
    try:
        args.max_depth = max(0, int(depth))
    except ValueError:
        pass

    max_pages = _prompt(console, "max pages", str(args.max_pages))
    try:
        args.max_pages = max(0, int(max_pages))
    except ValueError:
        pass

    out = _prompt(console, "output CSV", str(args.out))
    if out:
        args.out = Path(out)


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    console = make_console(no_color=args.no_color, no_ui=args.no_ui)
    use_ui = not args.no_ui
    reporter: Reporter = TerminalReporter(console) if use_ui else NULL_REPORTER

    # When the UI owns stdout, send logs to the file only so they don't clash
    # with the live spinner. --no-ui restores plain stdout logging.
    configure_logging(
        None if args.no_log_file else args.log_file, args.verbose, to_stdout=not use_ui
    )

    try:
        args.proxies_list = tuple(load_proxies(args.proxies)) if args.proxies else ()
        args.crawl_proxies_list = (
            tuple(load_proxies(args.crawl_proxies)) if args.crawl_proxies else ()
        )
        if args.proxies_list:
            LOGGER.info("Rotating %d proxy/ies (discovery + crawl)", len(args.proxies_list))
            reporter.info(f"rotating {len(args.proxies_list)} proxy/ies (discovery + crawl)")
        if args.crawl_proxies_list:
            LOGGER.info("Rotating %d proxy/ies (crawl only)", len(args.crawl_proxies_list))
            reporter.info(f"rotating {len(args.crawl_proxies_list)} proxy/ies (crawl only)")

        no_targets = not args.urls and not args.urls_file and not collect_dorks(args)
        if no_targets and use_ui and console.interactive and not args.no_input:
            run_wizard(args, console)

        urls = gather_seed_urls(args, reporter)
        if not urls:
            raise ValueError("No URLs to crawl after discovery; refine your dorks or provide URLs")

        config = config_from_args(args, urls)
        if config.max_depth > 0 and not config.max_pages:
            message = (
                "Depth crawling with no --max-pages cap can fetch a very large number of "
                "pages; consider setting --max-pages and/or --max-pages-per-domain."
            )
            LOGGER.warning(message)
            reporter.warn(message)
        LOGGER.info(
            "Starting crawl: %d seed URL(s), max-depth %d, scope %s",
            len(config.urls),
            config.max_depth,
            config.crawl_scope,
        )
        return asyncio.run(run(config, reporter))
    except KeyboardInterrupt:
        console.stop_status()
        LOGGER.warning("Interrupted")
        reporter.warn("interrupted")
        return 130
    except Exception as exc:
        console.stop_status()
        LOGGER.error("%s", exc)
        if use_ui:
            reporter.error(str(exc))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
