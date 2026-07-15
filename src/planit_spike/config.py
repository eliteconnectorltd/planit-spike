"""Shared constants for planit-spike. Leaf module: no in-package imports.

Imported by fetch, runner, and cli so tuning defaults, headers, and marker
strings live in exactly one place.
"""

from __future__ import annotations

# --- HTTP tuning defaults ----------------------------------------------------

DEFAULT_TIMEOUT = 30.0           # seconds per HTTP request
DEFAULT_CONCURRENCY = 4          # simultaneous fetches
DEFAULT_PER_HOST_DELAY = 1.5     # seconds between consecutive fetches on same host
DEFAULT_MAX_RETRIES = 3          # retry attempts on connection errors
DEFAULT_RETRY_BACKOFF = 2.0      # base seconds for exponential backoff between retries

# --- Browser-like request headers -------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9,en-US;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# --- Fetch tabs -------------------------------------------------------------

FETCH_KINDS = ("details", "contacts", "dates", "docs")  # tabs fetched per job

# --- Suspicious-content warning thresholds (see fetch._warn_if_suspicious) ---

SMALL_FILE_WARN_BYTES = 15_000                       # warn below this saved size
DENIED_MARKERS = ("Permission Denied", "Access Denied")  # access-denied page markers

# --- Logging -----------------------------------------------------------------

LOGGER_NAME = "planit-spike"     # renamed from the spike's "planit-fetcher"
