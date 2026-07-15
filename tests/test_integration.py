"""End-to-end integration test: drive main() with a mocked transport.

Inline subset used instead of sample_jobs.json because sample_jobs points at
5 live hosts and we only fixture 2 supported councils (Bradford, Cornwall)
plus 1 non-Idox (Bracknell). The MockTransport serves captured HTML from
tests/fixtures/ so the test is fully offline and deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from planit_spike import cli

FIXTURES = Path(__file__).parent / "fixtures"

# (host, activeTab) -> fixture filename
ROUTES = {
    ("planning.bradford.gov.uk", "summary"): "bradford_details.html",
    ("planning.bradford.gov.uk", "documents"): "bradford_docs.html",
    ("planning.cornwall.gov.uk", "summary"): "cornwall_details.html",
    ("planning.cornwall.gov.uk", "documents"): "cornwall_docs.html",
}

# Body served for any document download; small, and identifiable on disk.
FAKE_PDF_BYTES = b"%PDF-1.4 fake pdf bytes for testing"

JOBS = [
    {
        "uid": "26/02066/FUL",
        "council": "Bradford",
        "url": "https://planning.bradford.gov.uk/online-applications/applicationDetails.do?activeTab=makeComment&keyVal=TGENU6DHINE00",
    },
    {
        "uid": "PA26/03485",
        "council": "Cornwall",
        "url": "https://planning.cornwall.gov.uk/online-applications/applicationDetails.do?activeTab=summary&keyVal=TFCDXTFGGSY00",
    },
    {
        "uid": "SEARCH_ONLY",
        "council": "BracknellForest",
        "url": "https://publicaccess.bracknell-forest.gov.uk/s/register-view?c__r=Arcus_BE_Public_Register",
    },
]


def _handler(request: httpx.Request) -> httpx.Response:
    # Document downloads carry no activeTab param, so they must be routed by
    # path before the tab routing below.
    if "/online-applications/files/" in request.url.path:
        return httpx.Response(
            200,
            content=FAKE_PDF_BYTES,
            headers={"content-type": "application/pdf"},
            request=request,
        )

    host = request.url.host
    tab = request.url.params.get("activeTab")
    fixture = ROUTES.get((host, tab))
    if fixture is not None:
        body = (FIXTURES / fixture).read_text(encoding="utf-8")
        return httpx.Response(200, text=body, request=request)
    # contacts/dates tabs: synthetic stub until real fixtures land
    # post-refetch. See NOTES.md — live re-fetch pending.
    if tab in ("contacts", "dates"):
        return httpx.Response(200, text=f"<html>{tab} stub</html>", request=request)
    return httpx.Response(404, text="unmapped route", request=request)


def test_integration_sample_run(tmp_path, monkeypatch):
    input_path = tmp_path / "jobs.json"
    input_path.write_text(json.dumps(JOBS), encoding="utf-8")
    out_dir = tmp_path / "out"

    # Inject the mock transport into the client runner.run() builds, without
    # changing the CLI surface: wrap run() to pin transport, patch the name
    # cli imported. monkeypatch auto-reverts after the test.
    transport = httpx.MockTransport(_handler)
    from planit_spike import runner
    real_run = runner.run

    async def run_with_mock(*args, **kwargs):
        kwargs["transport"] = transport
        return await real_run(*args, **kwargs)

    monkeypatch.setattr(cli, "run", run_with_mock)

    rc = cli.main([
        "fetch",
        "--input", str(input_path),
        "--output-dir", str(out_dir),
        "--per-host-delay", "0",
    ])

    assert rc == 0

    # Supported jobs: all four tab files exist and are non-empty.
    brad = out_dir / "Bradford" / "26_02066_FUL"
    corn = out_dir / "Cornwall" / "PA26_03485"
    for d in (brad, corn):
        for name in ("details.html", "docs.html", "contacts.html", "dates.html"):
            assert (d / name).exists()
            assert (d / name).stat().st_size > 0

    # details/docs round-trip real fixtures (contacts/dates are stubs for now).
    assert (brad / "details.html").read_text(encoding="utf-8") == \
        (FIXTURES / "bradford_details.html").read_text(encoding="utf-8")
    assert (brad / "docs.html").read_text(encoding="utf-8") == \
        (FIXTURES / "bradford_docs.html").read_text(encoding="utf-8")

    # Documents: parsed from the real docs.html fixtures and downloaded through
    # the same client. Bradford's table lists 9, Cornwall's 19.
    brad_docs = sorted((brad / "documents").iterdir())
    assert len(brad_docs) == 9
    assert [p.name[:3] for p in brad_docs] == [f"{i:02d}_" for i in range(1, 10)]
    # Bradford's last two rows are .xlsx; the rest are .pdf.
    assert [p.suffix for p in brad_docs] == [".pdf"] * 7 + [".xlsx"] * 2

    corn_docs = sorted((corn / "documents").iterdir())
    assert len(corn_docs) == 19
    assert [p.name[:3] for p in corn_docs] == [f"{i:02d}_" for i in range(1, 20)]

    # Every document was streamed to disk, not merely created.
    for path in brad_docs + corn_docs:
        assert path.stat().st_size == len(FAKE_PDF_BYTES)

    # Non-Idox job: skipped, no directory created.
    assert not (out_dir / "BracknellForest").exists()

    # Report written and correct.
    report_path = out_dir / "run_report.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["totals"]["supported"] == 2
    assert report["totals"]["skipped"] == 1
    assert report["totals"]["fetches_attempted"] == 8   # 2 jobs × 4 tabs
    assert report["totals"]["fetches_ok"] == 8

    # Documents: 9 Bradford + 19 Cornwall, all served by the mock handler.
    assert report["totals"]["documents_attempted"] == 28
    assert report["totals"]["documents_ok"] == 28
    assert report["totals"]["documents_failed"] == 0
    assert report["totals"]["documents_total_bytes"] == 28 * len(FAKE_PDF_BYTES)
    assert report["totals"]["records_with_pagination"] == 0

    # Per-council document tallies.
    assert report["per_council"]["Bradford"]["documents_ok"] == 9
    assert report["per_council"]["Bradford"]["documents_failed"] == 0
    assert report["per_council"]["Bradford"]["documents_total_bytes"] == 9 * len(FAKE_PDF_BYTES)
    assert report["per_council"]["Cornwall"]["documents_ok"] == 19
    assert report["per_council"]["Cornwall"]["documents_failed"] == 0
    assert report["per_council"]["Cornwall"]["documents_total_bytes"] == 19 * len(FAKE_PDF_BYTES)

    # Per-job documents block for Bradford.
    bradford_job = next(j for j in report["jobs"] if j["council"] == "Bradford")
    assert bradford_job["documents"]["ok"] == 9
    assert bradford_job["documents"]["failed"] == 0
    assert bradford_job["documents"]["pagination_detected"] is False
