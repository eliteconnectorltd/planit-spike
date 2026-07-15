"""Parse an Idox documents tab (docs.html) into downloadable DocumentLink rows.

Anchors on the single `table#Documents` present on every Idox docs page. Column
positions differ by council (Bradford has 6 columns, Cornwall adds a "Drawing
Number" making 7), so cells are read via a header-label -> index map built from
the table's own <th> row rather than fixed indices. The View link is located by
its title="View Document" attribute, independent of column position.

Hrefs in Idox docs tables are relative ("/online-applications/files/..."); the
caller supplies base_url (scheme + host, derived from the record's portal URL)
and this module returns absolute URLs.

Pagination is detect-and-warn only: Phase 4 does not follow it. Observed tables
top out at 19 documents on a single page; a paginated table is unobserved, so
the parser reports the flag and leaves the decision to the caller.

Leaf module: stdlib only, no in-package imports.
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin

_TABLE_RE = re.compile(
    r'<table[^>]*\bid="Documents"[^>]*>(.*?)</table>', re.S | re.I
)
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
# Matches both <td>...</td> and self-closing <td/> (Cornwall's empty cells).
_CELL_RE = re.compile(r"<t[dh][^>]*/>|<t[dh][^>]*>.*?</t[dh]>", re.S | re.I)
_CELL_INNER_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_TH_RE = re.compile(r"<th[^>]*/>|<th[^>]*>.*?</th>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
# Lookahead so attribute order (title before href, or after) never matters.
_VIEW_HREF_RE = re.compile(
    r'<a\b(?=[^>]*\btitle=["\']View Document["\'])'
    r'[^>]*\bhref=["\']([^"\']+)["\']',
    re.S | re.I,
)
_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S | re.I)
_HREF_ATTR_RE = re.compile(r'href="([^"]*)"', re.I)
_CLASS_ID_ATTR_RE = re.compile(r'(?:class|id)="([^"]*)"', re.I)

# Pagination signals (see _detect_pagination).
_NEXT_TEXTS = {"next", ">>", "»"}
_PAGE_HREF_RE = re.compile(r"page[=-]\d+", re.I)
_PAGER_WORD_RE = re.compile(r"\b(?:pager|pagination)\b", re.I)
_PAGE_N_RE = re.compile(r"\bpage[-\d]", re.I)


@dataclass
class DocumentLink:
    """One downloadable document row parsed from an Idox documents table."""

    url: str                              # absolute, base_url-joined
    doc_type: Optional[str] = None        # "Document Type" column
    description: Optional[str] = None     # "Description" column
    date_published: Optional[str] = None  # "Date Published" column, as-printed
    drawing_number: Optional[str] = None  # "Drawing Number" column (Cornwall-style)


def _strip_tags(fragment: str) -> str:
    """Reduce an HTML fragment to its unescaped, whitespace-collapsed text."""
    text = _TAG_RE.sub(" ", fragment)
    return re.sub(r"\s+", " ", _html.unescape(text)).strip()


def _find_documents_table(docs_html: str) -> Optional[str]:
    """Return the inner HTML of <table id="Documents">, or None if absent."""
    match = _TABLE_RE.search(docs_html)
    return match.group(1) if match else None


def _split_rows(table_html: str) -> list[str]:
    """Return the table's <tr> inner fragments in document order."""
    return _ROW_RE.findall(table_html)


def _header_index_map(header_row: str) -> dict[str, int]:
    """Map lowercased <th> label -> column index (e.g. {'description': 5})."""
    index_map: dict[str, int] = {}
    for i, th in enumerate(_TH_RE.findall(header_row)):
        label = _strip_tags(th).lower()
        if label:
            index_map[label] = i
    return index_map


def _split_cells(row_html: str) -> list[str]:
    """Return a row's <td> fragments, including self-closing empties (<td/>)."""
    return _CELL_RE.findall(row_html)


def _cell_text(
    cells: list[str], index_map: dict[str, int], label: str
) -> Optional[str]:
    """Text of the cell under `label`, or None when the column or value is absent."""
    index = index_map.get(label)
    if index is None or index >= len(cells):
        return None
    inner = _CELL_INNER_RE.match(cells[index])
    text = _strip_tags(inner.group(1)) if inner else ""
    return text or None


def _view_href(row_html: str) -> Optional[str]:
    """The row's View-Document href, located by title="View Document"; None if absent."""
    match = _VIEW_HREF_RE.search(row_html)
    return _html.unescape(match.group(1)) if match else None


def _detect_pagination(docs_html: str) -> bool:
    """True if the page shows next/page-N pagination controls around the table.

    Strong signals: anchor text of exactly Next/>>/», or an href carrying
    page=N / page-N. Class/id signals require a whole-word 'pager'/'pagination',
    or 'page' followed by a digit or hyphen — never a bare 'page' prefix, which
    would false-positive on Idox's own pageheading / pagehelp markup.
    """
    for attrs, inner in _ANCHOR_RE.findall(docs_html):
        if _strip_tags(inner).lower() in _NEXT_TEXTS:
            return True

        href_match = _HREF_ATTR_RE.search(attrs)
        if href_match and _PAGE_HREF_RE.search(_html.unescape(href_match.group(1))):
            return True

        for value in _CLASS_ID_ATTR_RE.findall(attrs):
            if _PAGER_WORD_RE.search(value) or _PAGE_N_RE.search(value):
                return True

    return False


def parse_document_links(
    docs_html: str,
    base_url: str,
) -> tuple[list[DocumentLink], bool]:
    """Parse docs.html into (DocumentLinks with absolute urls, pagination_detected).

    Rows without a View-Document href (header row, any placeholder) are skipped.
    Returns ([], False) when no table#Documents is present.
    """
    table_html = _find_documents_table(docs_html)
    if table_html is None:
        return ([], False)

    rows = _split_rows(table_html)
    if not rows:
        return ([], False)

    index_map = _header_index_map(rows[0])
    links: list[DocumentLink] = []

    for row in rows:
        href = _view_href(row)
        if href is None:
            continue  # header row, or any row without a document to fetch

        cells = _split_cells(row)
        links.append(DocumentLink(
            url=urljoin(base_url, href),
            doc_type=_cell_text(cells, index_map, "document type"),
            description=_cell_text(cells, index_map, "description"),
            date_published=_cell_text(cells, index_map, "date published"),
            drawing_number=_cell_text(cells, index_map, "drawing number"),
        ))

    return (links, _detect_pagination(docs_html))
