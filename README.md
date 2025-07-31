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
uv run reconkit wp-detect examples/domains.csv --format json -o results
cat examples/domains.csv | uv run reconkit live-check -
```

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
