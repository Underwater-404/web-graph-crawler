"""CSV records for computed hyperlink styles."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


STYLE_CSV_COLUMNS = [
    "source_url",
    "link_url",
    "link_text",
    "link_order",
    "color",
    "font_size",
    "font_weight",
    "text_decoration",
    "background_color",
    "border",
    "is_external",
]


@dataclass
class LinkStyleRecord:
    source_url: str
    link_url: str
    link_text: str
    link_order: int
    color: str
    font_size: str
    font_weight: str
    text_decoration: str
    background_color: str
    border: str
    is_external: bool

    def as_row(self) -> dict[str, Any]:
        return {
            "source_url": self.source_url,
            "link_url": self.link_url,
            "link_text": self.link_text,
            "link_order": self.link_order,
            "color": self.color,
            "font_size": self.font_size,
            "font_weight": self.font_weight,
            "text_decoration": self.text_decoration,
            "background_color": self.background_color,
            "border": self.border,
            "is_external": str(self.is_external).lower(),
        }


class StyleCsvSink:
    """Append-only CSV writer for computed link styles."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        exists_with_content = self.path.exists() and self.path.stat().st_size > 0
        self._file = self.path.open("a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=STYLE_CSV_COLUMNS)
        if not exists_with_content:
            self._writer.writeheader()
            self._file.flush()

    def write_many(self, records: Iterable[LinkStyleRecord]) -> int:
        count = 0
        for record in records:
            self._writer.writerow(record.as_row())
            count += 1
        self._file.flush()
        return count

    def close(self) -> None:
        self._file.close()
