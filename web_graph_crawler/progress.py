"""Progress-reporter interface shared by the crawler and the terminal UI.

The engine (discovery + crawl) emits *events* to a :class:`Reporter`. The base
class is a silent no-op, so the core logic never depends on the terminal UI and
stays fully testable. :mod:`web_graph_crawler.ui` provides the interactive
implementation; anything else can subclass this to render events differently.
"""

from __future__ import annotations


class Reporter:
    """Silent reporter. Subclass and override to render progress."""

    # -- discovery -------------------------------------------------------
    def discovery_start(self, total_dorks: int, provider: str) -> None: ...

    def dork_start(self, index: int, total: int, dork: str) -> None: ...

    def dork_result(
        self, index: int, total: int, dork: str, new_count: int, total_count: int
    ) -> None: ...

    def discovery_done(self, selected: int, discovered: int) -> None: ...

    # -- crawl -----------------------------------------------------------
    def crawl_start(self, seeds: int, max_depth: int, scope: str) -> None: ...

    def page_start(self, url: str, depth: int, index: int, planned_total: int) -> None: ...

    def page_retry(self, url: str, attempt: int, max_attempts: int, delay: float) -> None: ...

    def page_done(self, url: str, rows: int, links: int, depth: int) -> None: ...

    def page_failed(self, url: str, error: str) -> None: ...

    def page_skipped(self, url: str, reason: str) -> None: ...

    def queued(self, added: int, depth: int) -> None: ...

    def crawl_done(self, stats: dict) -> None: ...

    # -- generic ---------------------------------------------------------
    def info(self, message: str) -> None: ...

    def warn(self, message: str) -> None: ...

    def error(self, message: str) -> None: ...


#: Convenience singleton for "report nothing".
NULL_REPORTER = Reporter()
