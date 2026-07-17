"""Unit tests for the document downloader.

Every test drives fetch_documents through httpx.MockTransport with
backoff_base=0.0, so the retry ladder is exercised without real sleeps.
Handlers are per-test instances (no module-level mutable state), following the
pattern in test_daily.py.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest

from planit_spike.document_fetcher import _backoff_seconds, fetch_documents
from planit_spike.document_parser import DocumentLink

LOGGER = logging.getLogger("test-document-fetcher")


def _links(*urls: str) -> list[DocumentLink]:
    """DocumentLinks for the given urls; only .url matters to the fetcher."""
    return [DocumentLink(url=u) for u in urls]


async def _run(handler, tmp_path: Path, links, **kwargs) -> list:
    """Drive fetch_documents against a MockTransport handler."""
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        return await fetch_documents(
            links, client, tmp_path, "Bradford", "26/02066/FUL", LOGGER,
            backoff_base=0.0, **kwargs,
        )


class _SequenceHandler:
    """Serves a scripted sequence of responses, one per request.

    Each entry is either a callable taking the request (so it can raise, or
    build a fresh streaming body) or a prebuilt httpx.Response. Calls past the
    end of the sequence repeat the last entry, which is how "always fails" is
    expressed. State is per-instance: every test constructs its own.
    """

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        index = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        entry = self.responses[index]
        return entry(request) if callable(entry) else entry


def _ok_pdf(body: bytes = b"PDF-BYTES"):
    """A 200 application/pdf response carrying `body`."""
    return httpx.Response(200, content=body, headers={"content-type": "application/pdf"})


def _status(code: int):
    """A bare response with the given status and an empty body."""
    return httpx.Response(code, content=b"")


def _raising_body_response(request=None):
    """A 200 whose body yields some bytes then fails mid-stream.

    httpx accepts an async iterable as `content`, so the generator below is a
    genuine streaming body: aiter_bytes() hands out b"PARTIAL", then the raise
    propagates out of the caller's `async for` — exactly like a connection
    dropping mid-download. No monkeypatching needed.
    """
    async def bad_body():
        yield b"PARTIAL"
        raise httpx.ReadError("simulated mid-stream failure")

    return httpx.Response(
        200, content=bad_body(), headers={"content-type": "application/pdf"}
    )


# =============================================================================
# GROUP A — happy path
# =============================================================================

@pytest.mark.anyio
async def test_three_links_all_succeed(tmp_path):
    bodies = {"a.pdf": b"AAA", "b.pdf": b"BBBB", "c.pdf": b"CCCCC"}

    def handler(request):
        name = request.url.path.rsplit("/", 1)[-1]
        return _ok_pdf(bodies[name])

    results = await _run(handler, tmp_path, _links(
        "https://h/files/K1/a.pdf", "https://h/files/K2/b.pdf", "https://h/files/K3/c.pdf",
    ))

    assert [r.ok for r in results] == [True, True, True]
    assert [r.attempts for r in results] == [1, 1, 1]
    assert [r.byte_size for r in results] == [3, 4, 5]
    assert len({r.saved_path for r in results}) == 3   # distinct paths


@pytest.mark.anyio
async def test_filenames_are_indexed_with_url_extension(tmp_path):
    def handler(request):
        return _ok_pdf()

    results = await _run(handler, tmp_path, _links(
        "https://h/files/K1/first.pdf",
        "https://h/files/K2/second.pdf",
        "https://h/files/K3/third.pdf",
    ))

    names = [r.saved_path.name for r in results]
    assert names == ["01_first.pdf", "02_second.pdf", "03_third.pdf"]


@pytest.mark.anyio
async def test_file_contents_match_served_bytes(tmp_path):
    payload = b"%PDF-1.4 binary\x00\xff bytes"

    def handler(request):
        return _ok_pdf(payload)

    results = await _run(handler, tmp_path, _links("https://h/files/K/doc.pdf"))

    assert results[0].saved_path.read_bytes() == payload
    assert results[0].byte_size == len(payload)


# =============================================================================
# GROUP B — filename composition
# =============================================================================

@pytest.mark.anyio
async def test_url_extension_wins_over_content_type(tmp_path):
    def handler(request):
        # Content-Type disagrees with the URL; the URL's .xlsx must win.
        return httpx.Response(200, content=b"X", headers={"content-type": "application/pdf"})

    results = await _run(handler, tmp_path, _links("https://h/files/K/sheet.xlsx"))
    assert results[0].saved_path.name == "01_sheet.xlsx"


@pytest.mark.anyio
async def test_pdf_url_keeps_pdf_regardless_of_content_type(tmp_path):
    def handler(request):
        return httpx.Response(200, content=b"X", headers={"content-type": "text/html"})

    results = await _run(handler, tmp_path, _links("https://h/files/K/doc.pdf"))
    assert results[0].saved_path.name == "01_doc.pdf"


@pytest.mark.anyio
async def test_extension_from_content_type_when_url_has_none(tmp_path):
    def handler(request):
        return httpx.Response(200, content=b"X", headers={"content-type": "application/pdf"})

    results = await _run(handler, tmp_path, _links("https://h/files/K/noext"))
    assert results[0].saved_path.name == "01_noext.pdf"


@pytest.mark.anyio
async def test_content_type_charset_is_stripped(tmp_path):
    def handler(request):
        return httpx.Response(
            200, content=b"X", headers={"content-type": "application/pdf; charset=UTF-8"}
        )

    results = await _run(handler, tmp_path, _links("https://h/files/K/noext"))
    assert results[0].saved_path.name == "01_noext.pdf"


@pytest.mark.anyio
async def test_unknown_content_type_falls_back_to_bin(tmp_path, caplog):
    def handler(request):
        return httpx.Response(200, content=b"X", headers={"content-type": "application/x-weird"})

    with caplog.at_level(logging.WARNING):
        results = await _run(handler, tmp_path, _links("https://h/files/K/noext"))

    assert results[0].saved_path.name == "01_noext.bin"
    assert any(
        "saving as .bin" in rec.message
        for rec in caplog.records if rec.levelname == "WARNING"
    )


@pytest.mark.anyio
async def test_missing_content_type_falls_back_to_bin(tmp_path, caplog):
    def handler(request):
        return httpx.Response(200, content=b"X")   # no content-type header

    with caplog.at_level(logging.WARNING):
        results = await _run(handler, tmp_path, _links("https://h/files/K/noext"))

    assert results[0].saved_path.name == "01_noext.bin"
    assert any(
        "saving as .bin" in rec.message
        for rec in caplog.records if rec.levelname == "WARNING"
    )


@pytest.mark.anyio
async def test_path_traversal_cannot_escape_documents_dir(tmp_path):
    def handler(request):
        return _ok_pdf()

    # %2F decodes to '/'; safe_path_part must collapse it so the name stays put.
    # A compromised defense would place the file outside documents/ — the
    # subpath assertions below fail in that case, where a bare "no exception
    # raised" check would not.
    results = await _run(handler, tmp_path, _links(
        "https://h/files/K/%2F..%2F..%2Fetc%2Fpasswd"
    ))

    saved = results[0].saved_path.resolve()
    documents_dir = (tmp_path / "documents").resolve()
    assert saved.parent == documents_dir
    assert documents_dir in saved.parents
    assert saved.exists()


# =============================================================================
# GROUP C — retry ladder (backoff_base=0.0 throughout)
# =============================================================================

@pytest.mark.anyio
async def test_three_500s_then_success(tmp_path, caplog):
    handler = _SequenceHandler(_status(500), _status(500), _status(500), _ok_pdf())

    with caplog.at_level(logging.INFO):
        results = await _run(handler, tmp_path, _links("https://h/files/K/doc.pdf"))

    assert results[0].ok is True
    assert results[0].attempts == 4
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 3
    assert any("saved" in r.message for r in caplog.records if r.levelname == "INFO")


@pytest.mark.anyio
async def test_four_500s_exhausts_attempts(tmp_path, caplog):
    handler = _SequenceHandler(_status(500))   # sequence of one: always 500

    with caplog.at_level(logging.INFO):
        results = await _run(handler, tmp_path, _links("https://h/files/K/doc.pdf"))

    assert results[0].ok is False
    assert results[0].error == "HTTP 500"
    assert results[0].attempts == 4
    assert any(
        "gave up after 4 attempts" in r.message
        for r in caplog.records if r.levelname == "ERROR"
    )


@pytest.mark.anyio
async def test_429_then_success(tmp_path):
    handler = _SequenceHandler(_status(429), _ok_pdf())
    results = await _run(handler, tmp_path, _links("https://h/files/K/doc.pdf"))

    assert results[0].ok is True
    assert results[0].attempts == 2


@pytest.mark.anyio
@pytest.mark.parametrize("code", [502, 503, 504])
async def test_5xx_codes_trigger_retry(tmp_path, code):
    handler = _SequenceHandler(_status(code), _ok_pdf())
    results = await _run(handler, tmp_path, _links("https://h/files/K/doc.pdf"))

    assert results[0].ok is True
    assert results[0].attempts == 2


@pytest.mark.anyio
async def test_connect_error_then_success(tmp_path):
    def raise_connect(request):
        raise httpx.ConnectError("simulated connect failure")

    handler = _SequenceHandler(raise_connect, _ok_pdf())
    results = await _run(handler, tmp_path, _links("https://h/files/K/doc.pdf"))

    assert results[0].ok is True
    assert results[0].attempts == 2


@pytest.mark.anyio
async def test_read_timeout_exhausts_attempts(tmp_path):
    def raise_timeout(request):
        raise httpx.ReadTimeout("simulated read timeout")

    handler = _SequenceHandler(raise_timeout)   # sequence of one: always times out
    results = await _run(handler, tmp_path, _links("https://h/files/K/doc.pdf"))

    assert results[0].ok is False
    assert "ReadTimeout" in results[0].error
    assert results[0].attempts == 4


@pytest.mark.anyio
@pytest.mark.parametrize("code", [401, 403, 404])
async def test_4xx_does_not_retry(tmp_path, caplog, code):
    handler = _SequenceHandler(_status(code))

    with caplog.at_level(logging.INFO):
        results = await _run(handler, tmp_path, _links("https://h/files/K/doc.pdf"))

    assert results[0].ok is False
    assert results[0].error == f"HTTP {code}"
    assert results[0].attempts == 1
    assert handler.calls == 1                      # proves no retry happened
    assert any(r.levelname == "ERROR" for r in caplog.records)
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


# =============================================================================
# GROUP D — streaming and cleanup
# =============================================================================

@pytest.mark.anyio
async def test_mid_stream_failure_deletes_partial(tmp_path):
    # ReadError is a NetworkError subclass, so it retries; failing on every
    # attempt is what surfaces the final failed result. _raising_body_response
    # is a callable, so each attempt gets a fresh generator.
    handler = _SequenceHandler(_raising_body_response)

    results = await _run(handler, tmp_path, _links("https://h/files/K/doc.pdf"))

    assert results[0].ok is False
    assert results[0].saved_path is None
    assert "ReadError" in results[0].error
    documents_dir = tmp_path / "documents"
    assert list(documents_dir.iterdir()) == []      # partial write cleaned up


@pytest.mark.anyio
async def test_mid_stream_failure_then_success_leaves_no_partial(tmp_path):
    handler = _SequenceHandler(_raising_body_response, _ok_pdf(b"GOOD"))

    results = await _run(handler, tmp_path, _links("https://h/files/K/doc.pdf"))

    assert results[0].ok is True
    assert results[0].attempts == 2
    assert results[0].saved_path.read_bytes() == b"GOOD"
    assert [p.name for p in (tmp_path / "documents").iterdir()] == ["01_doc.pdf"]


@pytest.mark.anyio
async def test_empty_body_is_a_success(tmp_path):
    def handler(request):
        return _ok_pdf(b"")

    results = await _run(handler, tmp_path, _links("https://h/files/K/doc.pdf"))

    assert results[0].ok is True
    assert results[0].byte_size == 0
    assert results[0].saved_path.exists()
    assert results[0].saved_path.read_bytes() == b""


# =============================================================================
# GROUP E — degenerate
# =============================================================================

@pytest.mark.anyio
async def test_empty_links_returns_empty(tmp_path):
    """Empty links returns [] before the mkdir — documents/ dir NOT created."""
    def handler(request):
        raise AssertionError("no request should be made")

    results = await _run(handler, tmp_path, [])

    assert results == []
    assert not (tmp_path / "documents").exists()


@pytest.mark.anyio
async def test_mixed_success_and_failure_preserves_order(tmp_path):
    def handler(request):
        if request.url.path.endswith("bad.pdf"):
            return _status(404)
        return _ok_pdf(b"GOOD")

    results = await _run(handler, tmp_path, _links(
        "https://h/files/K1/good.pdf", "https://h/files/K2/bad.pdf",
    ))

    assert len(results) == 2
    assert results[0].ok is True
    assert results[0].url.endswith("good.pdf")
    assert results[1].ok is False
    assert results[1].url.endswith("bad.pdf")


# =============================================================================
# GROUP F — backoff formula
# =============================================================================

@pytest.mark.parametrize("attempt,expected", [(1, 0.0), (2, 1.0), (3, 2.0), (4, 4.0)])
def test_backoff_seconds_ladder(attempt, expected):
    assert _backoff_seconds(attempt, 1.0) == expected


def test_backoff_zero_base_never_sleeps():
    assert _backoff_seconds(2, 0.0) == 0.0


# =============================================================================
# GROUP G — "Document Unavailable" 404 categorization
# =============================================================================

def _html_404(body: bytes):
    """A 404 text/html response carrying `body` (council error pages)."""
    return httpx.Response(
        404, content=body, headers={"content-type": "text/html; charset=UTF-8"}
    )


@pytest.mark.anyio
async def test_404_document_unavailable_html_is_categorized_unavailable(tmp_path):
    """A 404 whose HTML body says 'Document Unavailable' -> status 'unavailable'."""
    body = (
        b"<html><head><title>Idox</title></head><body>"
        b"<h1>Document Unavailable</h1>"
        b"<p>The requested document is not available.</p></body></html>"
    )
    handler = _SequenceHandler(_html_404(body))

    results = await _run(handler, tmp_path, _links("https://h/files/K/doc.pdf"))

    r = results[0]
    assert r.ok is False                       # still not a success
    assert r.error == "HTTP 404"               # error string retained
    assert r.download_status == "unavailable"
    assert r.notes == (
        "Council returned 'Document Unavailable' page (HTTP 404 with HTML). "
        "Document is not retrievable via this URL."
    )
    assert r.attempts == 1                      # 404 still does not retry
    assert handler.calls == 1
    assert results[0].saved_path is None        # nothing written


@pytest.mark.anyio
async def test_404_other_html_body_stays_failed_404(tmp_path):
    """A 404 with a different HTML body (no 'Document Unavailable') is unchanged."""
    body = (
        b"<html><body><h1>Not Found</h1>"
        b"<p>The page you requested could not be found.</p></body></html>"
    )
    handler = _SequenceHandler(_html_404(body))

    results = await _run(handler, tmp_path, _links("https://h/files/K/doc.pdf"))

    r = results[0]
    assert r.ok is False
    assert r.error == "HTTP 404"               # classic failure unchanged
    assert r.download_status is None           # NOT categorized as unavailable
    assert r.notes is None
    assert r.attempts == 1


@pytest.fixture
def anyio_backend():
    return "asyncio"
