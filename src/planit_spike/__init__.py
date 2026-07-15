"""planit-spike: fetch raw HTML metadata for UK planning applications from Idox portals."""

from __future__ import annotations

from .planit_client import fetch_jobs
from .runner import run
from .types import FetchResult, JobResult

__version__ = "0.1.0"

__all__ = ["FetchResult", "JobResult", "fetch_jobs", "run", "__version__"]
