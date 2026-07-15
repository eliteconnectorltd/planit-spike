"""Async orchestration: load jobs, build SSL context and httpx clients, fetch all.

Takes a fully-configured logger and returns list[JobResult]. Owns no CLI
concerns — no argparse, no timing, no logging setup; cli.py wraps this.
Imports config, types, throttle, fetch, and paths (portal_detect is reached
transitively via fetch.process_job -> derive_urls).
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

from . import config
from .fetch import process_job
from .throttle import HostThrottle
from .types import JobResult

try:
    import certifi
    _CERTIFI_PATH: Optional[str] = certifi.where()
except ImportError:
    _CERTIFI_PATH = None


def _load_jobs(input_path: Path, logger: logging.Logger) -> list[dict]:
    """Read the input file, accepting a bare list or a PlanIt {records:[...]} envelope."""
    raw = json.loads(input_path.read_text(encoding="utf-8"))

    if isinstance(raw, dict) and "records" in raw:
        jobs = raw["records"]
    elif isinstance(raw, list):
        jobs = raw
    else:
        raise ValueError(
            "Input JSON must be a list of job objects or a PlanIt envelope "
            "with a 'records' key."
        )

    logger.info(f"Loaded {len(jobs)} jobs from {input_path}")
    return jobs


def _build_ssl_context(logger: logging.Logger) -> ssl.SSLContext:
    """Build an SSLContext from certifi's CA bundle (Windows cert fix), or system default.

    Passing an explicit SSLContext to httpx is more reliable than a file path,
    especially on Windows where Python may not pick up the OS certificate store.
    This is the fix that made Mid Kent work over the secure client.
    """
    if _CERTIFI_PATH:
        ssl_ctx = ssl.create_default_context(cafile=_CERTIFI_PATH)
        logger.info(f"Using certifi CA bundle at {_CERTIFI_PATH}")
    else:
        ssl_ctx = ssl.create_default_context()
        logger.info("certifi not installed; using system default CA bundle")
    return ssl_ctx


async def _fetch_jobs(
    jobs: list[dict],
    output_root: Path,
    concurrency: int,
    per_host_delay: float,
    logger: logging.Logger,
    insecure_hosts: Optional[set[str]] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> list[JobResult]:
    """Fetch an already-loaded jobs list: build clients + throttle, process all.

    The shared fetch core reused by both run() (jobs-from-file) and
    daily.run_daily() (jobs-from-PlanIt). Owns client/SSL/throttle setup and
    the concurrent gather; knows nothing about where the jobs came from.
    """
    throttle = HostThrottle(per_host_delay)
    sem = asyncio.Semaphore(concurrency)
    insecure_hosts = insecure_hosts or set()

    ssl_ctx = _build_ssl_context(logger)

    if insecure_hosts:
        logger.warning(
            f"SSL verification DISABLED for hosts: {sorted(insecure_hosts)} "
            f"(--insecure-hosts). Do not use in production."
        )

    secure_client = httpx.AsyncClient(
        headers=config.BROWSER_HEADERS, http2=False, verify=ssl_ctx,
        transport=transport,
    )
    insecure_client = httpx.AsyncClient(
        headers=config.BROWSER_HEADERS, http2=False, verify=False,
        transport=transport,
    ) if insecure_hosts else None

    try:
        def pick_client(url: str) -> httpx.AsyncClient:
            host = urlparse(url).netloc
            if insecure_client is not None and host in insecure_hosts:
                return insecure_client
            return secure_client

        tasks = [
            process_job(pick_client, throttle, job, output_root, logger, sem)
            for job in jobs
        ]
        results = await asyncio.gather(*tasks)
    finally:
        await secure_client.aclose()
        if insecure_client is not None:
            await insecure_client.aclose()

    return results


async def run(
    input_path: Path,
    output_root: Path,
    concurrency: int,
    per_host_delay: float,
    logger: logging.Logger,
    insecure_hosts: Optional[set[str]] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> list[JobResult]:
    """Load jobs from a file, then fetch them via the shared core. Returns results."""
    jobs = _load_jobs(input_path, logger)
    return await _fetch_jobs(
        jobs, output_root, concurrency, per_host_delay, logger,
        insecure_hosts=insecure_hosts, transport=transport,
    )
