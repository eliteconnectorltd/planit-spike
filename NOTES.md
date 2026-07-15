# planit-spike — running notes

Operational notes and environmental gotchas discovered during development.
Not a design doc; a running log of things that will bite a future run.

## Council-specific fetch quirks

- **MidKent (pa.midkent.gov.uk) requires `--insecure-hosts` as of 2026-07-17.**
  Council presents an incomplete cert chain that certifi can't verify.
  Verified environmental, not client-side.

## Phase 3 — daily pipeline

- **The `run` subcommand requires a UK-egress VPN** because PlanIt's WAF
  geoblocks non-UK IPs. Verified 2026-07-13 with Windscribe.

- **Daily mode filters non-Idox records BEFORE building jobs, so they never
  produce JobResults.** Result: `report.totals.skipped` can be 0 while
  `report.discovery.non_idox_records` is > 0. Fetch mode counts non-Idox as
  skipped because the JobResult is built. Not a bug; the two counts measure
  different provenance.

## Phase 4 — document downloading

- **Document downloads use silent overwrite (`open('wb')`).** Re-running a
  fetch over an existing `output_dir` replaces same-named files but does NOT
  clean up stale files from previous runs whose filenames changed. If
  accumulation becomes a problem, either reset `documents_dir` before each
  write, or track filenames written this run and delete anything else in
  `documents_dir` after.

- **Idox docs tables observed up to 19 documents on a single page.**
  Pagination beyond that is unobserved; the parser detects-and-warns rather
  than following pagination. If a real run produces
  `records_with_pagination > 0` in the report, add pagination-following
  support before those records' document lists can be trusted.

- **`DocumentFetchResult.saved_path` is `Path`, not `str`** (differs from
  `FetchResult.saved_path`). The report layer never serializes
  `DocumentFetchResults` directly — it reads `.ok` and `.byte_size` off them
  and emits aggregate counts and sums. If a future report shape needs
  per-document detail, extract only serializable fields, convert Path to str
  in the serialization layer, or add a JSON encoder that handles Path. Do NOT
  change the dataclass field type to str — Path is correct for in-memory
  filesystem paths.
