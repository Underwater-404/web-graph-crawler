"""CSV output sink."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable

from .constants import CSV_COLUMNS
from .links import LinkRecord


class CsvSink:
    """Append-only CSV writer with a stable schema."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        exists_with_content = self.path.exists() and self.path.stat().st_size > 0
        self._file = self.path.open("a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=CSV_COLUMNS)
        if not exists_with_content:
            self._writer.writeheader()
            self._file.flush()

    def write_many(self, records: Iterable[LinkRecord]) -> int:
        count = 0
        for record in records:
            self._writer.writerow(record.as_row())
            count += 1
        self._file.flush()
        return count

    def close(self) -> None:
        self._file.close()
