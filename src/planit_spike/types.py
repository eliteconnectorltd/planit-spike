"""Pure dataclasses describing fetch outcomes. No logic, no in-package imports."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional


@dataclass
class FetchResult:
    """Outcome of fetching one URL (one tab of an application)."""

    council: str
    uid: str
    kind: Literal["details", "docs", "contacts", "dates"]
    url: str
    status_code: Optional[int] = None
    saved_path: Optional[str] = None
    byte_size: Optional[int] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        """True when status is 200 and no error was recorded."""
        return self.status_code == 200 and self.error is None


@dataclass
class JobResult:
    """Outcome of processing one job.

    A job is 'supported' if it is an Idox portal (derive_urls returned URLs),
    regardless of how many of its fetches actually succeeded. Per-fetch
    success/failure lives in each FetchResult.ok — there is deliberately no
    'partial' flag. Downstream consumers (extractor, report) inspect each
    entry in `fetches` individually.

    `fetches` maps kind -> FetchResult, e.g. {"details": ..., "docs": ...,
    "contacts": ..., "dates": ...}. Empty for skipped (non-Idox) jobs.

    `documents` holds one DocumentFetchResult per document listed on the docs
    tab — empty for non-Idox jobs, for jobs whose docs.html fetch failed, and
    for jobs whose docs tab lists nothing. Each entry carries its own .ok;
    partial document failure is not job failure. `pagination_detected` is True
    when the docs tab showed paging controls, meaning `documents` may be
    incomplete (Phase 4 does not follow pagination).
    """

    council: str
    uid: str
    supported: bool
    fetches: dict[str, FetchResult] = field(default_factory=dict)
    documents: list[DocumentFetchResult] = field(default_factory=list)
    pagination_detected: bool = False
    skip_reason: Optional[str] = None
    events: list[str] = field(default_factory=list)


@dataclass
class DocumentFetchResult:
    """Outcome of downloading one document from an Idox docs table."""

    url: str
    saved_path: Optional[Path] = None
    byte_size: int = 0
    error: Optional[str] = None
    attempts: int = 0
    # download_status classifies the outcome for reporting. None for the common
    # cases (a success, or a plain failure carried in `error`); set to
    # "unavailable" when the council served an Idox "Document Unavailable" page
    # (HTTP 404 with an HTML body) — the document is genuinely not retrievable,
    # as distinct from a transient or unexplained 404. `notes` carries the
    # human-readable explanation for that case.
    download_status: Optional[str] = None
    notes: Optional[str] = None

    @property
    def ok(self) -> bool:
        """True when the file was written and no error was recorded."""
        return self.saved_path is not None and self.error is None
