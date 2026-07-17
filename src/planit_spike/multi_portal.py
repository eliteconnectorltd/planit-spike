"""Multi-portal fetch (proof-of-concept) — NON-Idox planning portals.

Fetches planning-application records and documents from three portal families
that the existing Idox pipeline does not handle:

    - OcellaWeb           (plain HTML, httpx)          e.g. Arun
    - Salesforce Lightning (JS-rendered, Playwright)   e.g. Anglesey   [Phase 3]
    - BathNES webforms    (JS-rendered, Playwright)     e.g. BathNES    [Phase 4]

Each fetch function returns the same result-dict shape (see fetch_ocella_web)
and does the same job as the Idox pipeline: save the raw record HTML, save the
documents-listing HTML if separate, extract document links, try to download
each document (2-hop rule), and report per-document outcomes.

This module is deliberately self-contained. It does NOT import from any Idox
module (document_parser, document_fetcher, fetch, etc.); the Idox path is
untouched. Filenames match the Idox convention (details.html / docs.html).

── Field provenance: OcellaWeb (Arun) ──────────────────────────────────────
EXPOSES at the details level: reference, status, proposal, site_address
  (labelled "Location"), validated_date ("Validated"), applicant, agent.
  Also present but outside the 16-field schema: parish, case_officer,
  received_date, decision_by_date, comment_by_date.
Does NOT expose: application_type, estimated_cost, agent_company,
  agent_address, agent_phone, agent_email, applicant_address,
  applicant_phone, applicant_email  -> all null.
QUIRKS:
  * agent / applicant are single undelimited "name + address" blobs. We keep
    the whole string in *_name and never guess a split (downstream can).
  * The "Applicant" row sometimes holds a care-of ADDRESS rather than a
    person's name (data-entry quirk, e.g. "Care Hotham Park House ..."). The
    label is genuinely "Applicant", so we keep the raw value in applicant_name
    and do not reclassify it as an address.
────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Content-types we treat as a real, saveable document (anything else that comes
# back as text/html is an interstitial to follow per the 2-hop rule).
_FILE_CONTENT_TYPES = (
    "application/pdf",
    "image/",                     # jpeg, png, gif, tiff
    "application/vnd.openxmlformats-officedocument",   # docx, xlsx, pptx
    "application/msword",
    "application/vnd.ms-excel",
    "application/octet-stream",   # some portals serve files untyped
)

# Extension inferred from content-type when the filename hint lacks one.
_EXT_FOR_CT = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/tiff": ".tif",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
}

# 16-field planning schema: OcellaWeb details-table label -> our field name.
# Only these labels are mapped; everything else on the schema stays null.
_OCELLA_LABEL_MAP = {
    "reference": "reference",
    "status": "status",
    "proposal": "proposal",
    "location": "site_address",     # OcellaWeb labels this "Location"; mapped to site_address
    "validated": "validated_date",
    "applicant": "applicant_name",  # raw blob; may be a care-of address (see quirk note)
    "agent": "agent_name",          # raw name+address blob; not split
}

# Every field in the flat schema, so absent ones are explicitly null.
_SCHEMA_FIELDS = (
    "reference", "status", "proposal", "site_address", "validated_date",
    "application_type", "estimated_cost",
    "applicant_name", "applicant_address", "applicant_phone", "applicant_email",
    "agent_name", "agent_company", "agent_address", "agent_phone", "agent_email",
)

_ILLEGAL_PATH_CHARS = re.compile(r'[<>:"/\\|?*]+')


def _sanitize_for_path(value: str) -> str:
    """Make a reference/filename safe as a single path segment."""
    cleaned = _ILLEGAL_PATH_CHARS.sub("_", value).strip(" ._")
    return cleaned or "unknown"


def _parse_details(html: str) -> dict:
    """Parse the OcellaWeb details table into the flat 16-field schema.

    Every schema field is present; those OcellaWeb does not expose are None.
    """
    soup = BeautifulSoup(html, "html.parser")
    fields = {name: None for name in _SCHEMA_FIELDS}
    for tr in soup.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        label = cells[0].get_text(" ", strip=True).lower()
        value = cells[1].get_text(" ", strip=True)
        target = _OCELLA_LABEL_MAP.get(label)
        if target and value:
            fields[target] = value
    return fields


def _find_documents_form(soup: BeautifulSoup) -> tuple[str, dict] | None:
    """Locate the 'View Documents' form; return (action, post_body) or None.

    Verified from the Arun fixture: the form carries no hidden inputs — the
    only field is the submit button, and reference/module ride in the action
    query string. The POST body is therefore just the submit name=value.
    """
    form = soup.find("form", attrs={"name": "showDocuments"})
    if form is None:
        # Fall back to any form whose action points at showDocuments.
        for f in soup.find_all("form"):
            if "showDocuments" in (f.get("action") or ""):
                form = f
                break
    if form is None:
        return None
    body: dict[str, str] = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if name:
            body[name] = inp.get("value") or ""
    return form.get("action"), body


def _extract_document_rows(docs_html: str, base_url: str) -> list[dict]:
    """Extract one row per viewDocument link from the documents page.

    filename_hint = the row's description column, falling back to the category
    column when the description is blank (verified against the Arun fixture).
    """
    soup = BeautifulSoup(docs_html, "html.parser")
    rows: list[dict] = []
    for tr in soup.find_all("tr"):
        anchor = tr.find("a", href=True)
        if not anchor or "viewDocument" not in anchor["href"]:
            continue
        cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
        category = cells[0] if cells else ""
        description = cells[-1] if cells else ""
        hint = description or category or anchor.get_text(strip=True) or None
        rows.append({
            "url_level_1": urljoin(base_url, anchor["href"]),
            "filename_hint": hint,
        })
    return rows


def _is_file_content_type(content_type: str | None) -> bool:
    """True if the content-type is a real document (not an HTML interstitial)."""
    if not content_type:
        return False
    ct = content_type.split(";", 1)[0].strip().lower()
    return any(ct.startswith(prefix) for prefix in _FILE_CONTENT_TYPES)


def _filename_from_url(url: str, index: int, content_type: str | None) -> str:
    """Build a safe NN_<name>.<ext> filename for a document.

    The real filename lives in the viewDocument 'file=' query param
    (url-encoded, backslash-separated). Extension is taken from the name, or
    inferred from the content-type when the name has none.
    """
    name = "document"
    m = re.search(r"[?&]file=([^&]+)", url)
    if m:
        decoded = unquote(m.group(1)).replace("\\", "/")
        name = decoded.rsplit("/", 1)[-1] or name
    stem, dot, ext = name.rpartition(".")
    if not dot:  # no extension in the hint — infer from content-type
        ct = (content_type or "").split(";", 1)[0].strip().lower()
        ext = _EXT_FOR_CT.get(ct, "").lstrip(".")
        stem = name
    safe = _sanitize_for_path(f"{stem}.{ext}" if ext else stem)
    return f"{index:02d}_{safe}"


def _find_download_link(html: str, base_url: str) -> str | None:
    """From an HTML interstitial, find a real file download link (level-2 hop).

    Looks for an anchor to a file-looking href, then a meta-refresh target.
    Returns an absolute URL or None if nothing usable is found.
    """
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(r"\.(pdf|jpe?g|png|gif|tiff?|docx?|xlsx?)($|\?)", href, re.I) \
                or "viewDocument" in href or "download" in href.lower():
            return urljoin(base_url, href)
    meta = soup.find("meta", attrs={"http-equiv": re.compile("refresh", re.I)})
    if meta and "url=" in (meta.get("content") or "").lower():
        target = meta["content"].split("url=", 1)[1].strip().strip("'\"")
        return urljoin(base_url, target)
    return None


async def _download_document(
    entry: dict, index: int, docs_dir: Path, client: httpx.AsyncClient,
) -> dict:
    """Download one document per the 2-hop rule; return its result record.

    Level 1: GET url_level_1. If a real file type -> save (levels_followed=1).
    If text/html -> parse for a real link -> url_level_2.
    Level 2: GET url_level_2. If a real file type -> save (levels_followed=2).
    Otherwise -> requires_manual_fetch. Never a third hop.
    """
    record = {
        "index": index,
        "filename_hint": entry.get("filename_hint"),
        "url_level_1": entry["url_level_1"],
        "url_level_2": None,
        "download_status": None,
        "saved_path": None,
        "byte_size": 0,
        "levels_followed": 1,
        "notes": None,
    }

    async def _fetch_and_maybe_save(url: str, level: int) -> bool:
        """Return True if a real file was saved at this hop."""
        resp = await client.get(url, follow_redirects=True)
        record["levels_followed"] = level
        if resp.status_code != 200:
            record["download_status"] = f"failed_{resp.status_code}"
            return False
        ct = resp.headers.get("content-type")
        if _is_file_content_type(ct):
            docs_dir.mkdir(parents=True, exist_ok=True)
            fname = _filename_from_url(url, index, ct)
            path = docs_dir / fname
            # PoC: whole-body read via resp.content. Idox pipeline uses streaming
            # via client.stream + aiter_bytes for large files (e.g. Cornwall's
            # 6 MB PDFs). For OcellaWeb at production scale, migrate to streaming
            # to detect mid-download failures.
            path.write_bytes(resp.content)
            record["saved_path"] = str(path)
            record["byte_size"] = len(resp.content)
            record["download_status"] = "ok"
            return True
        # HTML interstitial — surface the follow-up link for the caller.
        record["_interstitial_html"] = resp.text
        record["_interstitial_url"] = str(resp.url)
        return False

    try:
        if await _fetch_and_maybe_save(record["url_level_1"], level=1):
            return _clean_internal_keys(record)
        # Level 1 was HTML (or failed_<code>). Only follow when we have HTML.
        if "_interstitial_html" in record:
            link = _find_download_link(
                record.pop("_interstitial_html"), record.pop("_interstitial_url")
            )
            if link:
                record["url_level_2"] = link
                if await _fetch_and_maybe_save(link, level=2):
                    return _clean_internal_keys(record)
            # No usable link, or level-2 still HTML/non-200: manual fetch.
            if record["download_status"] in (None, "ok"):
                record["download_status"] = "requires_manual_fetch"
            record["levels_followed"] = 2
            record["notes"] = (
                "Level-1 returned HTML; "
                + ("no download link found" if not link else "level-2 not a file")
                + ". Visit manually."
            )
        # else: level 1 was a non-200 failure; status already set.
    except httpx.HTTPError as exc:
        record["download_status"] = "network_error"
        record["notes"] = f"{type(exc).__name__}: {exc}"
    return _clean_internal_keys(record)


def _clean_internal_keys(record: dict) -> dict:
    """Strip transient internal keys and assert none leaked into the record."""
    record.pop("_interstitial_html", None)
    record.pop("_interstitial_url", None)
    assert "_interstitial_html" not in record, "internal key leaked"
    assert "_interstitial_url" not in record, "internal key leaked"
    return record


async def fetch_ocella_web(url: str, output_dir, client: httpx.AsyncClient) -> dict:
    """Fetch one OcellaWeb (plain-HTML) planning record and its documents.

    Saves details.html and docs.html under
    <output_dir>/OcellaWeb/<uid>/, extracts the 16-field schema and the
    document list, and downloads each document per the 2-hop rule.

    Returns:
      A result dict with portal-level fields, the flat 16-field planning
      schema (fields OcellaWeb does not expose are None — see the module
      docstring for provenance and quirks), a `documents` list, and
      `raw_html_paths` (keys: details, docs, rendered).

    Raises:
      Any httpx exception from the details page GET. Caller MUST catch
      per-record so batch runs survive individual failures. Per-document
      failures are recorded in the returned dict, not raised. A missing or
      failed documents page is likewise recorded (empty documents), not raised.
    """
    output_dir = Path(output_dir)

    # --- Level 0: details page (fetch failure here propagates by design) ---
    details_resp = await client.get(url, follow_redirects=True)
    details_resp.raise_for_status()
    details_html = details_resp.text
    base_url = str(details_resp.url)

    fields = _parse_details(details_html)
    reference = fields.get("reference") or "unknown"
    uid_safe = _sanitize_for_path(reference)

    record_dir = output_dir / "OcellaWeb" / uid_safe
    record_dir.mkdir(parents=True, exist_ok=True)
    details_path = record_dir / "details.html"
    details_path.write_text(details_html, encoding="utf-8")

    # portal_name derived from URL host. council intentionally None:
    # mapping hostname → official council name requires a registry we
    # don't maintain in this PoC. Downstream can enrich.
    hostname = urlparse(url).hostname or "unknown"
    portal_name = hostname.replace("www1.", "").replace("www.", "").split(".")[0]

    result = {
        "portal_family": "OcellaWeb",
        "portal_name": portal_name,   # e.g. "arun" from arun.gov.uk
        "council": None,              # hostname→council mapping not in PoC scope
        "uid": reference,
        "url": url,
        **fields,
        "documents": [],
        "raw_html_paths": {
            "details": str(details_path),
            "docs": None,
            "rendered": None,   # plain-HTML portal; no Playwright render
        },
    }

    # --- Level 1: documents page (missing/failed -> recorded, not raised) ---
    soup = BeautifulSoup(details_html, "html.parser")
    form_info = _find_documents_form(soup)
    if form_info is None:
        logger.info("OcellaWeb %s: no documents form found", reference)
        return result

    action, post_body = form_info
    docs_url = urljoin(base_url, action)
    try:
        docs_resp = await client.post(docs_url, data=post_body, follow_redirects=True)
        docs_resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("OcellaWeb %s: docs page fetch failed: %s", reference, exc)
        return result

    docs_html = docs_resp.text
    docs_path = record_dir / "docs.html"
    docs_path.write_text(docs_html, encoding="utf-8")
    result["raw_html_paths"]["docs"] = str(docs_path)

    # --- Level 2: per-document download (per-doc failure never aborts) ---
    rows = _extract_document_rows(docs_html, str(docs_resp.url))
    docs_dir = record_dir / "documents"
    documents = []
    for i, entry in enumerate(rows, start=1):
        documents.append(await _download_document(entry, i, docs_dir, client))
    result["documents"] = documents
    return result
