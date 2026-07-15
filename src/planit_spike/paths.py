"""Filesystem path sanitization and output-dir construction.

A concern distinct from portal detection: this module only makes strings safe
as path segments and assembles a job's output directory.
"""

from __future__ import annotations

import re
from pathlib import Path

_UID_SANITISE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_path_part(s: str) -> str:
    """Make a string safe as one filesystem path segment (e.g. '2025/0335' -> '2025_0335')."""
    return _UID_SANITISE.sub("_", s).strip("_") or "unknown"


def job_output_dir(root: Path, council: str, uid: str) -> Path:
    """Return root/{safe council}/{safe uid} for a job's output files."""
    return root / safe_path_part(council) / safe_path_part(uid)
