"""Command-line entry point: subcommands for daily-discovery and file-driven fetch.

  planit-spike run   --date YYYY-MM-DD [--limit N]   # PlanIt discovery + fetch
  planit-spike fetch --input jobs.json               # fetch from a jobs file

Shared flags (--output-dir, --concurrency, --per-host-delay, --verbose,
--insecure-hosts) live on a parent parser inherited by both subcommands.
main(argv=None) is importable for tests; the [project.scripts] entry calls it.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

from . import config
from .daily import run_daily
from .report import summarise, write_daily_report, write_report
from .runner import run


def _parse_insecure_hosts(raw: str) -> set[str]:
    """Split the comma-separated --insecure-hosts value into a set of hostnames."""
    return {h.strip() for h in raw.split(",") if h.strip()}


def _shared_parser() -> argparse.ArgumentParser:
    """Parent parser holding flags common to every subcommand (add_help=False)."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--output-dir", type=Path, default=Path("output"),
                   help="Root output directory (default: ./output)")
    p.add_argument("--concurrency", type=int, default=config.DEFAULT_CONCURRENCY,
                   help=f"Concurrent fetches (default: {config.DEFAULT_CONCURRENCY})")
    p.add_argument("--per-host-delay", type=float, default=config.DEFAULT_PER_HOST_DELAY,
                   help=f"Delay between fetches on the same host in seconds "
                        f"(default: {config.DEFAULT_PER_HOST_DELAY})")
    p.add_argument("--verbose", action="store_true", help="Verbose logging")
    p.add_argument("--insecure-hosts", type=str, default="",
                   help="Comma-separated hostnames to allow with SSL verification "
                        "DISABLED. Last resort for broken cert chains. Example: "
                        "--insecure-hosts pa.midkent.gov.uk")
    return p


def _build_parser() -> argparse.ArgumentParser:
    """Construct the top-level parser with 'run' and 'fetch' subcommands."""
    parser = argparse.ArgumentParser(description="planit-spike")
    shared = _shared_parser()
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", parents=[shared],
                           help="Discover a day's PlanIt records and fetch Idox portals")
    run_p.add_argument("--date", required=True,
                       help="Discovery date, YYYY-MM-DD (PlanIt 'created on' feed)")
    run_p.add_argument("--limit", type=int, default=None,
                       help="Process only the first N PlanIt records (testing)")

    fetch_p = sub.add_parser("fetch", parents=[shared],
                             help="Fetch council portals from a jobs JSON file")
    fetch_p.add_argument("--input", type=Path, required=True,
                         help="Path to jobs JSON file")

    return parser


def _setup_logging(verbose: bool) -> logging.Logger:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    return logging.getLogger(config.LOGGER_NAME)


def _cmd_fetch(args, logger) -> int:
    """fetch subcommand: run from a jobs file (the original behaviour)."""
    if not args.input.exists():
        logger.error(f"Input file not found: {args.input}")
        return 2
    args.output_dir.mkdir(parents=True, exist_ok=True)

    start = time.perf_counter()
    results = asyncio.run(run(
        input_path=args.input,
        output_root=args.output_dir,
        concurrency=args.concurrency,
        per_host_delay=args.per_host_delay,
        logger=logger,
        insecure_hosts=_parse_insecure_hosts(args.insecure_hosts),
    ))
    wall_time = time.perf_counter() - start

    summarise(results)
    report_path = args.output_dir / "run_report.json"
    write_report(results, report_path, wall_time=wall_time)
    logger.info(f"Wrote report to {report_path}")
    return 0


def _cmd_run(args, logger) -> int:
    """run subcommand: PlanIt discovery for --date, then fetch Idox portals."""
    date_root = args.output_dir / args.date
    date_root.mkdir(parents=True, exist_ok=True)

    start = time.perf_counter()
    records, results = asyncio.run(run_daily(
        date=args.date,
        output_root=date_root,
        concurrency=args.concurrency,
        per_host_delay=args.per_host_delay,
        logger=logger,
        insecure_hosts=_parse_insecure_hosts(args.insecure_hosts),
        limit=args.limit,
    ))
    wall_time = time.perf_counter() - start

    summarise(results)
    report_path = date_root / "run_report.json"
    write_daily_report(records, results, report_path, wall_time=wall_time)
    logger.info(f"Wrote report to {report_path}")
    return 0


def main(argv=None) -> int:
    """Parse args, dispatch to the chosen subcommand, return an exit code."""
    args = _build_parser().parse_args(argv)
    logger = _setup_logging(args.verbose)

    if args.command == "run":
        return _cmd_run(args, logger)
    return _cmd_fetch(args, logger)


if __name__ == "__main__":
    sys.exit(main())
