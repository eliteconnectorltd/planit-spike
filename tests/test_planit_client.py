"""Unit + MockTransport tests for the PlanIt discovery layer.

Offline against captured fixtures (PlanIt live access is WAF-blocked; a
@pytest.mark.live module is added when a whitelisted environment exists).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from planit_spike import planit_client
from planit_spike.planit_client import (
    PLANIT_LIST_URL,
    _normalize_record,
    _unwrap_envelope,
    fetch_jobs,
)

FIXTURES = Path(__file__).parent / "fixtures"
CREATED_LIST = json.loads(
    (FIXTURES / "sample_planit_created_list.json").read_text(encoding="utf-8")
)
DETAIL = json.loads(
    (FIXTURES / "sample_planit_detail.json").read_text(encoding="utf-8")
)


# --- _unwrap_envelope --------------------------------------------------------

def test_unwrap_envelope_dict():
    recs = _unwrap_envelope({"from": 0, "records": [{"uid": "a"}]})
    assert recs == [{"uid": "a"}]


def test_unwrap_envelope_bare_list():
    assert _unwrap_envelope([{"uid": "a"}]) == [{"uid": "a"}]


def test_unwrap_envelope_malformed_raises():
    with pytest.raises(ValueError):
        _unwrap_envelope("not an envelope")
    with pytest.raises(ValueError):
        _unwrap_envelope({"records": "not a list"})


# --- _normalize_record -------------------------------------------------------

def test_normalize_record_extracts_four_fields():
    # The detail fixture is a BARE record (no {records:[...]} wrapper) —
    # structurally identical to a list record (PLANIT_API_NOTES.md §5).
    record = DETAIL
    job = _normalize_record(record)
    assert job["uid"] == "2025/0335"
    assert job["council"] == "Wandsworth"
    assert job["docs_url"] == record["other_fields"]["docs_url"]
    # url is the council deep link, NOT the generic source_url search page.
    assert job["url"] == record["url"]
    assert job["url"] != record["other_fields"]["source_url"]


def test_normalize_record_missing_other_fields():
    job = _normalize_record({"uid": "x", "url": "u", "area_name": "C"})
    assert job["docs_url"] is None


# --- fetch_jobs dedup via MockTransport -------------------------------------

def _handler(request: httpx.Request) -> httpx.Response:
    # Serve the same 6-record fixture for BOTH created and updated feeds, so
    # fetch_jobs sees a full 6+6 uid overlap and must dedupe to 6.
    if request.url.path == "/api/applics/json":
        return httpx.Response(200, json=CREATED_LIST, request=request)
    return httpx.Response(404, request=request)


@pytest.mark.anyio
async def test_fetch_jobs_dedupes_overlap():
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        jobs = await fetch_jobs(client, "2025-06-30")
    uids = [j["uid"] for j in jobs]
    assert len(jobs) == 6                 # 12 fetched, deduped to 6 distinct uids
    assert len(set(uids)) == 6
    assert "2025/0335" in uids


@pytest.fixture
def anyio_backend():
    return "asyncio"
