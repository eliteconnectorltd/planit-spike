"""
planit-fetcher spike
--------------------

Reads a list of PlanIt job records from a JSON file and, for each Idox job,
fetches the details page (activeTab=summary) and the documents page
(activeTab=documents) as raw HTML. Saves both to
    output/{council}/{uid}/details.html
    output/{council}/{uid}/docs.html

Non-Idox portals are skipped and logged as unsupported.

Usage:
    python fetcher.py --input jobs.json --output-dir output/

Input JSON shape (list of jobs):
    [
      {
        "uid": "2025/0335",
        "council": "Wandsworth",
        "url": "https://planning.wandsworth.gov.uk/Northgate/...",
        "docs_url": "https://planning2.wandsworth.gov.uk/planningcase/comments.aspx?case=2025/0335"
      },
      ...
    ]

    Either PlanIt list-record shape works too — the script tolerates records
    that come straight from PlanIt's /api/applics/json response, using
    record["url"], record["uid"], record["area_name"], and
    record["other_fields"]["docs_url"].
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx

try:
    import certifi
    _CERTIFI_PATH = certifi.where()
except ImportError:
    _CERTIFI_PATH = None


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

DEFAULT_TIMEOUT = 30.0           # seconds per HTTP request
DEFAULT_CONCURRENCY = 4          # simultaneous fetches
DEFAULT_PER_HOST_DELAY = 1.5     # seconds between consecutive fetches on same host
DEFAULT_MAX_RETRIES = 3          # retry attempts on connection errors
DEFAULT_RETRY_BACKOFF = 2.0      # base seconds for exponential backoff between retries
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9,en-US;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


# -----------------------------------------------------------------------------
# Portal detection & URL normalisation
# -----------------------------------------------------------------------------

IDOX_PATH_MARKERS = ("applicationDetails.do",)


def is_idox_url(url: str) -> bool:
    """Return True if the URL looks like an Idox details page."""
    if not url:
        return False
    return any(m in url for m in IDOX_PATH_MARKERS)


def set_active_tab(url: str, tab: str) -> str:
    """Rewrite the activeTab query param on an Idox URL. Preserves keyVal."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs["activeTab"] = [tab]
    new_query = urlencode({k: v[0] for k, v in qs.items()}, safe="/")
    return urlunparse(parsed._replace(query=new_query))


def derive_urls(job: dict) -> tuple[Optional[str], Optional[str]]:
    """
    Given a job record, return (details_url, docs_url).

    Rules:
      - If job['url'] is an Idox URL, normalise it:
          details -> activeTab=summary
          docs    -> activeTab=documents
      - If job['docs_url'] is explicitly provided and is an Idox URL, prefer
        that for docs; otherwise derive docs from job['url'].
      - Non-Idox: return (None, None) — caller skips.
    """
    url = job.get("url") or job.get("details_url")
    if not url or not is_idox_url(url):
        return (None, None)

    details_url = set_active_tab(url, "summary")

    docs_url_input = job.get("docs_url") or job.get("other_fields", {}).get("docs_url")
    if docs_url_input and is_idox_url(docs_url_input):
        docs_url = set_active_tab(docs_url_input, "documents")
    else:
        docs_url = set_active_tab(url, "documents")

    return (details_url, docs_url)


# -----------------------------------------------------------------------------
# Filesystem helpers
# -----------------------------------------------------------------------------

_UID_SANITISE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_path_part(s: str) -> str:
    """Make a string safe as a filesystem path segment. e.g. '2025/0335' -> '2025_0335'."""
    return _UID_SANITISE.sub("_", s).strip("_") or "unknown"


def job_output_dir(root: Path, council: str, uid: str) -> Path:
    return root / safe_path_part(council) / safe_path_part(uid)


# -----------------------------------------------------------------------------
# Fetch result types
# -----------------------------------------------------------------------------

@dataclass
class FetchResult:
    council: str
    uid: str
    kind: str                       # "details" or "docs"
    url: str
    status_code: Optional[int] = None
    saved_path: Optional[str] = None
    byte_size: Optional[int] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status_code == 200 and self.error is None


@dataclass
class JobResult:
    council: str
    uid: str
    supported: bool
    details: Optional[FetchResult] = None
    docs: Optional[FetchResult] = None
    skip_reason: Optional[str] = None
    events: list[str] = field(default_factory=list)


# -----------------------------------------------------------------------------
# Fetch logic
# -----------------------------------------------------------------------------

class HostThrottle:
    """Enforce a minimum delay between requests to the same host."""

    def __init__(self, delay: float):
        self.delay = delay
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, host: str) -> asyncio.Lock:
        if host not in self._locks:
            self._locks[host] = asyncio.Lock()
        return self._locks[host]

    async def wait(self, host: str) -> None:
        async with self._lock(host):
            loop = asyncio.get_running_loop()
            now = loop.time()
            last = self._last.get(host, 0.0)
            wait_for = last + self.delay - now
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            self._last[host] = loop.time()


async def fetch_one(
    pick_client,
    throttle: HostThrottle,
    council: str,
    uid: str,
    kind: str,
    url: str,
    out_path: Path,
    logger: logging.Logger,
    max_retries: int = DEFAULT_MAX_RETRIES,
    retry_backoff: float = DEFAULT_RETRY_BACKOFF,
) -> FetchResult:
    """Fetch a single URL and save the response body to disk.

    Retries on transient ConnectError / ReadTimeout with exponential backoff.
    ``pick_client`` is a callable that returns the appropriate httpx.AsyncClient
    for a given URL (this lets us route hosts in --insecure-hosts through a
    separate client with SSL verification disabled).
    """
    result = FetchResult(council=council, uid=uid, kind=kind, url=url)
    host = urlparse(url).netloc
    client = pick_client(url)

    last_exception: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        await throttle.wait(host)
        try:
            attempt_note = f" (attempt {attempt}/{max_retries})" if attempt > 1 else ""
            logger.info(f"[{council}/{uid}/{kind}] GET {url}{attempt_note}")
            response = await client.get(url, follow_redirects=True, timeout=DEFAULT_TIMEOUT)
            result.status_code = response.status_code

            if response.status_code != 200:
                result.error = f"HTTP {response.status_code}"
                logger.warning(f"[{council}/{uid}/{kind}] HTTP {response.status_code}")
                return result

            body = response.text
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(body, encoding="utf-8")
            result.saved_path = str(out_path)
            result.byte_size = len(body.encode("utf-8"))
            logger.info(
                f"[{council}/{uid}/{kind}] saved {result.byte_size} bytes -> {out_path}"
            )
            return result

        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            last_exception = e
            if attempt < max_retries:
                wait = retry_backoff * (2 ** (attempt - 1))
                logger.warning(
                    f"[{council}/{uid}/{kind}] {type(e).__name__}: {e} — "
                    f"retrying in {wait:.1f}s"
                )
                await asyncio.sleep(wait)
                continue
            result.error = f"{type(e).__name__}: {e}"
            logger.error(
                f"[{council}/{uid}/{kind}] {result.error} (after {max_retries} attempts)"
            )
            return result
        except httpx.HTTPError as e:
            result.error = f"{type(e).__name__}: {e}"
            logger.error(f"[{council}/{uid}/{kind}] {result.error}")
            return result
        except Exception as e:
            result.error = f"{type(e).__name__}: {e}"
            logger.exception(f"[{council}/{uid}/{kind}] unexpected error")
            return result

    # Should not reach here, but be safe
    result.error = f"exhausted retries: {last_exception}"
    return result


async def process_job(
    pick_client,
    throttle: HostThrottle,
    job: dict,
    output_root: Path,
    logger: logging.Logger,
    sem: asyncio.Semaphore,
) -> JobResult:
    """Process a single job: derive URLs, fetch both, save both."""
    uid = job.get("uid") or job.get("name") or "unknown"
    council = job.get("council") or job.get("area_name") or "unknown"

    details_url, docs_url = derive_urls(job)
    if not details_url:
        return JobResult(
            council=council,
            uid=uid,
            supported=False,
            skip_reason="Not an Idox portal (unsupported family in this spike)",
        )

    async with sem:
        job_dir = job_output_dir(output_root, council, uid)
        details_task = fetch_one(
            pick_client, throttle, council, uid, "details",
            details_url, job_dir / "details.html", logger,
        )
        docs_task = fetch_one(
            pick_client, throttle, council, uid, "docs",
            docs_url, job_dir / "docs.html", logger,
        )
        details_res, docs_res = await asyncio.gather(details_task, docs_task)

    return JobResult(
        council=council, uid=uid, supported=True,
        details=details_res, docs=docs_res,
    )


# -----------------------------------------------------------------------------
# Orchestration
# -----------------------------------------------------------------------------

async def run(
    input_path: Path,
    output_root: Path,
    concurrency: int,
    per_host_delay: float,
    logger: logging.Logger,
    insecure_hosts: Optional[set[str]] = None,
) -> list[JobResult]:
    raw = json.loads(input_path.read_text(encoding="utf-8"))

    # Accept either a bare list or a PlanIt envelope { "records": [...] }
    if isinstance(raw, dict) and "records" in raw:
        jobs = raw["records"]
    elif isinstance(raw, list):
        jobs = raw
    else:
        raise ValueError(
            "Input JSON must be a list of job objects or a PlanIt envelope "
            "with a 'records' key."
        )

    logger.info(f"Loaded {len(jobs)} jobs from {input_path}")

    throttle = HostThrottle(per_host_delay)
    sem = asyncio.Semaphore(concurrency)
    insecure_hosts = insecure_hosts or set()

    # SSL context: build one explicitly from certifi's CA bundle. Passing an
    # SSLContext to httpx is more reliable than passing a file path, especially
    # on Windows where Python may not pick up the OS certificate store.
    import ssl as _ssl
    if _CERTIFI_PATH:
        ssl_ctx = _ssl.create_default_context(cafile=_CERTIFI_PATH)
        logger.info(f"Using certifi CA bundle at {_CERTIFI_PATH}")
    else:
        ssl_ctx = _ssl.create_default_context()
        logger.info("certifi not installed; using system default CA bundle")

    if insecure_hosts:
        logger.warning(
            f"SSL verification DISABLED for hosts: {sorted(insecure_hosts)} "
            f"(--insecure-hosts). Do not use in production."
        )

    secure_client = httpx.AsyncClient(
        headers=BROWSER_HEADERS, http2=False, verify=ssl_ctx,
    )
    insecure_client = httpx.AsyncClient(
        headers=BROWSER_HEADERS, http2=False, verify=False,
    ) if insecure_hosts else None

    try:
        # Attach a helper to pick the right client per URL
        def pick_client(url: str) -> httpx.AsyncClient:
            host = urlparse(url).netloc
            if insecure_client is not None and host in insecure_hosts:
                return insecure_client
            return secure_client

        tasks = [
            process_job(pick_client, throttle, job, output_root, logger, sem)
            for job in jobs
        ]
        results = await asyncio.gather(*tasks)
    finally:
        await secure_client.aclose()
        if insecure_client is not None:
            await insecure_client.aclose()

    return results


def summarise(results: list[JobResult]) -> None:
    total = len(results)
    supported = [r for r in results if r.supported]
    skipped = [r for r in results if not r.supported]

    details_ok = sum(1 for r in supported if r.details and r.details.ok)
    docs_ok = sum(1 for r in supported if r.docs and r.docs.ok)

    print()
    print("=" * 60)
    print(f"Total jobs:            {total}")
    print(f"  Supported (Idox):    {len(supported)}")
    print(f"  Skipped (other):     {len(skipped)}")
    print(f"  Details fetched OK:  {details_ok}/{len(supported)}")
    print(f"  Docs fetched OK:     {docs_ok}/{len(supported)}")
    print("=" * 60)

    if skipped:
        print("\nSkipped jobs:")
        for r in skipped:
            print(f"  - {r.council}/{r.uid}: {r.skip_reason}")

    failed = [
        r for r in supported
        if (r.details and not r.details.ok) or (r.docs and not r.docs.ok)
    ]
    if failed:
        print("\nJobs with fetch failures:")
        for r in failed:
            if r.details and not r.details.ok:
                print(f"  - {r.council}/{r.uid} details: "
                      f"{r.details.error or r.details.status_code}")
            if r.docs and not r.docs.ok:
                print(f"  - {r.council}/{r.uid} docs: "
                      f"{r.docs.error or r.docs.status_code}")


def main() -> int:
    parser = argparse.ArgumentParser(description="planit-fetcher spike")
    parser.add_argument("--input", type=Path, required=True,
                        help="Path to jobs JSON file")
    parser.add_argument("--output-dir", type=Path, default=Path("output"),
                        help="Root output directory (default: ./output)")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"Concurrent fetches (default: {DEFAULT_CONCURRENCY})")
    parser.add_argument("--per-host-delay", type=float, default=DEFAULT_PER_HOST_DELAY,
                        help=f"Delay between fetches on the same host in seconds "
                             f"(default: {DEFAULT_PER_HOST_DELAY})")
    parser.add_argument("--verbose", action="store_true",
                        help="Verbose logging")
    parser.add_argument("--insecure-hosts", type=str, default="",
                        help="Comma-separated list of hostnames to allow with SSL "
                             "verification DISABLED. Use only as a last resort for "
                             "hosts with broken cert chains. Example: "
                             "--insecure-hosts pa.midkent.gov.uk")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("planit-fetcher")

    if not args.input.exists():
        logger.error(f"Input file not found: {args.input}")
        return 2

    args.output_dir.mkdir(parents=True, exist_ok=True)

    insecure_hosts = {
        h.strip() for h in args.insecure_hosts.split(",") if h.strip()
    }

    results = asyncio.run(run(
        input_path=args.input,
        output_root=args.output_dir,
        concurrency=args.concurrency,
        per_host_delay=args.per_host_delay,
        logger=logger,
        insecure_hosts=insecure_hosts,
    ))
    summarise(results)

    # Write a run report next to the output
    report_path = args.output_dir / "run_report.json"
    report = [
        {
            "council": r.council,
            "uid": r.uid,
            "supported": r.supported,
            "skip_reason": r.skip_reason,
            "details": r.details.__dict__ if r.details else None,
            "docs": r.docs.__dict__ if r.docs else None,
        }
        for r in results
    ]
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    logger.info(f"Wrote report to {report_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
