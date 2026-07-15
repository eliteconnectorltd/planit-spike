# planit-spike

Discovers new/updated UK planning applications via PlanIt (planit.org.uk) and
fetches the raw HTML metadata for each from the council's Idox portal, saving
it to disk. A later phase feeds the HTML to an LLM for structured extraction.

## Usage

    # Daily discovery: pull a day's PlanIt records, fetch the Idox ones.
    planit-spike run --date 2026-07-13 --output-dir output

    # File-driven: fetch councils listed in a jobs JSON file.
    planit-spike fetch --input jobs.json --output-dir output

## VPN

The `run` subcommand fetches PlanIt's API from a UK-only endpoint. Turn on your
VPN before running (any UK-egress VPN works). The `fetch` subcommand does not
need a VPN if the councils in your jobs file are reachable directly.
