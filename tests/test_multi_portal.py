"""Fixture-based tests for the multi-portal (non-Idox) fetch functions.

Zero live HTTP: every request is served by an httpx.MockTransport that routes
by path segment to the captured fixtures (arun_details.html, arun_documents.html)
or to small inline synthetic bodies. Async paths are driven with asyncio.run
inside sync test functions (no pytest-asyncio dependency). Phase 3 (Salesforce)
and Phase 4 (BathNES) tests will be added to this file under their own classes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from planit_spike.multi_portal import (
    _download_document,
    _extract_document_rows,
    _parse_details,
    fetch_ocella_web,
)

FIXTURES = Path(__file__).parent / "fixtures" / "multi_portal"
ARUN_DETAILS = (FIXTURES / "arun_details.html").read_text(encoding="utf-8")
ARUN_DOCUMENTS = (FIXTURES / "arun_documents.html").read_text(encoding="utf-8")

ARUN_URL = (
    "https://www1.arun.gov.uk/aplanning/OcellaWeb/planningDetails"
    "?from=planningSearch&reference=BR%2F128%2F26%2FTC"
)

# A minimal real PDF body — the %PDF- magic is all the code inspects.
FAKE_PDF = b"%PDF-1.4 fake"

# An HTML interstitial that contains NO resolvable download link (level-2 dead end):
# its only anchor points at planningSearch — no file extension, no viewDocument,
# no "download" — so _find_download_link returns None.
INTERSTITIAL_NO_LINK = (
    "<html><body><h1>Document viewer</h1>"
    "<p>Please wait while your document loads.</p>"
    "<a href='/aplanning/OcellaWeb/planningSearch'>Back to search</a>"
    "</body></html>"
)


# ── Mock transport builders ─────────────────────────────────────────────────

def _make_ok_transport(doc_body: bytes, doc_ct: str) -> httpx.MockTransport:
    """Route details GET / showDocuments POST to fixtures; viewDocument -> a file.

    Every viewDocument request returns the same `doc_body`/`doc_ct`, covering
    the 'all docs are real files' happy path.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "planningDetails" in path:
            return httpx.Response(200, text=ARUN_DETAILS,
                                  headers={"content-type": "text/html;charset=UTF-8"})
        if "showDocuments" in path:
            return httpx.Response(200, text=ARUN_DOCUMENTS,
                                  headers={"content-type": "text/html;charset=UTF-8"})
        if "viewDocument" in path:
            return httpx.Response(200, content=doc_body,
                                  headers={"content-type": doc_ct})
        return httpx.Response(404, text="not found")
    return httpx.MockTransport(handler)


def _make_doc_only_transport(body_text: str, ct: str) -> httpx.MockTransport:
    """Every viewDocument GET returns the same body/content-type.

    Used by the _download_document unit tests, which only hit viewDocument.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body_text, headers={"content-type": ct})
    return httpx.MockTransport(handler)


def _first_document_entry() -> dict:
    """The real first document row (URL + hint) from the docs fixture."""
    base = "https://www1.arun.gov.uk/aplanning/OcellaWeb/showDocuments"
    return _extract_document_rows(ARUN_DOCUMENTS, base)[0]


class TestOcellaWeb:
    # ── Parser-level unit tests (no HTTP) ───────────────────────────────────

    def test_ocella_web_parser_extracts_16_field_schema(self):
        """The 16-field schema is populated from the details fixture."""
        fields = _parse_details(ARUN_DETAILS)
        assert fields["reference"] == "BR/128/26/TC"
        assert fields["status"] == "Undecided"
        assert fields["proposal"].startswith(
            "Crown reduction to 1 no. Magnolia (T1)")
        # OcellaWeb labels this "Location"; mapped to site_address.
        assert fields["site_address"] == (
            "Hotham Park House, Garden Flat High Street Bognor Regis  PO21 1HW")
        assert fields["validated_date"] == "14-07-26"

    def test_ocella_web_parser_applicant_care_of_quirk(self):
        """The care-of value stays verbatim in applicant_name, unsplit."""
        fields = _parse_details(ARUN_DETAILS)
        assert fields["applicant_name"] == (
            "Care Hotham Park House Garden Flat High Street Bognor Regis  PO21 1HW")
        # Not reclassified as an address.
        assert fields["applicant_address"] is None

    def test_ocella_web_parser_agent_name_fused_unsplit(self):
        """agent_name holds the whole name+address blob; sub-fields stay null."""
        fields = _parse_details(ARUN_DETAILS)
        assert fields["agent_name"] == (
            "Mr & Mrs Nigel Sampson 18 Westmorland Drive Bognor Regis  PO228LZ")
        assert fields["agent_address"] is None
        assert fields["agent_company"] is None
        assert fields["agent_phone"] is None
        assert fields["agent_email"] is None

    def test_ocella_web_parser_absent_fields_are_null(self):
        """Fields OcellaWeb does not expose are None, not fabricated."""
        fields = _parse_details(ARUN_DETAILS)
        for absent in ("application_type", "estimated_cost",
                       "applicant_phone", "applicant_email",
                       "agent_phone", "agent_email"):
            assert fields[absent] is None, f"{absent} should be None"

    def test_ocella_web_parser_extracts_document_rows(self):
        """Five documents extracted, with correct URLs and hint fallback."""
        base = "https://www1.arun.gov.uk/aplanning/OcellaWeb/showDocuments"
        rows = _extract_document_rows(ARUN_DOCUMENTS, base)
        assert len(rows) == 5
        assert rows[0]["filename_hint"] == "Application Form - Without Personal Data"
        assert rows[0]["url_level_1"].endswith("ApplicationFormRedacted.pdf&module=pl")
        assert rows[0]["url_level_1"].startswith("https://www1.arun.gov.uk/")
        # Rows 4 & 5 have a blank description column -> category fallback.
        assert rows[3]["filename_hint"] == "Systems Correspondence"
        assert rows[4]["filename_hint"] == "Systems Correspondence"

    # ── Download unit tests (_download_document via asyncio.run) ─────────────

    def test_ocella_web_download_level1_pdf_saved(self, tmp_path):
        """Level-1 PDF happy path: saved, sized, ok, one hop, no leaked keys."""
        entry = _first_document_entry()
        # FAKE_PDF is pure ASCII, so latin-1 round-trip preserves bytes exactly.
        # httpx.Response(text=...) re-encodes to the same 13 bytes.
        transport = _make_doc_only_transport(FAKE_PDF.decode("latin-1"),
                                             "application/pdf")

        async def _do():
            async with httpx.AsyncClient(transport=transport) as client:
                return await _download_document(entry, 1, tmp_path, client)

        doc = asyncio.run(_do())
        assert doc["download_status"] == "ok"
        assert doc["levels_followed"] == 1
        assert doc["url_level_2"] is None
        assert doc["byte_size"] == len(FAKE_PDF)
        assert doc["saved_path"] is not None
        assert Path(doc["saved_path"]).exists()
        assert Path(doc["saved_path"]).read_bytes() == FAKE_PDF
        assert Path(doc["saved_path"]).name == "01_ApplicationFormRedacted.pdf"
        assert not any(k.startswith("_") for k in doc)

    def test_ocella_web_download_html_interstitial_requires_manual(self, tmp_path):
        """Level-1 HTML with no download link -> requires_manual_fetch at 2 hops."""
        entry = _first_document_entry()
        transport = _make_doc_only_transport(INTERSTITIAL_NO_LINK, "text/html")

        async def _do():
            async with httpx.AsyncClient(transport=transport) as client:
                return await _download_document(entry, 1, tmp_path, client)

        doc = asyncio.run(_do())
        assert doc["download_status"] == "requires_manual_fetch"
        assert doc["saved_path"] is None
        assert doc["levels_followed"] == 2
        assert doc["notes"] and "manual" in doc["notes"].lower()
        # No internal bookkeeping keys leaked into the returned record.
        assert not any(k.startswith("_") for k in doc), \
            f"internal key leaked: {[k for k in doc if k.startswith('_')]}"

    # ── Full integration test (fetch_ocella_web end-to-end) ─────────────────

    def test_ocella_web_full_fetch_produces_expected_dict_shape(self, tmp_path):
        """End-to-end: details GET + docs POST + 5 doc downloads, full dict."""
        transport = _make_ok_transport(FAKE_PDF, "application/pdf")

        async def _do():
            async with httpx.AsyncClient(transport=transport) as client:
                return await fetch_ocella_web(ARUN_URL, tmp_path, client)

        result = asyncio.run(_do())

        # Portal-level fields.
        assert result["portal_family"] == "OcellaWeb"
        assert result["portal_name"] == "arun"   # derived from www1.arun.gov.uk
        assert result["council"] is None
        assert result["uid"] == "BR/128/26/TC"
        assert result["url"] == ARUN_URL

        # All 16 schema fields: real values where present, null where absent.
        assert result["reference"] == "BR/128/26/TC"
        assert result["status"] == "Undecided"
        assert result["proposal"].startswith("Crown reduction to 1 no. Magnolia")
        assert result["site_address"] == (
            "Hotham Park House, Garden Flat High Street Bognor Regis  PO21 1HW")
        assert result["validated_date"] == "14-07-26"
        assert result["applicant_name"] == (
            "Care Hotham Park House Garden Flat High Street Bognor Regis  PO21 1HW")
        assert result["agent_name"] == (
            "Mr & Mrs Nigel Sampson 18 Westmorland Drive Bognor Regis  PO228LZ")
        for absent in ("application_type", "estimated_cost",
                       "applicant_address", "applicant_phone", "applicant_email",
                       "agent_company", "agent_address", "agent_phone", "agent_email"):
            assert result[absent] is None, f"{absent} should be None"

        # Five documents, every one saved at a single hop.
        assert len(result["documents"]) == 5
        for i, doc in enumerate(result["documents"], start=1):
            assert doc["download_status"] == "ok", f"doc {i} not ok"
            assert doc["levels_followed"] == 1, f"doc {i} wrong hop count"
            assert doc["index"] == i
            assert doc["byte_size"] == len(FAKE_PDF)
            assert Path(doc["saved_path"]).exists()

        # raw_html_paths present with real on-disk paths (docs.html convention).
        paths = result["raw_html_paths"]
        assert Path(paths["details"]).exists() and Path(paths["details"]).name == "details.html"
        assert Path(paths["docs"]).exists() and Path(paths["docs"]).name == "docs.html"
        assert paths["rendered"] is None

        # No leaked internal keys anywhere in the top-level result.
        assert not any(k.startswith("_") for k in result)
