"""Rendered-DOM link records, normalization, and classification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse


@dataclass
class LinkRecord:
    timestamp: str
    source_url: str
    final_source_url: str
    link_url: str
    link_type: str
    link_text: str
    element_tag: str
    source_attribute: str
    x: int | None
    y: int | None
    width: int | None
    height: int | None

    def as_row(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "source_url": self.source_url,
            "final_source_url": self.final_source_url,
            "link_url": self.link_url,
            "link_type": self.link_type,
            "link_text": self.link_text,
            "element_tag": self.element_tag,
            "source_attribute": self.source_attribute,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }


def build_link_records(
    source_url: str,
    final_source_url: str,
    raw_links: list[dict[str, Any]],
    include_non_http: bool,
    dedupe_links: bool,
) -> list[LinkRecord]:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    records: list[LinkRecord] = []
    seen: set[str] = set()

    for raw in raw_links:
        absolute_url = normalize_link_url(final_source_url, str(raw.get("url", "")))
        if not absolute_url:
            continue

        scheme = urlparse(absolute_url).scheme.lower()
        if scheme not in {"http", "https"} and not include_non_http:
            continue

        if dedupe_links:
            if absolute_url in seen:
                continue
            seen.add(absolute_url)

        records.append(
            LinkRecord(
                timestamp=timestamp,
                source_url=source_url,
                final_source_url=final_source_url,
                link_url=absolute_url,
                link_type=classify_link(final_source_url, absolute_url),
                link_text=str(raw.get("text") or ""),
                element_tag=str(raw.get("tag") or ""),
                source_attribute=str(raw.get("attr") or ""),
                x=to_int_or_none(raw.get("x")),
                y=to_int_or_none(raw.get("y")),
                width=to_int_or_none(raw.get("width")),
                height=to_int_or_none(raw.get("height")),
            )
        )

    return records


def normalize_link_url(base_url: str, raw_url: str) -> str | None:
    raw_url = raw_url.strip()
    if not raw_url:
        return None

    lowered = raw_url.lower()
    if lowered.startswith(("javascript:", "data:", "blob:")):
        return None

    absolute = urljoin(base_url, raw_url)
    parsed = urlparse(absolute)
    if not parsed.scheme:
        return None

    clean_path = re.sub(r"[\r\n\t]", "", parsed.path)
    clean_query = re.sub(r"[\r\n\t]", "", parsed.query)
    clean_fragment = re.sub(r"[\r\n\t]", "", parsed.fragment)
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc,
            clean_path,
            parsed.params,
            clean_query,
            clean_fragment,
        )
    )


def classify_link(source_url: str, link_url: str) -> str:
    parsed_link = urlparse(link_url)
    if parsed_link.scheme.lower() not in {"http", "https"}:
        return "non-http"

    source_host = normalized_host(urlparse(source_url).hostname or "")
    link_host = normalized_host(parsed_link.hostname or "")
    if not source_host or not link_host:
        return "external"
    return "internal" if source_host == link_host else "external"


def normalized_host(host: str) -> str:
    host = host.lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def canonical_url(url: str) -> str:
    """Key used to de-duplicate visited pages: lowercase scheme/host, no fragment."""

    parsed = urlparse(url)
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path,
            parsed.params,
            parsed.query,
            "",
        )
    )


def to_int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
