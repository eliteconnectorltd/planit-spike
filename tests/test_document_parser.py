"""Unit tests for the Idox documents-table parser.

Fixtures are real captured docs.html from two councils with genuinely different
column layouts: Bradford (6 columns) and Cornwall (7 — it adds "Drawing
Number", shifting Description and View right by one). Parsing both correctly
through one code path is the point of the header-label index map.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from planit_spike.document_parser import (
    _detect_pagination,
    _view_href,
    parse_document_links,
)

FIXTURES = Path(__file__).parent / "fixtures"

BRADFORD_HTML = (FIXTURES / "bradford_docs_table.html").read_text(encoding="utf-8")
CORNWALL_HTML = (FIXTURES / "cornwall_docs_table.html").read_text(encoding="utf-8")

BRADFORD_BASE = "https://planning.bradford.gov.uk"
CORNWALL_BASE = "https://planning.cornwall.gov.uk"

BRADFORD_LINKS, BRADFORD_PAGINATED = parse_document_links(BRADFORD_HTML, BRADFORD_BASE)
CORNWALL_LINKS, CORNWALL_PAGINATED = parse_document_links(CORNWALL_HTML, CORNWALL_BASE)


# --- link counts (a, b) ------------------------------------------------------

def test_bradford_link_count():
    assert len(BRADFORD_LINKS) == 9


def test_cornwall_link_count():
    assert len(CORNWALL_LINKS) == 19


# --- absolute URLs (c, d) ----------------------------------------------------

def test_bradford_urls_are_absolute():
    assert all(link.url.startswith("https://") for link in BRADFORD_LINKS)


def test_cornwall_urls_are_absolute():
    assert all(link.url.startswith("https://") for link in CORNWALL_LINKS)


# --- Bradford column content (e, f, g) ---------------------------------------

def test_bradford_doc_type_counts():
    counts = Counter(link.doc_type for link in BRADFORD_LINKS)
    assert counts == {
        "Application Form": 1,
        "Drawing": 4,
        "Supporting Information": 4,
    }


def test_bradford_descriptions_round_trip():
    descriptions = [link.description for link in BRADFORD_LINKS]
    assert descriptions.count("APPLICATION FORM REDACTED") == 1
    assert descriptions.count("LOCATION PLAN") == 1


def test_bradford_has_no_drawing_numbers():
    # Bradford's table has no "Drawing Number" column at all.
    assert all(link.drawing_number is None for link in BRADFORD_LINKS)


# --- Cornwall column content (h, i) ------------------------------------------

def test_cornwall_drawing_numbers():
    numbers = [link.drawing_number for link in CORNWALL_LINKS if link.drawing_number]
    assert len(numbers) == 4
    assert set(numbers) == {"001", "002", "003", "004"}


def test_cornwall_description_read_from_correct_column():
    # Index-sensitive: Cornwall's Description sits at index 5, one right of
    # Bradford's. A miscomputed index would yield the Drawing Number cell ('001')
    # or the Document Type ('Plan - Site and Block') instead.
    by_number = {
        link.drawing_number: link.description
        for link in CORNWALL_LINKS
        if link.drawing_number
    }
    assert by_number["001"] == "LOCATION PLANS"
    assert by_number["003"] == "PROPOSED FLOOR PLAN, ELEVATIONS AND ROOF PLAN"


# --- pagination (j, k) -------------------------------------------------------

def test_bradford_pagination_not_detected():
    assert _detect_pagination(BRADFORD_HTML) is False
    assert BRADFORD_PAGINATED is False


def test_cornwall_pagination_not_detected():
    assert _detect_pagination(CORNWALL_HTML) is False
    assert CORNWALL_PAGINATED is False


# --- View-href attribute-order tolerance (l) ---------------------------------

def test_view_href_tolerates_attribute_order_and_quotes():
    href_first = '<a href="/files/a.pdf" target="_blank" title="View Document">v</a>'
    title_first = '<a class="recaptcha-link" title="View Document" href="/files/b.pdf">v</a>'
    single_quoted = "<a title='View Document' href='/files/c.pdf'>v</a>"

    assert _view_href(href_first) == "/files/a.pdf"
    assert _view_href(title_first) == "/files/b.pdf"
    assert _view_href(single_quoted) == "/files/c.pdf"


def test_view_href_absent_returns_none():
    assert _view_href('<a href="/files/x.pdf">no title attr</a>') is None


# --- urljoin correctness (m) -------------------------------------------------

def test_urljoin_produces_expected_absolute_url():
    # Full-string assertion so any urljoin/base-handling regression is caught,
    # not just a "starts with https" smoke check.
    assert BRADFORD_LINKS[0].url == (
        "https://planning.bradford.gov.uk/online-applications/files/"
        "29ECFE54E358F99683705D2B570FF4DC/pdf/"
        "26_02066_FUL-APPLICATION_FORM_REDACTED-9032466.pdf"
    )


# --- degenerate inputs (n, o) ------------------------------------------------

def test_empty_html_returns_empty():
    assert parse_document_links("", "https://x") == ([], False)


def test_no_table_returns_empty():
    assert parse_document_links("<html>no table</html>", "https://x") == ([], False)
