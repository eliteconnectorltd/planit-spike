"""Daily-discovery orchestration: PlanIt records for a date -> saved artifacts.

The 'run' subcommand's engine. Fetches a day's PlanIt 'created' feed, writes
planit.json for EVERY record (Idox or not), then fetches all four Idox tabs
for the Idox records via the shared runner core (runner._fetch_jobs). Returns
(records, results) so the caller can build the daily report over both.

Reuses: planit_client.fetch_created_raw (discovery) + _normalize_record
(mapping), portal_detect.is_idox_url (Idox test), paths.job_output_dir (path
coherence), runner._fetch_jobs / _build_ssl_context (the shared client/SSL/
fetch core).

VPN: PlanIt calls require the user's VPN to be ON before running (no proxy
handling in code — see README.md / NOTES.md).
"""

from __future__ import annotations

import json
import logging
import ssl
from pathlib import Path
from typing import Optional

import httpx

from . import config
from .paths import job_output_dir
from .planit_client import _normalize_record, fetch_created_raw
from .portal_detect import is_idox_url
from .runner import _build_ssl_context, _fetch_jobs
from .types import JobResult

_PLANIT_HEADERS = {
    "Accept": "application/json",
    "User-Agent": config.USER_AGENT,
    "Referer": "https://www.planit.org.uk/",
}


def _build_planit_client(
    ssl_ctx: ssl.SSLContext,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> httpx.AsyncClient:
    """Build the httpx client for PlanIt's JSON API (distinct headers from council fetches)."""
    return httpx.AsyncClient(
        headers=_PLANIT_HEADERS, http2=False, verify=ssl_ctx, transport=transport,
    )


def _write_planit_json(record: dict, output_root: Path) -> None:
    """Write a record's raw dict to {output_root}/{council}/{uid}/planit.json (indent=2)."""
    council = record.get("area_name") or "unknown"
    uid = record.get("uid") or "unknown"
    out_dir = job_output_dir(output_root, council, uid)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "planit.json").write_text(
        json.dumps(record, indent=2), encoding="utf-8"
    )


async def run_daily(
    date: str,
    output_root: Path,
    concurrency: int,
    per_host_delay: float,
    logger: logging.Logger,
    insecure_hosts: Optional[set[str]] = None,
    limit: Optional[int] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> tuple[list[dict], list[JobResult]]:
    """Discover PlanIt records for `date`, save planit.json for each, fetch Idox tabs.

    Returns (records, results): the raw PlanIt records (post-limit) and the
    JobResults from fetching the Idox subset. output_root is the date-scoped
    dir (output/{date}); the caller has already created it.
    """
    ssl_ctx = _build_ssl_context(logger)

    async with _build_planit_client(ssl_ctx, transport) as planit_client:
        records = await fetch_created_raw(planit_client, date)
    logger.info(f"PlanIt returned {len(records)} records for {date}")

    if limit is not None:
        records = records[:limit]
        logger.info(f"Limited to first {len(records)} records")

    for record in records:
        _write_planit_json(record, output_root)

    jobs = [
        _normalize_record(r) for r in records
        if is_idox_url(r.get("url") or "")
    ]
    logger.info(f"{len(jobs)} of {len(records)} records are Idox; fetching tabs")

    # _fetch_jobs builds its own SSL context internally. Rebuilding is
    # cheap and each AsyncClient needs its own reference; sharing would
    # need a signature change on _fetch_jobs for no functional gain.
    results = await _fetch_jobs(
        jobs, output_root, concurrency, per_host_delay, logger,
        insecure_hosts=insecure_hosts, transport=transport,
    )

    return records, results
