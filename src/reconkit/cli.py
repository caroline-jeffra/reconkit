"""reconkit command-line interface."""

from __future__ import annotations

from typing import Optional

import typer

from .commands import cloudflare_subdomains, live_check, wp_detect, wp_users
from .core.io import read_domains, read_upstream, write_results
from .core.models import ProbeResult

app = typer.Typer(help="A small toolkit of web-recon commands.", no_args_is_help=True)

FormatOpt = typer.Option("csv", "--format", "-f", help="Output format: csv or json.")
OutDirOpt = typer.Option("results", "--out-dir", "-o", help="Directory for result files.")
FromOpt = typer.Option(
    None,
    "--from",
    help="Chain from a previous result CSV: reuses the discovered scheme and "
        "skips domains that were unreachable. Omit to probe a plain domain list.",
)


def _resolve_input(
    source: Optional[str], from_csv: Optional[str], column: int
) -> tuple[list[str], dict[str,str], list[ProbeResult]]:
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
    source: Optional[str] = typer.Argument(None, help="CSV of domains, or '-' for stdin."),
    from_csv: Optional[str] = FromOpt,
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
    source: Optional[str] = typer.Argument(None, help="CSV of domains, or '-' for stdin."),
    from_csv: Optional[str] = FromOpt,
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
    api_token: Optional[str] = typer.Option(
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
        raise typer.BadParameter(
            "Provide --api-token or set CLOUDFLARE_API_TOKEN."
        )
    results = cloudflare_subdomains.enumerate_subdomains(api_token)
    _emit(results, out_dir, "cf_subdomains", fmt)


if __name__ == "__main__":
    app()
