"""reconkit command-line interface."""

from __future__ import annotations

import typer

from .commands import (
    cloudflare_subdomains,
    error_spikes,
    live_check,
    wp_detect,
    wp_users,
)
from .core.io import read_domains, read_upstream, write_results
from .core.models import ProbeResult

app = typer.Typer(help="A small toolkit of web-recon commands.", no_args_is_help=True)

FormatOpt = typer.Option("csv", "--format", "-f", help="Output format: csv or json.")
OutDirOpt = typer.Option(
    "results", "--out-dir", "-o", help="Directory for result files."
)
FromOpt = typer.Option(
    None,
    "--from",
    help="Chain from a previous result CSV: reuses the discovered scheme and "
    "skips domains that were unreachable. Omit to probe a plain domain list.",
)


def _resolve_input(
    source: str | None, from_csv: str | None, column: int
) -> tuple[list[str], dict[str, str], list[ProbeResult]]:
    """Return (domains, schemes, carried-through SKIPPED rows).

    Standalone is the default. Without --from, schemes is empty and every
    domain gets normal HTTPS->HTTP fallback.
    """
    if from_csv and source:
        raise typer.BadParameter("Pass a domain list or --from, not both.")
    if from_csv:
        up = read_upstream(from_csv)
        return up.domains, up.schemes, up.skipped_results()
    if not source:
        raise typer.BadParameter("Provide a domain list or --from.")
    return read_domains(source, column), {}, []


def _emit(results: list[ProbeResult], out_dir: str, stem: str, fmt: str) -> None:
    path = write_results(results, out_dir, stem, fmt)
    counts: dict[str, int] = {}
    for r in results:
        counts[r.outcome.value] = counts.get(r.outcome.value, 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    typer.echo(f"{len(results)} rows ({summary}) -> {path}")


@app.command("live-check")
def live_check_cmd(
    source: str = typer.Argument(..., help="CSV of domains, or '-' for stdin."),
    column: int = typer.Option(0, help="Zero-based CSV column holding the domain."),
    out_dir: str = OutDirOpt,
    fmt: str = FormatOpt,
) -> None:
    """Check whether each domain responds over HTTP."""
    results = live_check.check_live(read_domains(source, column))
    _emit(results, out_dir, "live_check", fmt)


@app.command("wp-detect")
def wp_detect_cmd(
    source: str | None = typer.Argument(None, help="CSV of domains, or '-' for stdin."),
    from_csv: str | None = FromOpt,
    column: int = typer.Option(0),
    out_dir: str = OutDirOpt,
    fmt: str = FormatOpt,
) -> None:
    """Detect WordPress via the /wp-json endpoint."""
    domains, schemes, skipped = _resolve_input(source, from_csv, column)
    results = wp_detect.detect_wordpress(domains, schemes=schemes) + skipped
    _emit(results, out_dir, "wp_detect", fmt)


@app.command("wp-users")
def wp_users_cmd(
    source: str | None = typer.Argument(None, help="CSV of domains, or '-' for stdin."),
    from_csv: str | None = FromOpt,
    column: int = typer.Option(0),
    out_dir: str = OutDirOpt,
    fmt: str = FormatOpt,
) -> None:
    """Check whether WordPress sites expose user data via the REST API."""
    domains, schemes, skipped = _resolve_input(source, from_csv, column)
    results = wp_users.check_wp_users(domains, schemes=schemes) + skipped
    _emit(results, out_dir, "wp_users", fmt)


@app.command("cf-subdomains")
def cf_subdomains_cmd(
    api_token: str | None = typer.Option(
        None,
        "--api-token",
        envvar="CLOUDFLARE_API_TOKEN",
        help="Cloudflare API token. Falls back to $CLOUDFLARE_API_TOKEN. Never stored.",
    ),
    out_dir: str = OutDirOpt,
    fmt: str = FormatOpt,
) -> None:
    """Enumerate A-record subdomains across all zones in a Cloudflare account."""
    if not api_token:
        raise typer.BadParameter("Provide --api-token or set CLOUDFLARE_API_TOKEN.")
    results = cloudflare_subdomains.enumerate_subdomains(api_token)
    _emit(results, out_dir, "cf_subdomains", fmt)


@app.command("error-spikes")
def error_spikes_cmd(
    project: str | None = typer.Option(
        None,
        "--project",
        "-p",
        envvar="GOOGLE_CLOUD_PROJECT",
        help="GCP project ID to scan. Required. Falls back to "
        "$GOOGLE_CLOUD_PROJECT. Never inferred from gcloud or ADC, which may "
        "point at a different project than the one you mean to audit.",
    ),
    hours: int = typer.Option(24, help="Size of the window to scan, in hours."),
    threshold: int = typer.Option(
        10, help="Minimum 5xx responses in one bucket to count as a spike."
    ),
    bucket_seconds: int = typer.Option(
        300, help="Alignment bucket width, in seconds."
    ),
    samples: int = typer.Option(
        0,
        "--samples",
        help="Sample up to N 5xx requests per spike from Cloud Logging. "
        "0 (the default) reads no logs at all.",
    ),
    include_client_detail: bool = typer.Option(
        False,
        "--include-client-detail",
        help="Include client IP, user agent, referer and query string in "
        "samples. Off by default: these identify people and client sites, "
        "and the results file is written to disk.",
    ),
    out_dir: str = OutDirOpt,
    fmt: str = FormatOpt,
) -> None:
    """Collect historical Cloud Run error-spike data for one GCP project."""
    if not project:
        raise typer.BadParameter(
            "Provide --project or set GOOGLE_CLOUD_PROJECT. "
            "error-spikes does not fall back to the ADC default project."
        )
    if include_client_detail and samples <= 0:
        raise typer.BadParameter(
            "--include-client-detail only applies with --samples N."
        )
    try:
        results = error_spikes.find_spikes(
            project,
            hours=hours,
            threshold=threshold,
            alignment_seconds=bucket_seconds,
            samples=samples,
            include_client_detail=include_client_detail,
        )
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if include_client_detail:
        typer.echo(
            "warning: samples include client IPs and user agents; "
            "treat the output file as personal data.",
            err=True,
        )
    _emit(results, out_dir, "error_spikes", fmt)


if __name__ == "__main__":
    app()
