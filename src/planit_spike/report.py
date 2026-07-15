"""Console summary and the richer JSON run report.

summarise() prints the human-facing block; build_report() assembles a
JSON-serializable dict (totals, per-council success rates, grouped error
reasons, timing); write_report() serializes it to disk. Imports types only.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import config
from .portal_detect import is_idox_url
from .types import JobResult


def _error_bucket(error: str) -> str:
    """Bucket an error string so per-host detail doesn't fragment the buckets.

    SSL errors keep their reason code; everything else buckets by bare class name:
      'ConnectError: All connection attempts failed'     -> 'ConnectError'
      'ReadTimeout: server took too long'                -> 'ReadTimeout'
      'HTTPError: HTTP 403'                              -> 'HTTPError'
      'SSL: CERTIFICATE_VERIFY_FAILED [certificate ...]' -> 'SSL: CERTIFICATE_VERIFY_FAILED'
      'SSLError: WRONG_VERSION_NUMBER'                   -> 'SSL: WRONG_VERSION_NUMBER'
      'RemoteProtocolError: peer closed connection'      -> 'RemoteProtocolError'
    """
    if error.startswith(("SSL:", "SSLError:")):
        after_colon = error.split(":", 1)[1].strip()
        reason = after_colon.split()[0] if after_colon.split() else ""
        return f"SSL: {reason}".rstrip()

    # Bare class name: up to the first ': ', ' (', or ' [' — whichever comes first.
    for delim in (": ", " (", " ["):
        idx = error.find(delim)
        if idx != -1:
            error = error[:idx]
    return error.strip()


def _document_tally(results: list[JobResult]) -> dict[str, int]:
    """Count documents across jobs: attempted, ok, failed, and ok bytes.

    Reads only .ok/.byte_size off each DocumentFetchResult — never serializes
    one, so saved_path (a Path, unlike FetchResult's str) never reaches JSON.
    """
    documents = [d for r in results for d in r.documents]
    ok = [d for d in documents if d.ok]
    return {
        "attempted": len(documents),
        "ok": len(ok),
        "failed": len(documents) - len(ok),
        "total_bytes": sum(d.byte_size for d in ok),
    }


def _format_bytes(count: int) -> str:
    """Human-readable byte count for the console block (JSON keeps raw ints)."""
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def summarise(results: list[JobResult]) -> None:
    """Print totals, per-portal counts, supported vs skipped, and grouped failures."""
    total = len(results)
    supported = [r for r in results if r.supported]
    skipped = [r for r in results if not r.supported]
    total_fetches = sum(len(r.fetches) for r in supported)
    ok_fetches = sum(1 for r in supported for f in r.fetches.values() if f.ok)
    documents = _document_tally(supported)
    paginated = sum(1 for r in supported if r.pagination_detected)

    print()
    print("=" * 60)
    print(f"Total jobs:            {total}")
    print(f"  Supported (Idox):    {len(supported)}")
    print(f"  Skipped (other):     {len(skipped)}")
    print(f"  Fetches OK:          {ok_fetches}/{total_fetches}")
    print(f"  Documents OK:        {documents['ok']}/{documents['attempted']}"
          f" ({_format_bytes(documents['total_bytes'])})")
    print("=" * 60)

    if paginated:
        print(f"\n{paginated} record(s) showed pagination; document lists may be incomplete.")

    if skipped:
        print("\nSkipped jobs:")
        for r in skipped:
            print(f"  - {r.council}/{r.uid}: {r.skip_reason}")

    failed = [r for r in supported if any(not f.ok for f in r.fetches.values())]
    if failed:
        print("\nJobs with fetch failures:")
        for r in failed:
            for kind, f in r.fetches.items():
                if not f.ok:
                    print(f"  - {r.council}/{r.uid} {kind}: "
                          f"{f.error or f.status_code}")


def build_report(results: list[JobResult], *, wall_time: float) -> dict:
    """Assemble the JSON-serializable run report: totals, per-council rates, errors, timing."""
    supported = [r for r in results if r.supported]
    skipped = [r for r in results if not r.supported]

    fetches = [f for r in supported for f in r.fetches.values()]
    total_fetches = len(fetches)
    documents = _document_tally(supported)

    per_council: dict[str, dict[str, list[int]]] = {}
    per_council_docs: dict[str, dict[str, int]] = {}
    for r in supported:
        c = per_council.setdefault(r.council, {k: [0, 0] for k in config.FETCH_KINDS})
        for kind, f in r.fetches.items():
            c.setdefault(kind, [0, 0])
            c[kind][0] += 1                  # attempted
            c[kind][1] += 1 if f.ok else 0   # ok
        per_council_docs.setdefault(
            r.council,
            {"documents_ok": 0, "documents_failed": 0, "documents_total_bytes": 0},
        )
        tally = _document_tally([r])
        per_council_docs[r.council]["documents_ok"] += tally["ok"]
        per_council_docs[r.council]["documents_failed"] += tally["failed"]
        per_council_docs[r.council]["documents_total_bytes"] += tally["total_bytes"]

    per_council_rates = {
        council: {
            **{kind: f"{ok}/{n}" for kind, (n, ok) in kinds.items() if n},
            **per_council_docs[council],
        }
        for council, kinds in per_council.items()
    }

    error_buckets: dict[str, int] = {}
    for f in fetches:
        if f and f.error:
            bucket = _error_bucket(f.error)
            error_buckets[bucket] = error_buckets.get(bucket, 0) + 1

    jobs_entries = []
    for r in results:
        tally = _document_tally([r])
        jobs_entries.append({
            "council": r.council,
            "uid": r.uid,
            "supported": r.supported,
            "skip_reason": r.skip_reason,
            "fetches": {k: f.__dict__ for k, f in r.fetches.items()},
            "documents": {
                "ok": tally["ok"],
                "failed": tally["failed"],
                "total_bytes": tally["total_bytes"],
                "pagination_detected": r.pagination_detected,
            },
        })

    return {
        "totals": {
            "jobs": len(results),
            "supported": len(supported),
            "skipped": len(skipped),
            "fetches_attempted": total_fetches,
            "fetches_ok": sum(1 for f in fetches if f.ok),
            "documents_attempted": documents["attempted"],
            "documents_ok": documents["ok"],
            "documents_failed": documents["failed"],
            "documents_total_bytes": documents["total_bytes"],
            "records_with_pagination": sum(1 for r in supported if r.pagination_detected),
        },
        "per_council": per_council_rates,
        "error_reasons": error_buckets,
        "timing": {
            "wall_time_seconds": round(wall_time, 3),
            "average_seconds_per_fetch": round(wall_time / max(1, total_fetches), 3),
        },
        "jobs": jobs_entries,
    }


def write_report(results: list[JobResult], path: Path, *, wall_time: float) -> None:
    """Serialize build_report(...) to JSON at path."""
    report = build_report(results, wall_time=wall_time)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")


def build_daily_report(
    records: list[dict],
    results: list[JobResult],
    *,
    wall_time: float,
) -> dict:
    """Assemble the daily run report: record-level discovery totals over the
    fetch report.

    Composes build_report(results, ...) for the fetch section (totals,
    per_council rates, error_reasons, timing, jobs) and adds discovery-level
    counts from the raw PlanIt records: total/idox/non-idox and per-council
    records-seen vs fetched.
    """
    fetch_report = build_report(results, wall_time=wall_time)

    idox = [r for r in records if is_idox_url(r.get("url") or "")]
    non_idox = [r for r in records if not is_idox_url(r.get("url") or "")]

    # records seen per council (all records), fetched per council (Idox jobs).
    seen: dict[str, int] = {}
    for r in records:
        council = r.get("area_name") or "unknown"
        seen[council] = seen.get(council, 0) + 1
    fetched: dict[str, int] = {}
    for jr in results:
        if jr.supported:
            fetched[jr.council] = fetched.get(jr.council, 0) + 1
    per_council_records = {
        council: {"seen": n, "fetched": fetched.get(council, 0)}
        for council, n in seen.items()
    }

    return {
        "totals": fetch_report["totals"],
        "discovery": {
            "total_records": len(records),
            "idox_records": len(idox),
            "non_idox_records": len(non_idox),
        },
        "per_council": fetch_report["per_council"],
        "per_council_records": per_council_records,
        "error_reasons": fetch_report["error_reasons"],
        "timing": fetch_report["timing"],
        "jobs": fetch_report["jobs"],
    }


def write_daily_report(
    records: list[dict],
    results: list[JobResult],
    path: Path,
    *,
    wall_time: float,
) -> None:
    """Serialize build_daily_report(...) to JSON at path."""
    report = build_daily_report(records, results, wall_time=wall_time)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
