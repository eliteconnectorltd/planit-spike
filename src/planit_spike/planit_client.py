"""PlanIt discovery layer: query the created + updated feeds, normalize, dedupe.

Talks to PlanIt's single list endpoint (GET /api/applics/json) twice with
different date-param pairs, unwraps the {from, records, total?} envelope, and
maps each record to a job dict the fetcher understands (uid, url, council,
docs_url). Returns ALL jobs regardless of portal family — portal filtering is
the fetcher's concern (derive_urls), not discovery's.

KNOWN_LIMITATIONS (Phase 1, deferred to Phase 2):
  - The 'updated' feed's changed_start/changed_end query params are UNVERIFIED
    against the live API (PLANIT_API_NOTES.md §3) — built to the documented
    shape, proven only against fixtures. This is the load-bearing gotcha:
    confirm with a @pytest.mark.live probe when a whitelisted environment is
    available.
  - No pagination: sends pg_sz=300&page=1 and reads a single page. If a feed
    exceeds 300 records the remainder is silently dropped. (Phase 2: page on
    envelope 'from'/'total'.)
  - No rate-limit/retry logic here: the caller's httpx.AsyncClient owns that.
  - No response validation beyond JSON parse + envelope unwrap.
"""

from __future__ import annotations

import httpx

PLANIT_BASE_URL = "https://www.planit.org.uk"
PLANIT_LIST_PATH = "/api/applics/json"
PLANIT_LIST_URL = f"{PLANIT_BASE_URL}{PLANIT_LIST_PATH}"

# Baked into the documented pattern (PLANIT_API_NOTES.md §2, §7). Single page.
_PAGE_SIZE = 300
_PAGE = 1


def _unwrap_envelope(payload) -> list[dict]:
    """Return the records list from a {from, records, total?} envelope or a bare list."""
    if isinstance(payload, dict) and "records" in payload:
        records = payload["records"]
    elif isinstance(payload, list):
        records = payload
    else:
        raise ValueError(
            "PlanIt response must be a {records:[...]} envelope or a bare list; "
            f"got {type(payload).__name__}"
        )
    if not isinstance(records, list):
        raise ValueError("PlanIt 'records' must be a list")
    return records


def _normalize_record(record: dict) -> dict:
    """Map a PlanIt record to a job dict: uid, url, council, docs_url.

    Uses record['url'] (the council deep link) — NOT other_fields.source_url,
    which is a generic search page (PLANIT_API_NOTES.md §5). Council comes from
    area_name. docs_url comes from other_fields.docs_url when present.

    Contact fields (agent_name, applicant_name, case_officer) are NOT extracted
    in STANDARD scope, so PLANIT_API_NOTES §8's 'See source' sentinel needs no
    handling here.
    """
    other = record.get("other_fields") or {}
    return {
        "uid": record.get("uid"),
        "url": record.get("url"),
        "council": record.get("area_name"),
        "docs_url": other.get("docs_url"),
    }


async def _fetch_feed_raw(
    client: httpx.AsyncClient,
    params: dict,
) -> list[dict]:
    """GET the list endpoint with the given date params; return unwrapped RAW records."""
    query = {**params, "pg_sz": _PAGE_SIZE, "page": _PAGE}
    response = await client.get(PLANIT_LIST_URL, params=query)
    response.raise_for_status()
    return _unwrap_envelope(response.json())


async def _fetch_feed(
    client: httpx.AsyncClient,
    params: dict,
) -> list[dict]:
    """GET the list endpoint with the given date params; return normalized jobs."""
    records = await _fetch_feed_raw(client, params)
    return [_normalize_record(r) for r in records]


async def fetch_created(client: httpx.AsyncClient, date: str) -> list[dict]:
    """Fetch PlanIt's 'created on date' feed; return normalized job dicts."""
    return await _fetch_feed(client, {"start_date": date, "end_date": date})


async def fetch_created_raw(client: httpx.AsyncClient, date: str) -> list[dict]:
    """Fetch PlanIt's 'created on date' feed; return RAW (unnormalized) records."""
    return await _fetch_feed_raw(client, {"start_date": date, "end_date": date})


async def fetch_updated(client: httpx.AsyncClient, date: str) -> list[dict]:
    """Fetch PlanIt's 'updated on date' feed; return normalized job dicts.

    NOTE: changed_start/changed_end are UNVERIFIED live (PLANIT_API_NOTES.md §3).
    """
    return await _fetch_feed(client, {"changed_start": date, "changed_end": date})


async def fetch_jobs(client: httpx.AsyncClient, date: str) -> list[dict]:
    """Fetch created + updated for date, merge, dedupe by uid (first-seen wins)."""
    created = await fetch_created(client, date)
    updated = await fetch_updated(client, date)

    merged: dict[str, dict] = {}
    for job in [*created, *updated]:
        uid = job.get("uid")
        if uid is None:
            continue
        merged.setdefault(uid, job)
    return list(merged.values())
