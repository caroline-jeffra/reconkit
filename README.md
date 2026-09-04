# reconkit

[![CI](https://github.com/caroline-jeffra/reconkit/actions/workflows/ci.yml/badge.svg)](https://github.com/caroline-jeffra/reconkit/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)

A small toolkit of web-reconnaissance commands, packaged as an installable
CLI and an importable Python library. Give it a CSV of domains and it probes
each one; results are written as CSV or JSON.

## Authorized use only

**Only point these commands at sites you own or have written permission to
test.** Scanning systems without authorization is illegal in many
jurisdictions, regardless of intent or how light the traffic is.

The recommended way to stay on the right side of that line is to let
Cloudflare decide what you are allowed to scan — see
[Scan what you own](#scan-what-you-own) below. An API token only lists zones
in your own account, so a domain list built that way is inherently
ownership-scoped.

What these commands do: unauthenticated `GET` requests to public endpoints,
recording the status and whether a public API returned data. What they do not
do: authenticate, log in, submit forms, guess credentials, exploit anything,
or attempt to access non-public data. `wp-users` reads
`/wp-json/wp/v2/users`, which WordPress serves publicly by default — it
reports what an anonymous visitor could already see.

That still makes real requests to real servers. You are responsible for
having permission to send them.

Found a security issue in `reconkit` itself? See [SECURITY.md](SECURITY.md).
If the tool showed you a problem on someone *else's* site, report it to that
site's owner — this repository is not a disclosure channel for third parties.

| Command          | What it does                                             |
|------------------|----------------------------------------------------------|
| `live-check`     | Is the domain responding over HTTP?                      |
| `wp-detect`      | Is it running WordPress? (probes `/wp-json`)             |
| `wp-users`       | Does the WP REST API leak usernames?                     |
| `cf-subdomains`  | Enumerate A-record subdomains from a Cloudflare account  |
| `error-spikes`   | Find historical Cloud Run 5xx spikes in a GCP project    |

## Install & run (uv)

Requires Python 3.11 or newer.

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

## Scan what you own

The recommended workflow starts from Cloudflare rather than a hand-written
CSV. `cf-subdomains` enumerates the A records across every zone in your
Cloudflare account, and its output feeds straight into the other commands
with `--from`:

```bash
export CLOUDFLARE_API_TOKEN=xxxxx
uv run reconkit cf-subdomains -o results                        # your zones
uv run reconkit wp-detect --from results/cf_subdomains.csv -o results
uv run reconkit wp-users  --from results/wp_detect.csv -o results
```

The token is what makes this the safe default: Cloudflare only returns zones
belonging to your account, so the domain list cannot contain someone else's
site. Nothing enforces this — the commands accept any CSV, and the library
accepts any list of strings — but starting from your own zones means the
question of authorization is settled before the first request goes out.

A read-only token with `Zone:Read` and `DNS:Read` is sufficient.

If your DNS is somewhere other than Cloudflare, build the CSV another way
from a source you control — an inventory export or a registrar's zone
list — rather than by hand, for the same reason.

## Chaining any list

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

## Request behaviour

Worth knowing before pointing this at a long list:

- **One request at a time.** Domains are probed sequentially, never in
  parallel, and each command sends one request per domain (two if the first
  scheme fails and it falls back).
- **No delay between domains.** A long list becomes a steady stream of
  requests. Nothing here is throttled, so if you are scanning a few hundred
  domains that share infrastructure, consider splitting the list.
- **429 is respected, not ignored.** Rate-limit and transient 5xx responses
  are retried twice with exponential backoff. If the limit persists, the
  domain is reported `inconclusive` rather than being recorded as a negative
  result — a throttled site is an unknown, not a clean one.
- **Every request identifies itself.** The `User-Agent` names the tool and
  links to this repository, so an administrator reading their logs can see
  what reached them and why:

  ```
  reconkit/0.1.0 (+https://github.com/caroline-jeffra/reconkit)
  ```

## Use as a library

```python
from reconkit.commands.wp_detect import detect_wordpress

results = detect_wordpress(["example.com"])
```

## Cloudflare auth

`cf-subdomains` reads a token from `--api-token` or `$CLOUDFLARE_API_TOKEN`.
It is used only at runtime and never written to disk.

A read-only token is enough: `Zone:Read` (to list your zones) and `DNS:Read`
(to read their A records). Nothing here writes to Cloudflare.

```bash
CLOUDFLARE_API_TOKEN=xxxxx uv run reconkit cf-subdomains -o results
```

## GCP auth (error-spikes)

`error-spikes` is standalone data collection: it takes no domain list and
reads Cloud Monitoring, not HTTP. It needs the optional extra and Application
Default Credentials.

```bash
uv sync --extra gcp
gcloud auth application-default login \
  --scopes=https://www.googleapis.com/auth/monitoring.read,https://www.googleapis.com/auth/logging.read
```

**The project is always explicit.** Pass `--project` or set
`$GOOGLE_CLOUD_PROJECT`. The command never falls back to the ADC default
project, because ADC resolves to whichever project was last configured in
gcloud — which is easily not the one you mean to audit, and a wrong project
returns plausible-looking data for the wrong service.

```bash
# spike timing and duration, no logs read at all
uv run reconkit error-spikes --project my-proj --hours 48 --threshold 25

# add ~150 requests either side of each spike's peak
uv run reconkit error-spikes -p my-proj --samples 300
```

Consecutive breaching buckets collapse into one spike, so each row reports
`started`, `ended`, `duration_minutes`, `peak_at`, `peak_errors` and
`total_errors` rather than one row per bucket.

### Samples straddle the peak

`--samples N` reads Cloud Logging around each spike. The budget is split
evenly: half walking backwards from the peak bucket, half forwards, so
`--samples 300` gives roughly 150 requests either side. Sampling is centred on
the **peak**, not the spike's start, so a long spike with one sharp burst
samples the moment that matters.

**All status codes are collected.** The successful requests during a spike are
usually what explain it — a flood of asset requests, or a 200 served in four
seconds. Filtering to 5xx throws that context away.

### What samples contain

Each sample keeps the request URL, method, status, latency, protocol, request
and response sizes, server IP, and timestamp. The URL is the most diagnostic
field in an access log, so it is kept in full — **minus the query string**.

Query strings are dropped because they carry tokens, reset keys, session IDs
and email addresses. Client IP, user agent and referer are dropped too: they
identify people and the pages they were reading.

`--include-client-detail` opts back in to all four. It requires `--samples`
and prints a warning, because the results file then contains personal data and
lands on disk. Leave it off unless you have a specific reason, such as
confirming a single client is causing the spike.

### Read quota

Cloud Logging allows **120 `entries.list` reads per minute per project**. A
large `--samples` across several spikes will hit it. The command backs off and
retries, so a big scan can take a few minutes; if the quota is still exhausted
it says so and suggests lowering `--samples`, narrowing `--hours`, or raising
`--threshold`.

## Design

Logic lives in `reconkit.commands` and `reconkit.core` as pure functions that
return `ProbeResult` objects — no printing, no `argv`, no hardcoded paths. Only
`cli.py` handles I/O and formatting. That split keeps the logic reusable and
testable (see `tests/`, which mock HTTP and run offline).

## Development

```bash
uv sync                      # includes dev dependencies
uv run pytest --cov          # 98 tests, offline (all HTTP is mocked)
uv run ruff check .          # lint
uv run ruff format .         # format
```

CI runs the same three commands on Python 3.11–3.13, and separately builds the
package and runs the installed wheel.
