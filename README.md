# reconkit

A small toolkit of web-reconnaissance commands, packaged as an installable
CLI and an importable Python library. Give it a CSV of domains and it probes
each one; results are written as CSV or JSON.

## Commands

| Command          | What it does                                             |
|------------------|----------------------------------------------------------|
| `live-check`     | Is the domain responding over HTTP?                      |
| `wp-detect`      | Is it running WordPress? (probes `/wp-json`)             |
| `wp-users`       | Does the WP REST API leak usernames?                     |
| `cf-subdomains`  | Enumerate A-record subdomains from a Cloudflare account  |

## Install & run (uv)

```bash
uv sync                          # create venv, install reconkit + deps
uv run reconkit --help
uv run reconkit wp-detect examples/wp-detect.csv --format json -o results
cat examples/live-check.csv | uv run reconkit live-check -
```

## Example inputs

`examples/` holds one CSV per command that reads a domain list — bare domains,
one per line, no header row. Each file is chosen to exercise that command's
full range of outcomes, so you can see a positive and a negative on first run.

| File                       | Command      | Shows                                             |
|----------------------------|--------------|---------------------------------------------------|
| `examples/live-check.csv`  | `live-check` | responding hosts, plus one that never resolves    |
| `examples/wp-detect.csv`   | `wp-detect`  | WordPress sites and non-WordPress ones            |
| `examples/wp-users.csv`    | `wp-users`   | sites that enumerate authors, and ones that don't |

The `wp-users` list includes WordPress project sites that publish their
authors through the REST API, so it returns a genuine positive.

`cf-subdomains` takes no CSV — it reads zones from the Cloudflare API.

Commands can be chained, feeding one command's results into the next with
`--from`. Already-dead domains are skipped rather than re-probed:

```bash
uv run reconkit live-check examples/wp-users.csv -o results
uv run reconkit wp-detect --from results/live_check.csv -o results
uv run reconkit wp-users  --from results/wp_detect.csv -o results
```

These examples probe live third-party sites. Hosts that are hit repeatedly in
quick succession may rate-limit, which is reported as `inconclusive` rather
than mistaken for a negative.

## Use as a library

```python
from reconkit.commands.wp_detect import detect_wordpress

results = detect_wordpress(["example.com"])
```

## Cloudflare auth

`cf-subdomains` reads a token from `--api-token` or `$CLOUDFLARE_API_TOKEN`.
It is used only at runtime and never written to disk.

```bash
CLOUDFLARE_API_TOKEN=xxxxx uv run reconkit cf-subdomains -o results
```

## Design

Logic lives in `reconkit.commands` and `reconkit.core` as pure functions that
return `ProbeResult` objects — no printing, no `argv`, no hardcoded paths. Only
`cli.py` handles I/O and formatting. That split keeps the logic reusable and
testable (see `tests/`, which mock HTTP and run offline).
