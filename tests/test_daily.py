"""End-to-end daily-pipeline tests: drive `run` subcommand via a mocked transport.

One MockTransport (a _MockHandler instance) serves BOTH PlanIt's JSON API (the
synthetic daily fixture) and the council portals (real Bradford/Cornwall HTML
fixtures + contacts/dates stubs), routing by host. Fully offline/deterministic.

Semantic note asserted below: in daily mode non-Idox records are filtered out
BEFORE jobs are built, so they never produce a JobResult. The daily report
therefore shows totals.skipped == 0 (a fetch-layer count) while
discovery.non_idox_records == 1 (a discovery-layer count). This differs from
fetch mode, where a non-Idox job DOES produce a supported=False JobResult and
counts as skipped. Both are correct; they count different things.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from planit_spike import cli

FIXTURES = Path(__file__).parent / "fixtures"
DAILY_FIXTURE = FIXTURES / "sample_planit_created_daily.json"

# (host, activeTab) -> real HTML fixture for the Idox summary/documents tabs.
COUNCIL_ROUTES = {
    ("planning.bradford.gov.uk", "summary"): "bradford_details.html",
    ("planning.bradford.gov.uk", "documents"): "bradford_docs.html",
    ("planning.cornwall.gov.uk", "summary"): "cornwall_details.html",
    ("planning.cornwall.gov.uk", "documents"): "cornwall_docs.html",
}

DATE = "2026-07-13"


class _MockHandler:
    """MockTransport handler serving PlanIt + council hosts; records what it saw.

    Per-instance state (seen_hosts, observed_keyvals) so each test gets a fresh
    handler with no shared/cross-test state.
    """

    def __init__(self, daily_fixture: Path):
        self.daily_fixture = daily_fixture
        self.seen_hosts: dict[str, int] = {}
        self.observed_keyvals: dict[str, set[str]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.seen_hosts[host] = self.seen_hosts.get(host, 0) + 1

        # PlanIt discovery API -> serve the synthetic daily fixture.
        if host == "www.planit.org.uk":
            return httpx.Response(
                200, text=self.daily_fixture.read_text(encoding="utf-8"), request=request
            )

        # A non-Idox host must never be fetched; make it a loud, catchable failure.
        if host == "planning.wandsworth.gov.uk":
            return httpx.Response(599, text="UNEXPECTED wandsworth fetch", request=request)

        # Council portals: record the keyVal, then route summary/documents from
        # real fixtures and contacts/dates to a stub.
        tab = request.url.params.get("activeTab")
        keyval = request.url.params.get("keyVal")
        if keyval is not None:
            self.observed_keyvals.setdefault(host, set()).add(keyval)

        fixture = COUNCIL_ROUTES.get((host, tab))
        if fixture is not None:
            return httpx.Response(
                200, text=(FIXTURES / fixture).read_text(encoding="utf-8"), request=request
            )
        if tab in ("contacts", "dates"):
            return httpx.Response(200, text=f"<html>{tab} stub</html>", request=request)
        return httpx.Response(404, text="unmapped route", request=request)


def _patch_transport(monkeypatch, handler: _MockHandler) -> None:
    """Wrap cli.run_daily so it pins the mock transport, without changing the CLI."""
    transport = httpx.MockTransport(handler)
    from planit_spike import daily
    real_run_daily = daily.run_daily

    async def run_daily_with_mock(*args, **kwargs):
        kwargs["transport"] = transport
        return await real_run_daily(*args, **kwargs)

    monkeypatch.setattr(cli, "run_daily", run_daily_with_mock)


def test_daily_run(tmp_path, monkeypatch):
    handler = _MockHandler(DAILY_FIXTURE)
    _patch_transport(monkeypatch, handler)
    out_dir = tmp_path / "out"

    rc = cli.main([
        "run",
        "--date", DATE,
        "--output-dir", str(out_dir),
        "--per-host-delay", "0",
    ])
    assert rc == 0

    date_root = out_dir / DATE
    brad = date_root / "Bradford" / "26_90001_FUL"
    corn = date_root / "Cornwall" / "PA26_90002"
    wand = date_root / "Wandsworth" / "2026_90003"

    # --- Discovery-level: planit.json for ALL 3 records --------------------
    for d, exp_uid, exp_council in (
        (brad, "26/90001/FUL", "Bradford"),
        (corn, "PA26/90002", "Cornwall"),
        (wand, "2026/90003", "Wandsworth"),
    ):
        pj_path = d / "planit.json"
        assert pj_path.exists(), f"missing planit.json for {exp_council}"
        pj = json.loads(pj_path.read_text(encoding="utf-8"))
        assert pj["uid"] == exp_uid                 # uid round-trips unchanged
        assert pj["area_name"] == exp_council       # area_name round-trips unchanged

    # Cornwall's raw sentinel passes through unchanged (no normalization).
    corn_pj = json.loads((corn / "planit.json").read_text(encoding="utf-8"))
    assert corn_pj["other_fields"]["agent_name"] == "See source"

    # --- Fetch-level: 4 tabs for both Idox records ------------------------
    for d in (brad, corn):
        for name in ("details.html", "contacts.html", "dates.html", "docs.html"):
            assert (d / name).exists()
            assert (d / name).stat().st_size > 0

    # Bradford details round-trips the real fixture byte-for-byte.
    assert (brad / "details.html").read_text(encoding="utf-8") == \
        (FIXTURES / "bradford_details.html").read_text(encoding="utf-8")

    # Non-Idox Wandsworth: planit.json only, NO tab HTML.
    for name in ("details.html", "contacts.html", "dates.html", "docs.html"):
        assert not (wand / name).exists()

    # Wandsworth portal was never contacted (non-Idox = no fetch); PlanIt was.
    assert handler.seen_hosts.get("planning.wandsworth.gov.uk", 0) == 0
    assert handler.seen_hosts.get("www.planit.org.uk", 0) >= 1

    # Each Idox host saw only its own keyVal (guards derive_urls keyVal handling).
    assert handler.observed_keyvals["planning.bradford.gov.uk"] == {"DAILYTEST0001"}
    assert handler.observed_keyvals["planning.cornwall.gov.uk"] == {"DAILYTEST0002"}

    # --- Report-level ------------------------------------------------------
    report = json.loads((date_root / "run_report.json").read_text(encoding="utf-8"))
    assert report["discovery"] == {
        "total_records": 3, "idox_records": 2, "non_idox_records": 1,
    }
    assert report["per_council_records"]["Bradford"] == {"seen": 1, "fetched": 1}
    assert report["per_council_records"]["Cornwall"] == {"seen": 1, "fetched": 1}
    assert report["per_council_records"]["Wandsworth"] == {"seen": 1, "fetched": 0}
    assert report["totals"]["supported"] == 2
    # Non-Idox filtered out before jobs built -> never a JobResult -> not "skipped".
    # discovery.non_idox_records carries the "saw it, didn't fetch" signal instead.
    assert report["totals"]["skipped"] == 0


def test_daily_run_limit(tmp_path, monkeypatch):
    handler = _MockHandler(DAILY_FIXTURE)
    _patch_transport(monkeypatch, handler)
    out_dir = tmp_path / "out"

    rc = cli.main([
        "run",
        "--date", DATE,
        "--limit", "1",
        "--output-dir", str(out_dir),
        "--per-host-delay", "0",
    ])
    assert rc == 0

    date_root = out_dir / DATE
    brad = date_root / "Bradford" / "26_90001_FUL"
    corn = date_root / "Cornwall" / "PA26_90002"
    wand = date_root / "Wandsworth" / "2026_90003"

    # Only the first record (Bradford) is processed: planit.json + 4 tabs.
    assert (brad / "planit.json").exists()
    for name in ("details.html", "contacts.html", "dates.html", "docs.html"):
        assert (brad / name).exists()

    # Cornwall and Wandsworth: neither planit.json nor tab HTML.
    for d in (corn, wand):
        assert not (d / "planit.json").exists()
        assert not (d / "details.html").exists()

    report = json.loads((date_root / "run_report.json").read_text(encoding="utf-8"))
    assert report["discovery"]["total_records"] == 1
