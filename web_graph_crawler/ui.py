"""Interactive terminal UI: colours, a live spinner, and progress rendering.

Pure standard library (no ``rich``/``curses``), so it runs on any Linux
terminal with zero extra dependencies. When stdout is not a TTY (pipes, CI,
log files) it degrades to plain, un-animated lines automatically.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from typing import TextIO

from .progress import Reporter

# --------------------------------------------------------------------------- #
# Capability detection
# --------------------------------------------------------------------------- #

_ANSI = {
    "reset": 0, "bold": 1, "dim": 2, "italic": 3, "underline": 4,
    "red": 31, "green": 32, "yellow": 33, "blue": 34,
    "magenta": 35, "cyan": 36, "white": 37, "gray": 90,
}


def _enable_windows_ansi() -> None:
    """Best-effort enable of ANSI escape processing on legacy Windows consoles."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        for handle_id in (-11, -12):  # STDOUT, STDERR
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def supports_color(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return True


def _supports_unicode(stream: TextIO) -> bool:
    enc = (getattr(stream, "encoding", "") or "").lower()
    return "utf" in enc


# --------------------------------------------------------------------------- #
# Console: thread-safe output with an optional live spinner line
# --------------------------------------------------------------------------- #

class Symbols:
    def __init__(self, unicode_ok: bool) -> None:
        if unicode_ok:
            self.ok, self.bad, self.arrow = "✓", "✗", "↳"
            self.bullet, self.skip, self.dot = "•", "»", "·"
            self.frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        else:
            self.ok, self.bad, self.arrow = "+", "x", "->"
            self.bullet, self.skip, self.dot = "*", ">>", "."
            self.frames = "|/-\\"


class Console:
    """Minimal thread-safe console with a single animated status line."""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        color: bool | None = None,
        interactive: bool | None = None,
    ) -> None:
        self.stream = stream or sys.stdout
        _enable_windows_ansi()
        self.color = supports_color(self.stream) if color is None else color
        tty = hasattr(self.stream, "isatty") and self.stream.isatty()
        self.interactive = tty if interactive is None else (interactive and tty)
        self.sym = Symbols(_supports_unicode(self.stream))

        self._lock = threading.RLock()
        self._spinning = False
        self._status = ""
        self._frame = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- styling -------------------------------------------------------------
    def paint(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        codes = "".join(f"\033[{_ANSI[s]}m" for s in styles if s in _ANSI)
        return f"{codes}{text}\033[0m"

    def _width(self) -> int:
        return shutil.get_terminal_size(fallback=(80, 24)).columns

    # -- spinner -------------------------------------------------------------
    def _draw(self) -> None:
        if not (self.interactive and self._spinning):
            return
        frame = self.sym.frames[self._frame % len(self.sym.frames)]
        line = f"{self.paint(frame, 'cyan')} {self._status}"
        budget = max(10, self._width() - 1)
        self.stream.write("\r\033[2K" + _fit(line, budget, self.color))
        self.stream.flush()

    def _spin(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                self._draw()
                self._frame += 1
            self._stop.wait(0.09)

    def start_status(self, text: str) -> None:
        with self._lock:
            self._status = text
            if not self.interactive:
                return
            if self._spinning:
                self._draw()
                return
            self._spinning = True
            self._stop.clear()
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()

    def set_status(self, text: str) -> None:
        with self._lock:
            self._status = text
            self._draw()

    def stop_status(self) -> None:
        with self._lock:
            was = self._spinning
            self._spinning = False
        if was:
            self._stop.set()
            if self._thread:
                self._thread.join(timeout=0.5)
            with self._lock:
                self.stream.write("\r\033[2K")
                self.stream.flush()

    # -- output --------------------------------------------------------------
    def log(self, text: str = "") -> None:
        """Print a permanent line, cleanly above any active spinner."""
        with self._lock:
            if self.interactive and self._spinning:
                self.stream.write("\r\033[2K")
            self.stream.write(text + "\n")
            if self.interactive and self._spinning:
                self._draw()
            self.stream.flush()

    def rule(self, title: str) -> None:
        bar = self.sym.dot * 3
        self.log("")
        self.log(self.paint(f"{bar} {title} {bar}", "bold", "cyan"))


def _visible_len(text: str) -> int:
    out, i = 0, 0
    while i < len(text):
        if text[i] == "\033":
            j = text.find("m", i)
            if j == -1:
                break
            i = j + 1
            continue
        out += 1
        i += 1
    return out


def _fit(text: str, width: int, has_ansi: bool) -> str:
    """Truncate to ``width`` visible columns, preserving a trailing reset."""
    if not has_ansi:
        return text if len(text) <= width else text[: width - 1] + "…"
    if _visible_len(text) <= width:
        return text
    out, count, i = [], 0, 0
    while i < len(text) and count < width - 1:
        if text[i] == "\033":
            j = text.find("m", i)
            if j == -1:
                break
            out.append(text[i : j + 1])
            i = j + 1
            continue
        out.append(text[i])
        count += 1
        i += 1
    out.append("…\033[0m")
    return "".join(out)


def shorten_url(url: str, maxlen: int = 72) -> str:
    if len(url) <= maxlen:
        return url
    keep = maxlen - 1
    head = keep * 3 // 5
    tail = keep - head
    return url[:head] + "…" + url[-tail:]


# --------------------------------------------------------------------------- #
# TerminalReporter: renders engine events
# --------------------------------------------------------------------------- #

class TerminalReporter(Reporter):
    def __init__(self, console: Console) -> None:
        self.c = console
        s = console.sym
        self._ok = console.paint(s.ok, "green")
        self._bad = console.paint(s.bad, "red")
        self._skip = console.paint(s.skip, "yellow")
        self._arrow = s.arrow

    # -- discovery -----------------------------------------------------------
    def discovery_start(self, total_dorks: int, provider: str) -> None:
        sep = self.c.sym.dot
        self.c.rule(
            f"Discovering links {sep} {total_dorks} dork(s) {sep} {self.c.paint(provider, 'magenta')}"
        )

    def dork_start(self, index: int, total: int, dork: str) -> None:
        tag = self.c.paint(f"[{index}/{total}]", "dim")
        self.c.start_status(f"{tag} searching  {self.c.paint(dork, 'white')}")

    def dork_result(self, index: int, total: int, dork: str, new_count: int, total_count: int) -> None:
        self.c.stop_status()
        tag = self.c.paint(f"[{index}/{total}]", "dim")
        count = self.c.paint(f"+{new_count}", "green" if new_count else "dim")
        running = self.c.paint(f"{total_count} total", "dim")
        self.c.log(f"  {self._ok} {tag} {count} links  ({running})  {self.c.paint(dork, 'dim')}")

    def discovery_done(self, selected: int, discovered: int) -> None:
        self.c.log(
            f"  {self.c.paint(self.c.sym.bullet, 'cyan')} grabbed "
            f"{self.c.paint(str(selected), 'bold', 'green')} link(s) "
            f"{self.c.paint(f'(from {discovered} found)', 'dim')}"
        )

    # -- crawl ---------------------------------------------------------------
    def crawl_start(self, seeds: int, max_depth: int, scope: str) -> None:
        sep = self.c.sym.dot
        self.c.rule(
            f"Scraping {sep} {seeds} page(s) {sep} depth {max_depth} {sep} "
            f"scope {self.c.paint(scope, 'magenta')}"
        )

    def _counter(self, index: int, planned_total: int) -> str:
        text = f"{index}/{planned_total}" if planned_total and index <= planned_total else f"#{index}"
        return self.c.paint(text, "dim")

    def page_start(self, url: str, depth: int, index: int, planned_total: int) -> None:
        short = shorten_url(url)
        counter = self._counter(index, planned_total)
        if self.c.interactive:
            self.c.start_status(f"{counter} scraping  {self.c.paint(short, 'white')}")
        else:
            self.c.log(f"  {self._arrow} {counter} scraping  {short}")

    def page_retry(self, url: str, attempt: int, max_attempts: int, delay: float) -> None:
        msg = f"retry {attempt}/{max_attempts} in {delay:.1f}s"
        if self.c.interactive:
            self.c.set_status(f"{self.c.paint(msg, 'yellow')}  {self.c.paint(shorten_url(url), 'dim')}")
        else:
            self.c.log(f"    {self.c.paint(msg, 'yellow')}  {shorten_url(url)}")

    def page_done(self, url: str, rows: int, links: int, depth: int) -> None:
        self.c.stop_status()
        sep = self.c.sym.dot
        detail = self.c.paint(f"{links} links {sep} {rows} rows {sep} depth {depth}", "dim")
        self.c.log(f"  {self._ok} {self.c.paint(shorten_url(url), 'white')}  {detail}")

    def page_failed(self, url: str, error: str) -> None:
        self.c.stop_status()
        self.c.log(f"  {self._bad} {self.c.paint(shorten_url(url), 'white')}  {self.c.paint(error, 'red')}")

    def page_skipped(self, url: str, reason: str) -> None:
        self.c.stop_status()
        self.c.log(f"  {self._skip} {self.c.paint(shorten_url(url), 'dim')}  {self.c.paint(reason, 'dim')}")

    def queued(self, added: int, depth: int) -> None:
        if added:
            self.c.log(self.c.paint(f"      {self._arrow} queued {added} new link(s) for depth {depth}", "dim"))

    def crawl_done(self, stats: dict) -> None:
        self.c.rule("Summary")
        rows = [
            ("fetched", stats.get("fetched", 0), "white"),
            ("succeeded", stats.get("success", 0), "green"),
            ("failed", stats.get("failure", 0), "red" if stats.get("failure") else "dim"),
            ("skipped", stats.get("skipped", 0), "yellow" if stats.get("skipped") else "dim"),
            ("link rows", stats.get("rows", 0), "cyan"),
        ]
        for label, value, color in rows:
            self.c.log(f"  {label:<11} {self.c.paint(str(value), 'bold', color)}")
        out = stats.get("output")
        if out:
            self.c.log(f"  {'output':<11} {self.c.paint(str(out), 'underline')}")

    # -- generic -------------------------------------------------------------
    def info(self, message: str) -> None:
        self.c.log(f"  {self.c.paint(self.c.sym.dot, 'dim')} {message}")

    def warn(self, message: str) -> None:
        self.c.stop_status()
        self.c.log(f"  {self.c.paint('!', 'yellow')} {self.c.paint(message, 'yellow')}")

    def error(self, message: str) -> None:
        self.c.stop_status()
        self.c.log(f"  {self._bad} {self.c.paint(message, 'red')}")


def make_console(no_color: bool = False, no_ui: bool = False, stream: TextIO | None = None) -> Console:
    return Console(
        stream,
        color=False if no_color else None,
        interactive=False if no_ui else None,
    )
