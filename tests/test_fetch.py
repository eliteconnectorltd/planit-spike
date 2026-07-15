"""Unit tests for fetch._warn_if_suspicious (item-a advisory warnings)."""

from __future__ import annotations

import logging

from planit_spike.config import SMALL_FILE_WARN_BYTES
from planit_spike.fetch import _warn_if_suspicious
from planit_spike.types import FetchResult


def _result(byte_size: int) -> FetchResult:
    return FetchResult(
        council="Test", uid="1", kind="docs", url="https://x/",
        status_code=200, byte_size=byte_size,
    )


def test_denied_marker_warns_access_denied(caplog):
    body = "<html>Permission Denied</html>"
    with caplog.at_level(logging.WARNING):
        _warn_if_suspicious(body, _result(byte_size=50_000), logging.getLogger("t"))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "access-denied" in warnings[0].message


def test_small_body_warns_unusually_small(caplog):
    body = "x" * 100
    with caplog.at_level(logging.WARNING):
        _warn_if_suspicious(body, _result(byte_size=100), logging.getLogger("t"))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "unusually small" in warnings[0].message


def test_denied_wins_when_both_apply(caplog):
    # Denied marker AND under threshold: exactly one warning, denied wins.
    body = "Permission Denied"
    assert len(body.encode()) < SMALL_FILE_WARN_BYTES
    with caplog.at_level(logging.WARNING):
        _warn_if_suspicious(body, _result(byte_size=len(body)), logging.getLogger("t"))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "access-denied" in warnings[0].message
    assert "unusually small" not in warnings[0].message


def test_normal_body_no_warning(caplog):
    body = "x" * 50_000
    with caplog.at_level(logging.WARNING):
        _warn_if_suspicious(body, _result(byte_size=50_000), logging.getLogger("t"))
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
