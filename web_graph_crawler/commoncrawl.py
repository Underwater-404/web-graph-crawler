"""Keyless, ban-free discovery via the Common Crawl URL index.

Common Crawl publishes a CDX index of billions of crawled URLs. It is a static
dataset queried over plain HTTP — no live engine to captcha or ban you, no API
key. This provider maps dork operators onto a CDX query:

    site:example.com   -> url=*.example.com        (host + subdomains)
    site:example.com/x -> url=example.com/x*        (path prefix)
    site:.de           -> url=*.de                  (whole TLD; broad/capped)
    filetype:pdf       -> filter=mime:application/pdf   (server-side, efficient)
    filetype:php       -> keep URLs whose path ends .php (client-side; php is html)
    inurl:"?id="       -> keep URLs containing ?id=      (client-side)

The CDX index is anchored on the host, and it only filters server-side on
metadata fields (mime/status), NOT on the URL text. So a dork MUST carry a
``site:`` anchor, and ``inurl:`` / script-extension filetypes are matched
client-side over a ``collapse=urlkey`` slice (raise ``--cc-max-records`` for more
coverage). Operator-only dorks and ``intitle:``/``intext:`` can't be served and
are skipped with a warning.
"""

from __future__ import annotations

import json
import logging
import shlex
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse

from .search_providers import HttpClient, SearchError, SearchProvider, _is_http_url

LOGGER = logging.getLogger("web_graph_crawler.search")

COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"

# filetypes that map to a distinct MIME (filterable server-side, efficiently).
MIME_BY_EXT = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "rtf": "application/rtf",
    "csv": "text/csv",
    "txt": "text/plain",
    "xml": "application/xml",
    "json": "application/json",
    "zip": "application/zip",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "svg": "image/svg+xml",
}


def parse_dork(dork: str) -> tuple[str | None, list[str], str | None, list[str]]:
    """Split a dork into (site, inurl_terms, filetype, unsupported_tokens)."""
    site: str | None = None
    inurl: list[str] = []
    filetype: str | None = None
    unsupported: list[str] = []

    try:
        tokens = shlex.split(dork)
    except ValueError:
        tokens = dork.split()

    for token in tokens:
        low = token.lower()
        if low.startswith("site:"):
            site = token[5:].strip().strip('"') or None
        elif low.startswith("inurl:"):
            value = token[6:].strip().strip('"')
            if value:
                inurl.append(value)
        elif low.startswith("filetype:") or low.startswith("ext:"):
            value = token.split(":", 1)[1].strip().strip('"').lstrip(".").lower()
            filetype = value or None
        else:
            unsupported.append(token)

    return site, inurl, filetype, unsupported


def pattern_for_site(site: str) -> tuple[str, bool]:
    """Return (cdx_url_pattern, is_tld) for a ``site:`` value."""
    value = site.strip().strip('"').lower().split("//")[-1]
    if value.startswith("."):  # TLD, e.g. .de
        return "*" + value, True
    if "/" in value:  # host + path -> prefix match
        host, _, path = value.partition("/")
        if host.startswith("www."):
            host = host[4:]
        return f"{host}/{path}*", False
    host = value[4:] if value.startswith("www.") else value
    return "*." + host, False


def matches_filters(url: str, inurl_terms: list[str], suffix: str | None) -> bool:
    low = url.lower()
    for term in inurl_terms:
        if term.lower() not in low:
            return False
    if suffix and not urlparse(url).path.lower().endswith("." + suffix):
        return False
    return True


class CommonCrawlProvider(SearchProvider):
    """Query the Common Crawl CDX index for URLs matching a dork."""

    name = "commoncrawl"

    def __init__(
        self,
        client: HttpClient,
        index_id: str | None = None,
        max_index_records: int = 10000,
    ) -> None:
        super().__init__(client)
        self.index_id = index_id
        self.max_index_records = max(100, max_index_records)
        self._cdx_api: str | None = None

    def _resolve_cdx_api(self) -> str:
        if self._cdx_api:
            return self._cdx_api
        if self.index_id:
            self._cdx_api = f"https://index.commoncrawl.org/{self.index_id}-index"
            return self._cdx_api
        collections = self.client.get_json(COLLINFO_URL)
        if not isinstance(collections, list) or not collections:
            raise SearchError("Common Crawl: could not read collinfo.json")
        newest = collections[0]
        self._cdx_api = newest.get("cdx-api") or f"https://index.commoncrawl.org/{newest['id']}-index"
        LOGGER.info("Common Crawl index: %s", newest.get("id", self._cdx_api))
        return self._cdx_api

    def search(self, query: str, max_results: int) -> list[str]:
        site, inurl_terms, filetype, unsupported = parse_dork(query)
        if unsupported:
            LOGGER.debug("Common Crawl ignores non-URL terms: %s", " ".join(unsupported))
        if not site:
            LOGGER.warning(
                "Common Crawl needs a site: anchor in the dork (%r); skipping. Add "
                "site:example.com or site:.tld — the URL index can't serve operator-only dorks.",
                query,
            )
            return []

        pattern, is_tld = pattern_for_site(site)
        if is_tld:
            LOGGER.info(
                "Common Crawl TLD query %s is broad; you get the index-ordered slice "
                "(raise --cc-max-records for more).", pattern
            )

        want = max(1, max_results)
        mime = MIME_BY_EXT.get(filetype) if filetype else None
        # Script-extension filetypes (php/asp/...) are text/html, so match on path.
        suffix = filetype if (filetype and not mime) else None
        needs_client = bool(inurl_terms) or bool(suffix)
        if needs_client:
            limit = min(self.max_index_records, max(want * 20, 200))
        else:
            limit = min(self.max_index_records, max(want, 50))

        params = [
            ("url", pattern),
            ("output", "json"),
            ("collapse", "urlkey"),  # one row per unique URL, not per capture
            ("limit", str(limit)),
        ]
        if mime:
            params.append(("filter", f"mime:{mime}"))

        cdx_api = self._resolve_cdx_api()
        try:
            text = self.client.get_text(cdx_api + "?" + urlencode(params))
        except HTTPError as exc:
            if exc.code == 404:
                LOGGER.info("Common Crawl: no captures for %s", pattern)
                return []
            LOGGER.warning("Common Crawl request failed for %r: %s", query, exc)
            return []
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Common Crawl request failed for %r: %s", query, exc)
            return []

        results: list[str] = []
        seen: set[str] = set()
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            url = record.get("url") or record.get("original")
            if not _is_http_url(url) or url in seen:
                continue
            if not matches_filters(url, inurl_terms, suffix):
                continue
            seen.add(url)
            results.append(url)
            if len(results) >= want:
                break
        return results
