"""Unit tests for the pure portal-detection and URL-derivation functions."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from planit_spike.portal_detect import derive_urls, is_idox_url, set_active_tab

IDOX = "https://planning.bradford.gov.uk/online-applications/applicationDetails.do?activeTab=makeComment&keyVal=ABC123"
NON_IDOX = "https://publicaccess.bracknell-forest.gov.uk/s/register-view?c__r=Arcus_BE_Public_Register"


def test_is_idox_url_positive():
    assert is_idox_url(IDOX) is True


def test_is_idox_url_negative():
    assert is_idox_url(NON_IDOX) is False
    assert is_idox_url("") is False


def test_set_active_tab_rewrites_tab_preserves_keyval():
    out = set_active_tab(IDOX, "summary")
    qs = parse_qs(urlparse(out).query)
    assert qs["activeTab"] == ["summary"]
    assert qs["keyVal"] == ["ABC123"]


def test_set_active_tab_adds_tab_when_absent():
    base = "https://host/online-applications/applicationDetails.do?keyVal=XYZ"
    qs = parse_qs(urlparse(set_active_tab(base, "documents")).query)
    assert qs["activeTab"] == ["documents"]
    assert qs["keyVal"] == ["XYZ"]


def test_derive_urls_idox():
    urls = derive_urls({"url": IDOX})
    assert set(urls) == {"details", "contacts", "dates", "docs"}
    expected_tab = {
        "details": "summary", "contacts": "contacts",
        "dates": "dates", "docs": "documents",
    }
    for kind, tab in expected_tab.items():
        qs = parse_qs(urlparse(urls[kind]).query)
        assert qs["activeTab"] == [tab]
        assert qs["keyVal"] == ["ABC123"]   # preserved on every tab


def test_derive_urls_non_idox_returns_empty():
    assert derive_urls({"url": NON_IDOX}) == {}
    assert derive_urls({}) == {}


def test_derive_urls_prefers_explicit_docs_url():
    other_docs = "https://host/online-applications/applicationDetails.do?activeTab=makeComment&keyVal=DOCS9"
    urls = derive_urls({"url": IDOX, "docs_url": other_docs})
    qs = parse_qs(urlparse(urls["docs"]).query)
    assert qs["activeTab"] == ["documents"]
    assert qs["keyVal"] == ["DOCS9"]
