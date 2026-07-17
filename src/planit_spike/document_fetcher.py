"""Download the documents listed in an Idox docs table, one at a time.

Consumes DocumentLinks from document_parser and streams each file to
output_dir/documents/ as {index:02d}_{idox_filename}. Sequential by design —
polite to councils, and a record's ~10 documents finish in seconds.

Streaming (client.stream + aiter_bytes) is a new pattern in this codebase:
fetch.py reads response.text for 30-50KB HTML tabs, but drawings can exceed
10MB and must not be held in memory. Status and Content-Type are inspected
before any file is opened, so a failed or misnamed fetch never leaves a partial
file behind; a mid-stream failure deletes what it wrote.

Reuses the caller's httpx.AsyncClient so session cookies and the SSL/insecure
routing already established for the portal carry over to the document fetches.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

import httpx

from .document_parser import DocumentLink
from .errors import format_exception
from .paths import safe_path_part
from .types import DocumentFetchResult

# Content-Type -> extension. Used only when the URL carries no extension of its
# own, which is the uncommon case: every document link observed so far ends in
# .pdf/.xlsx. Kept deliberately tight — add entries when a real response proves
# the need, not speculatively.
_CONTENT_TYPE_EXTENSIONS: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tif",
    "application/zip": ".zip",
}

# Transient HTTP statuses worth another attempt. Other 4xx are permanent.
_RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# Transient transport failures worth another attempt. NetworkError is the
# parent of ConnectError/ReadError/WriteError; the subclasses are named
# explicitly because the design calls them out, and the redundancy is harmless.
_RETRY_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.NetworkError,
)


def _idox_filename(url: str) -> str:
    """Last path segment of a document URL (Idox's own filename), path-safe.

    Decoded first, then sanitized: a %2F in the URL would otherwise decode to a
    separator and let the name escape documents_dir.
    """
    raw = unquote(urlparse(url).path.rsplit("/", 1)[-1])
    return safe_path_part(raw) if raw else raw


def _extension_from_content_type(content_type: Optional[str]) -> Optional[str]:
    """Map a Content-Type header to a file extension, or None if unmapped."""
    if not content_type:
        return None
    mime = content_type.split(";", 1)[0].strip().lower()
    return _CONTENT_TYPE_EXTENSIONS.get(mime)


def _compose_filename(
    index: int,
    url: str,
    content_type: Optional[str],
    logger: logging.Logger,
    tag: str,
) -> str:
    """Build {index:02d}_{idox_filename}, filling a missing extension from
    Content-Type, else .bin with a warning."""
    name = _idox_filename(url) or "document"

    if not Path(name).suffix:
        extension = _extension_from_content_type(content_type)
        if extension is None:
            logger.warning(
                f"{tag} no extension in URL and unmapped Content-Type "
                f"{content_type!r}; saving as .bin"
            )
            extension = ".bin"
        name = f"{name}{extension}"

    return f"{index:02d}_{name}"


def _should_retry(status_code: int) -> bool:
    """True for 429 and 5xx; False for other 4xx and for success."""
    return status_code in _RETRY_STATUS_CODES


def _backoff_seconds(attempt: int, backoff_base: float) -> float:
    """Sleep before `attempt`: 0, base*1, base*2, base*4 for attempts 1..4."""
    if attempt <= 1:
        return 0.0
    return backoff_base * (2 ** (attempt - 2))


async def _stream_to_disk(
    response: httpx.Response,
    target: Path,
) -> int:
    """Stream a response body into target, returning bytes written.

    The caller deletes target if this raises partway through.
    """
    bytes_written = 0
    with open(target, "wb") as handle:
        async for chunk in response.aiter_bytes():
            handle.write(chunk)
            bytes_written += len(chunk)
    return bytes_written


async def _fetch_one_document(
    link: DocumentLink,
    index: int,
    client: httpx.AsyncClient,
    documents_dir: Path,
    council: str,
    uid: str,
    logger: logging.Logger,
    max_attempts: int,
    backoff_base: float,
) -> DocumentFetchResult:
    """Stream one document to disk with retry/backoff; delete partials on failure."""
    tag = f"[{council}/{uid}/docs/{index:02d}]"
    result = DocumentFetchResult(url=link.url)
    last_error: Optional[str] = None

    for attempt in range(1, max_attempts + 1):
        result.attempts = attempt

        sleep_for = _backoff_seconds(attempt, backoff_base)
        if sleep_for:
            await asyncio.sleep(sleep_for)

        target: Optional[Path] = None
        try:
            logger.info(f"{tag} GET {link.url}")
            async with client.stream("GET", link.url) as response:
                # Status and headers are known before any file is opened, so a
                # rejected fetch never creates one.
                if response.status_code != 200:
                    error = f"HTTP {response.status_code}"
                    if _should_retry(response.status_code) and attempt < max_attempts:
                        last_error = error
                        logger.warning(
                            f"{tag} attempt {attempt} failed: {error}; retrying in "
                            f"{_backoff_seconds(attempt + 1, backoff_base):.1f}s"
                        )
                        # continue exits the async with cleanly (__aexit__ closes
                        # the response); retry sleep happens at the top of the
                        # next iteration.
                        continue
                    if _should_retry(response.status_code):
                        # attempt == max_attempts here; fall through to shared tail
                        last_error = error
                        break
                    result.error = error
                    # Distinguish a council-side "Document Unavailable" page (an
                    # Idox 404 with an HTML body) from other 404s. The document
                    # is genuinely not retrievable via this URL, so it is not a
                    # transient failure — categorize it for the report. Body is
                    # read only here, on the non-retryable give-up path.
                    if response.status_code == 404 and "text/html" in (
                        response.headers.get("content-type", "").lower()
                    ):
                        body = await response.aread()
                        if b"document unavailable" in body[:4096].lower():
                            result.download_status = "unavailable"
                            result.notes = (
                                "Council returned 'Document Unavailable' page "
                                "(HTTP 404 with HTML). Document is not retrievable "
                                "via this URL."
                            )
                    logger.error(f"{tag} gave up after {attempt} attempts: {error}")
                    return result

                target = documents_dir / _compose_filename(
                    index, link.url, response.headers.get("content-type"), logger, tag
                )
                byte_size = await _stream_to_disk(response, target)

            result.saved_path = target
            result.byte_size = byte_size
            logger.info(f"{tag} saved {byte_size} bytes -> {target}")
            return result

        except _RETRY_EXCEPTIONS as exc:
            if target is not None:
                target.unlink(missing_ok=True)  # drop the partial write
            last_error = format_exception(exc)
            if attempt < max_attempts:
                logger.warning(
                    f"{tag} attempt {attempt} failed: {last_error}; retrying in "
                    f"{_backoff_seconds(attempt + 1, backoff_base):.1f}s"
                )
                continue
            break

        except Exception as exc:  # non-retryable: bad path, disk full, etc.
            if target is not None:
                target.unlink(missing_ok=True)
            result.error = format_exception(exc)
            logger.error(f"{tag} gave up after {attempt} attempts: {result.error}")
            return result

    result.error = last_error
    logger.error(f"{tag} gave up after {result.attempts} attempts: {last_error}")
    return result


async def fetch_documents(
    links: list[DocumentLink],
    client: httpx.AsyncClient,
    output_dir: Path,
    council: str,
    uid: str,
    logger: logging.Logger,
    max_attempts: int = 4,
    backoff_base: float = 1.0,
) -> list[DocumentFetchResult]:
    """Download every link sequentially into output_dir/documents/."""
    if not links:
        return []

    documents_dir = output_dir / "documents"
    documents_dir.mkdir(parents=True, exist_ok=True)

    results: list[DocumentFetchResult] = []
    for index, link in enumerate(links, start=1):
        results.append(await _fetch_one_document(
            link, index, client, documents_dir, council, uid, logger,
            max_attempts, backoff_base,
        ))
    return results
