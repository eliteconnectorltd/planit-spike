"""Unit tests for filesystem path sanitization."""

from __future__ import annotations

from pathlib import Path

from planit_spike.paths import job_output_dir, safe_path_part


def test_safe_path_part_slash_to_underscore():
    assert safe_path_part("2025/0335") == "2025_0335"


def test_safe_path_part_preserves_allowed_chars():
    # dots, hyphens, underscores survive; slashes and colons don't
    assert safe_path_part("PA26/03485") == "PA26_03485"
    assert safe_path_part("CA-26.0084_x") == "CA-26.0084_x"


def test_safe_path_part_strips_edges():
    assert safe_path_part("/2025/0335/") == "2025_0335"


def test_safe_path_part_empty_fallback():
    assert safe_path_part("") == "unknown"
    assert safe_path_part("///") == "unknown"


def test_job_output_dir_structure():
    out = job_output_dir(Path("output"), "Bradford", "26/02066/FUL")
    assert out == Path("output") / "Bradford" / "26_02066_FUL"
