"""Idox portal detection and Idox URL normalization / derivation.

Detection is by URL substring (the Idox applicationDetails.do marker). URL
normalization rewrites only the activeTab query param, preserving every other
param — keyVal above all, since it is the record's opaque portal key.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from . import config

IDOX_PATH_MARKERS = ("applicationDetails.do",)

# Idox-specific translation: our fetch-kind -> the portal's activeTab value.
_TAB_FOR_KIND = {
    "details": "summary",
    "contacts": "contacts",
    "dates": "dates",
    "docs": "documents",
}


def is_idox_url(url: str) -> bool:
    """True if the URL looks like an Idox applicationDetails page."""
    if not url:
        return False
    return any(m in url for m in IDOX_PATH_MARKERS)


def set_active_tab(url: str, tab: str) -> str:
    """Rewrite the activeTab query param, preserving all other params (incl. keyVal)."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs["activeTab"] = [tab]
    new_query = urlencode({k: v[0] for k, v in qs.items()}, safe="/")
    return urlunparse(parsed._replace(query=new_query))


def derive_urls(job: dict) -> dict[str, str]:
    """Return {kind: url} for an Idox job, or {} if not Idox.

    Keys are config.FETCH_KINDS ("details", "contacts", "dates", "docs"); each
    URL is job['url'] with activeTab rewritten per _TAB_FOR_KIND, keyVal
    preserved. An explicit Idox docs_url, if provided, overrides derived docs.
    Non-Idox: return {} — caller skips.
    """
    url = job.get("url") or job.get("details_url")
    if not url or not is_idox_url(url):
        return {}

    urls = {
        kind: set_active_tab(url, _TAB_FOR_KIND[kind])
        for kind in config.FETCH_KINDS
    }

    docs_url_input = job.get("docs_url") or job.get("other_fields", {}).get("docs_url")
    if docs_url_input and is_idox_url(docs_url_input):
        urls["docs"] = set_active_tab(docs_url_input, "documents")

    return urls
