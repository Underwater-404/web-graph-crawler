# Web Graph Crawler

Dork-driven, browser-rendered hyperlink collector for web-graph and OSINT
research — with an interactive terminal UI.

Give it **search dorks** (queries with operators like `site:`, `inurl:`,
`intitle:`, `filetype:`). It turns each dork into result URLs through a
pluggable search backend, opens every page in a real browser engine
(Playwright), waits for dynamic content, scrolls through lazy-loaded sections,
extracts links from the rendered DOM, classifies them as internal or external,
and appends rows to CSV. It can optionally follow those links to build a
multi-hop graph.

It is designed for consent-based and polite crawling: it respects `robots.txt`
by default, rate-limits per host, persists cookies/local storage between runs,
and does no anti-bot bypass or stealth fingerprint spoofing.

```
··· Discovering links · 2 dork(s) · duckduckgo ···
  ✓ [1/2] +7 links  (7 total)  site:python.org inurl:downloads
  ✓ [2/2] +9 links  (16 total)  "web scraping" site:github.com
  • grabbed 10 link(s) (from 16 found)

··· Scraping · 10 page(s) · depth 0 · scope seed-hosts ···
  ✓ https://www.python.org/downloads/          128 links · 128 rows · depth 0
  ✓ https://github.com/topics/web-scraping       94 links · 94 rows · depth 0
  ⠹ #3 scraping  https://github.com/topics/scraping
```

## Requirements

- Linux (primary target; also runs on macOS and Windows)
- Python 3.10+
- Playwright's Chromium (installed by `install.sh`)

## Install (Linux)

```bash
git clone https://github.com/<you>/web-graph-crawler.git
cd web-graph-crawler
./install.sh
source .venv/bin/activate
```

`install.sh` creates a virtualenv, installs the package (`pip install -e .`), and
downloads Chromium (`playwright install --with-deps chromium`). The
`--with-deps` step needs `sudo` on most distros; if it fails, the script falls
back to a browser-only install and prints the `playwright install-deps` command
to run manually.

Manual install, if you prefer:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .                       # or: pip install -r requirements.txt
python -m playwright install chromium
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
python -m playwright install chromium
```
</details>

Installing the package adds two commands to your `PATH`: `web-graph-crawler` and
`web-graph-styles`. Everything below also works as `python3 -m web_graph_crawler`.

## Quick start

### Interactive mode

Run it with no arguments and it walks you through it — enter dorks (or paste
URLs), pick a search engine, depth, and output, then watch it work live:

```bash
web-graph-crawler
```

### From dorks (no API key)

The default search provider is DuckDuckGo, which needs no key:

```bash
# Inline dorks (repeat --dork as many times as you like)
web-graph-crawler --dork "site:python.org inurl:downloads" --out data/links.csv

# Dorks from a file (one per line; see dorks.example.txt)
web-graph-crawler --dorks dorks.example.txt --out data/links.csv
```

Each dork is searched, the result URLs are de-duplicated and capped, and every
selected page is rendered and mined for links. Save the discovered seed list for
review or reuse with `--discovered-urls-out`:

```bash
web-graph-crawler --dorks dorks.txt --discovered-urls-out data/seeds.txt
```

> DuckDuckGo rate-limits aggressive automated use. If a run reports zero links,
> that is almost always temporary throttling, not "no matches" — wait a minute,
> lower `--results-per-dork`, or switch `--search-provider` (below). The crawler
> automatically retries via DuckDuckGo's `lite` endpoint before giving up.

## Search providers

Pick a backend with `--search-provider`. Keys can be passed as flags or via
environment variables.

| Provider     | Flag value    | Auth needed                          | Notes |
|--------------|---------------|--------------------------------------|-------|
| DuckDuckGo   | `duckduckgo`  | none                                 | Default. Keyless HTTP endpoint; great for light use but **rate-limits/blocks aggressively** (per-IP) under automation. |
| Common Crawl | `commoncrawl` | none                                 | Keyless, **never bans** (static dataset, not a live engine). Queries the open web index. Best for `site:domain` + `filetype:` dorks. |
| Browser      | `browser`     | none                                 | Keyless. Drives the installed Chromium like a real user; defaults to **Bing** via `--browser-engine`. Most block-resistant keyless option and handles `filetype:`/`intitle:`. |
| SearXNG      | `searxng`     | `--searxng-url` (or `SEARXNG_URL`)   | Best for large sweeps; point at your own instance. |
| Serper       | `serper`      | `--serper-api-key` / `SERPER_API_KEY` | **Google's actual results** via a SERP API — the service scrapes Google, so your IP never gets banned/captcha'd. Free tier ~2,500 queries. Full operator support. |
| Brave        | `brave`       | `--brave-api-key` / `BRAVE_SEARCH_API_KEY` | Free tier, real operator support. |
| Google CSE   | `google`      | `--google-api-key` + `--google-cx`   | Google Programmable Search JSON API (100/day free). |
| Bing         | `bing`        | `--bing-api-key`                     | Legacy. Microsoft retired the public API in Aug 2025; use only against a private/compatible endpoint. |

### Keyless browser search (recommended for real dorking)

The `duckduckgo` provider hits a lightweight HTTP endpoint that throttles fast —
on a shared/NAT IP you can get zero results (that is rate-limiting, not "no
matches"). The `browser` provider instead drives the Chromium you already
installed, so it looks like a human session, survives those blocks, and can pull
from **Bing** (a different service, unaffected by DuckDuckGo throttling):

```bash
# Broad, un-targeted dorking via Bing (no site: needed, no API key)
web-graph-crawler --search-provider browser --dork "intitle:index.of filetype:pdf"
web-graph-crawler --search-provider browser --dorks mydorks.txt --out data/links.csv

# Or drive DuckDuckGo's normal site in the browser instead of Bing
web-graph-crawler --search-provider browser --browser-engine duckduckgo --dork "inurl:blog"
```

It runs the search headless regardless of `--headful`. Discovery happens before
the crawl, so this adds ~1–2 s per dork to launch the browser.

### Common Crawl (keyless, never bans)

Live engines throttle/captcha automated searching. Common Crawl is a **static
open index of the web** — you query it over plain HTTP, so it can't ban you and
needs no key. The provider maps your dork onto a CDX query:

```bash
web-graph-crawler --search-provider commoncrawl --dork "site:example.com filetype:pdf"
web-graph-crawler --search-provider commoncrawl --dork "site:target.org inurl:admin" --out data/links.csv
web-graph-crawler --search-provider commoncrawl --dorks dorks.txt --cc-max-records 20000
```

What it does well vs. not:

- **Great:** `site:domain filetype:<pdf|doc|xls|ppt|csv|xml|json|zip|img…>` — the
  filetype becomes a server-side MIME filter (fast, complete).
- **Good:** `site:domain inurl:substring` — matched client-side over a
  `collapse=urlkey` slice; raise `--cc-max-records` (default 10000) for deeper
  coverage of large sites.
- **Weak:** whole-TLD dorks (`site:.de …`) and script-extension + query-param
  hunting (`filetype:php inurl:"?id="`). The index is host-anchored and only
  filters server-side on metadata, so a TLD query only returns the alphabetical
  slice. For that style, use the Brave API (real operator support) instead.
- **Unsupported:** dorks with no `site:` anchor, and `intitle:`/`intext:`
  (the URL index has no page text) — these are skipped with a warning.

`--cc-index CC-MAIN-2024-33` pins a specific crawl (default: latest).

### Google results without the bans (Serper)

Scraping Google/Bing yourself always ends in captchas. A **SERP API** returns
Google's real results while the service takes the scraping/ban risk — you just
call a clean API. `serper` (https://serper.dev, free tier ~2,500 queries) gives
full Google operator support (`site:`/`inurl:`/`filetype:`/`intitle:`), which is
what heavy dork lists actually need:

```bash
export SERPER_API_KEY="your-key"
web-graph-crawler --search-provider serper --dorks dorks.txt --out data/links.csv
```

This is the reliable path for whole-TLD / `inurl:"?id=" filetype:php` style
sweeps that keyless sources can't do. Google's own `--search-provider google`
(CSE) is the official alternative (100 queries/day free).

**Free-tier note:** Serper's free plan returns 10 results per request (`num` > 10
is paid), so the provider paginates in 10s. Each page = 1 credit, so
`--results-per-dork` sets the credit cost per dork: `10` = 1 credit/dork,
`50` = 5. With ~2,500 free credits, `--results-per-dork 10` on 108 dorks is
~108 credits (≈23 full runs); the default `50` is ~540/run.

Examples:

```bash
# SearXNG instance
web-graph-crawler --dorks dorks.txt --search-provider searxng \
  --searxng-url https://searx.example.org

# Brave
export BRAVE_SEARCH_API_KEY="your-key"
web-graph-crawler --dork "site:gov.uk filetype:pdf" --search-provider brave

# Google Programmable Search
web-graph-crawler --dork "intitle:report site:un.org" --search-provider google \
  --google-api-key KEY --google-cx CX
```

Discovery controls: `--results-per-dork` (default 20), `--search-delay-min` /
`--search-delay-max` (pacing between queries), `--search-proxy` (route search
requests through a proxy independent of page visits).

## How many links, dedup, and filtering

- **How many** per dork: `--results-per-dork` (default **50**). Engines cap the
  real maximum per query (Bing ~50–100), so for a bigger sweep use **more
  dorks** rather than only raising this. Total across all dorks is uncapped at
  depth 0 unless you set `--max-pages`.
- **De-duplication** is automatic: discovered links are collapsed by a key that
  ignores `http`/`https`, a leading `www.`, and trailing slashes (so the same
  page isn't crawled twice). A different query string still counts as a
  different page.
- **Famous sites are dropped by default** — Google, YouTube, Facebook,
  Instagram, X/Twitter, LinkedIn, Reddit, Wikipedia, Amazon, Apple, Microsoft,
  and other mainstream platforms (see `filters.py`). Dork sweeps usually want the
  long tail, not these.
  - `--include-famous` — keep them.
  - `--exclude-domains a.com,b.org` — drop extra domains of your own (matches
    subdomains too).
- `--max-pages-per-domain N` also caps how many results a single domain can
  contribute, so one big site can't dominate the seed set.

```bash
# 80 results/dork, drop mainstream sites + two of your own, one page per domain
web-graph-crawler --search-provider browser --dorks dorks.example.txt \
  --results-per-dork 80 --exclude-domains stackoverflow.com,github.io \
  --max-pages-per-domain 1 --out data/links.csv
```

## Crawling URLs directly

Search discovery is optional. You can still pass URLs or a URL file:

```bash
web-graph-crawler https://example.org https://www.python.org --out data/links.csv
web-graph-crawler --urls-file urls.txt --out data/links.csv
```

Dorks, positional URLs, and `--urls-file` can be combined; the seed set is
de-duplicated across all sources.

## Following links (multi-hop graph)

By default the crawler is depth-0: it mines links from the seed/discovered pages
only. Raise `--max-depth` to follow links and build a larger graph.

```bash
web-graph-crawler --dorks dorks.txt --max-depth 2 --max-pages 500
```

- `--max-depth N` — how many hops to follow (0 = seeds only).
- `--crawl-scope` — which links are eligible to follow when depth > 0:
  - `seed-hosts` (default): only hosts present in the seed set.
  - `same-host`: only links on the same host as the page they were found on.
  - `any`: follow any http(s) link (broad; use a cap).
- `--max-pages N` — total pages to fetch (0 = no limit).
- `--max-pages-per-domain N` — per-domain fetch cap (0 = no limit).

When depth > 0, always set `--max-pages` and/or `--max-pages-per-domain` to bound
the crawl.

## Terminal UI

The interactive UI (spinner, live status, coloured summary) is on by default
whenever stdout is a terminal. It is pure standard library — no `rich`/`curses`
— and degrades to plain lines when piped or redirected.

- `--no-ui` — plain streaming log lines instead of the live UI (good for cron/CI).
- `--no-color` — disable ANSI colours (also honours the `NO_COLOR` env var).
- `--no-input` — never prompt; error out if no dorks/URLs were given.

When the UI is active, full logs still go to the log file (`data/crawler.log` by
default; disable with `--no-log-file`). With `--no-ui`, logs stream to stdout as
before.

```bash
web-graph-crawler --dorks dorks.txt --no-ui --no-log-file   # plain, no file
web-graph-crawler --headful https://example.org             # visible browser
```

## Proxies (rotation)

Rotate a pool of proxies across **both** discovery and the crawl to dodge
per-IP throttling/bans. Put one proxy per line in a file (scheme-prefixed,
auto-detected; bare `host:port` is assumed `http`):

```text
# proxies.txt
socks5://127.0.0.1:9050         # Tor / no-auth SOCKS5
socks5://10.0.0.9:1080
http://user:pass@1.2.3.4:8080   # HTTP(S) may carry auth
9.9.9.9:8000                    # bare = http://
```

```bash
web-graph-crawler --search-provider commoncrawl --dorks dorks.txt \
  --proxies proxies.txt --out data/links.csv
```

A proxy is chosen at random per request/page; one that errors is marked dead and
skipped (the pool resets if all die). Notes:

- **SOCKS needs PySocks** for the discovery client: `pip install -e '.[socks]'`
  (or `pip install PySocks`). HTTP/HTTPS proxies need nothing extra.
- **Auth:** HTTP/HTTPS proxies support `user:pass`. The crawl uses Chromium,
  which does **not** support authentication on SOCKS proxies — use no-auth
  SOCKS5 (e.g. Tor) or HTTP/HTTPS-with-auth for the crawl.
- Not needed for `serper`/`brave`/`google` (real APIs don't ban); most useful for
  the keyless scrapers (`duckduckgo`/`browser`/`commoncrawl`) and the crawl.
- `--proxy` still sets a single proxy; `--proxies` (a pool) takes precedence.

## Project layout

- `web_graph_crawler.py` / `collect_link_styles.py` — compatibility launchers.
- `web_graph_crawler/cli.py` — argument parsing, the interactive wizard, dork discovery, and crawl orchestration.
- `web_graph_crawler/ui.py` — stdlib terminal UI (colours, spinner, progress rendering).
- `web_graph_crawler/progress.py` — the reporter interface the engine emits events to.
- `web_graph_crawler/search_providers.py` — pluggable search backends (DuckDuckGo/SearXNG/Brave/Google/Bing).
- `web_graph_crawler/search_discovery.py` — dork loading and discovery orchestration (dedup, caps, pacing).
- `web_graph_crawler/browser.py` — Playwright rendering, waits, scrolling, and DOM extraction.
- `web_graph_crawler/links.py` — URL normalization, link classification, and record creation.
- `web_graph_crawler/politeness.py` — `robots.txt` checks and per-domain rate limiting.
- `web_graph_crawler/output.py` — CSV writing.
- `web_graph_crawler/config.py` — typed crawler configuration and input parsing.

## Link style dataset collector

`web-graph-styles` discovers candidate pages with the same dork/provider system,
renders each discovered page with Playwright, extracts every rendered `a[href]`,
and records computed styles from `getComputedStyle()`.

```bash
# Keyless discovery via DuckDuckGo
web-graph-styles --dorks dorks.example.txt --output data/styles.csv --max-pages 200

# Or a specific provider
web-graph-styles --dorks dorks.txt --search-provider brave --output data/styles.csv

# Or bypass discovery with a curated URL list
web-graph-styles --seed-urls urls.example.txt --output data/styles.csv
```

Output columns: `source_url`, `link_url`, `link_text`, `link_order`, `color`,
`font_size`, `font_weight`, `text_decoration`, `background_color`, `border`,
`is_external`. `--queries` is accepted as an alias of `--dorks`;
`--max-pages-per-domain` defaults to `10`.

## CSV columns (link graph)

- `timestamp`, `source_url`, `final_source_url`, `link_url`, `link_type`,
  `link_text`, `element_tag`, `source_attribute`, `x`, `y`, `width`, `height`

By default, repeated link occurrences are preserved because their positions and
labels can matter for graph analysis. Add `--dedupe-links` for one row per
resolved link URL per source page.

## Politeness & robots.txt

- `robots.txt` is respected by default. Following RFC 9309:
  - a `4xx` robots.txt (e.g. 404) is treated as **allow-all** (no rules published);
  - a `5xx` or network failure is treated as **unreachable** and the origin is skipped.
- Use `--ignore-robots` only when you have explicit permission or a controlled
  research target.
- Cookies and local storage are saved to `data/storage_state.json` by default.
- Non-HTTP links such as `mailto:` and `tel:` are skipped unless
  `--include-non-http` is provided.
- Only the standard library is used for search discovery and the terminal UI, so
  Playwright remains the single third-party dependency.
```
