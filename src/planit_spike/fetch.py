"""Fetch one URL with retry/backoff and save it; process a job's two fetches.

Imports config (tuning + warning thresholds), types (result dataclasses),
throttle (per-host pacing), paths (output-dir construction), and portal_detect
(URL derivation). Emits an advisory warning on suspicious saved content but
always keeps the file.

After the tab fetches, an Idox job whose docs tab arrived cleanly has its
documents parsed and downloaded through the same client, so the portal session
established by the tab fetches carries over.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import config
from .document_fetcher import fetch_documents
from .document_parser import parse_document_links
from .errors import format_exception
from .paths import job_output_dir
from .portal_detect import derive_urls
from .throttle import HostThrottle
from .types import FetchResult, JobResult


def _warn_if_suspicious(
    body: str,
    result: FetchResult,
    logger: logging.Logger,
) -> None:
    """Advisory warning for likely-bad saved content. Never blocks the save.

    Denied-text wins: if an access-denied marker is present, warn about that
    and return. Only when not denied do we check for an unusually small body.
    """
    tag = f"[{result.council}/{result.uid}/{result.kind}]"

    for marker in config.DENIED_MARKERS:
        if marker in body:
            logger.warning(f"{tag} content appears to be an access-denied page")
            return

    if result.byte_size is not None and result.byte_size < config.SMALL_FILE_WARN_BYTES:
        logger.warning(
            f"{tag} unusually small response ({result.byte_size} bytes), "
            f"may be incomplete"
        )


async def fetch_one(
    pick_client,
    throttle: HostThrottle,
    council: str,
    uid: str,
    kind: str,
    url: str,
    out_path: Path,
    logger: logging.Logger,
    max_retries: int = config.DEFAULT_MAX_RETRIES,
    retry_backoff: float = config.DEFAULT_RETRY_BACKOFF,
) -> FetchResult:
    """Fetch one URL, retry transient errors, save body to out_path, return a FetchResult.

    Retries on transient ConnectError / ReadTimeout / RemoteProtocolError with
    exponential backoff. ``pick_client`` is a callable returning the httpx client
    for a given URL (routes --insecure-hosts through a verify-disabled client).
    """
    result = FetchResult(council=council, uid=uid, kind=kind, url=url)
    host = urlparse(url).netloc
    client = pick_client(url)

    last_exception: Exception | None = None
    for attempt in range(1, max_retries + 1):
        await throttle.wait(host)
        try:
            attempt_note = f" (attempt {attempt}/{max_retries})" if attempt > 1 else ""
            logger.info(f"[{council}/{uid}/{kind}] GET {url}{attempt_note}")
            response = await client.get(url, follow_redirects=True, timeout=config.DEFAULT_TIMEOUT)
            result.status_code = response.status_code

            if response.status_code != 200:
                result.error = f"HTTP {response.status_code}"
                logger.warning(f"[{council}/{uid}/{kind}] HTTP {response.status_code}")
                return result

            body = response.text
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(body, encoding="utf-8")
            result.saved_path = str(out_path)
            result.byte_size = len(body.encode("utf-8"))
            logger.info(
                f"[{council}/{uid}/{kind}] saved {result.byte_size} bytes -> {out_path}"
            )
            _warn_if_suspicious(body, result, logger)
            return result

        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            last_exception = e
            if attempt < max_retries:
                wait = retry_backoff * (2 ** (attempt - 1))
                logger.warning(
                    f"[{council}/{uid}/{kind}] {format_exception(e)} — "
                    f"retrying in {wait:.1f}s"
                )
                await asyncio.sleep(wait)
                continue
            result.error = format_exception(e)
            logger.error(
                f"[{council}/{uid}/{kind}] {result.error} (after {max_retries} attempts)"
            )
            return result
        except httpx.HTTPError as e:
            result.error = format_exception(e)
            logger.error(f"[{council}/{uid}/{kind}] {result.error}")
            return result
        except Exception as e:
            result.error = format_exception(e)
            logger.exception(f"[{council}/{uid}/{kind}] unexpected error")
            return result

    # Should not reach here, but be safe
    result.error = f"exhausted retries: {last_exception}"
    return result


async def process_job(
    pick_client,
    throttle: HostThrottle,
    job: dict,
    output_root: Path,
    logger: logging.Logger,
    sem: asyncio.Semaphore,
) -> JobResult:
    """Derive URLs for a job, fetch every tab, then download the listed documents."""
    uid = job.get("uid") or job.get("name") or "unknown"
    council = job.get("council") or job.get("area_name") or "unknown"

    urls = derive_urls(job)
    if not urls:
        return JobResult(
            council=council,
            uid=uid,
            supported=False,
            skip_reason="Not an Idox portal (unsupported family in this spike)",
        )

    async with sem:
        job_dir = job_output_dir(output_root, council, uid)
        kinds = list(urls)
        tasks = [
            fetch_one(
                pick_client, throttle, council, uid, kind,
                urls[kind], job_dir / f"{kind}.html", logger,
            )
            for kind in kinds
        ]
        results = await asyncio.gather(*tasks)
        fetches = dict(zip(kinds, results))

        documents, pagination_detected = await _process_documents(
            pick_client, fetches, urls, job_dir, council, uid, logger,
        )

    return JobResult(
        council=council, uid=uid, supported=True,
        fetches=fetches,
        documents=documents,
        pagination_detected=pagination_detected,
    )


async def _process_documents(
    pick_client,
    fetches: dict[str, FetchResult],
    urls: dict[str, str],
    job_dir: Path,
    council: str,
    uid: str,
    logger: logging.Logger,
) -> tuple[list, bool]:
    """Parse the saved docs tab and download what it lists.

    Returns ([], False) unless the docs tab fetched cleanly: a failed docs
    fetch leaves nothing to parse, and an empty table is data, not an error.
    """
    docs_result = fetches.get("docs")
    if docs_result is None or not docs_result.ok or docs_result.saved_path is None:
        return ([], False)

    docs_html = Path(docs_result.saved_path).read_text(encoding="utf-8")
    parsed = urlparse(urls["docs"])
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    links, pagination_detected = parse_document_links(docs_html, base_url)
    if pagination_detected:
        logger.warning(
            f"[{council}/{uid}/docs] pagination controls detected; "
            f"document list may be incomplete"
        )
    if not links:
        logger.info(f"[{council}/{uid}/docs] no documents found for {council}/{uid}")
        return ([], pagination_detected)

    logger.info(f"[{council}/{uid}/docs] {len(links)} documents listed")
    documents = await fetch_documents(
        links, pick_client(urls["docs"]), job_dir, council, uid, logger,
    )
    return (documents, pagination_detected)
