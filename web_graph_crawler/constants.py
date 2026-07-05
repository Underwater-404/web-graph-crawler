"""Shared constants for crawler defaults and CSV schema."""

from pathlib import Path
import re

DEFAULT_STORAGE_STATE = Path("data/storage_state.json")
DEFAULT_OUTPUT = Path("data/links.csv")
DEFAULT_LOG_FILE = Path("data/crawler.log")

CSV_COLUMNS = [
    "timestamp",
    "source_url",
    "final_source_url",
    "link_url",
    "link_type",
    "link_text",
    "element_tag",
    "source_attribute",
    "x",
    "y",
    "width",
    "height",
]

COOKIE_TEXT_RE = re.compile(
    r"\b("
    r"accept all|accept|agree|allow all|i agree|ok|okay|got it|continue|"
    r"reject all|save choices|save preferences"
    r")\b",
    re.IGNORECASE,
)
