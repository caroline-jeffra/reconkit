"""Input/output helpers: read a domain list, write results as CSV or JSON."""

from __future__ import annotations

import csv
import json
import sys
from _collections_abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .models import ROW_FIELDS, Outcome, ProbeResult

#: Upstream outcomes that signify it is not worth re-probing
DEAD_OUTCOMES = frozenset({Outcome.ERROR.value, Outcome.TIMEOUT.value})


@dataclass
class Upstream:
    """A previous command's results used to skip work on a chained run.
    
    `schemes` are the answering scheme from last run. `dead` domains are not
    worth probing. Both are optional.
    """

    domains:list[str]
    schemes: dict[str, str]
    dead: dict[str, str]

    def skipped_results(self) -> list[ProbeResult]:
        """Rows for the domains deliberately not probed.
        
        Emitted for chained run row count consistency.
        """
        return [
            ProbeResult(domain, Outcome.SKIPPED, detail=reason)
            for domain, reason in self.dead.items()
        ]

def read_upstream(source: str | Path, stage: str = "live-check") -> Upstream:
    """Read a previous command's result CSV."""
    live: list[str] = []
    schemes: dict[str, str] = {}
    dead: dict[str, str] = {}

    with open(source, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            domain = (row.get("domain") or "").strip()
            if not domain:
                continue
            outcome = (row.get("outcome") or "").strip()
            if outcome in DEAD_OUTCOMES:
                dead[domain] = f"skipped: {outcome} in {stage}"
                continue
            live.append(domain)
            if scheme := (row.get("scheme") or "").strip():
                schemes[domain] = scheme

    return Upstream(domains=live, schemes=schemes, dead=dead)

def read_domains(source: str | Path, column: int = 0) -> list[str]:
    """Read domains from a CSV file or '-' for stdin. One domain per row, `column`-th field."""
    if str(source) == "-":
        return _parse_rows(sys.stdin, column)
    with open(source, encoding="utf-8", newline="") as fh:
        return _parse_rows(fh, column)


def _parse_rows(fh: Iterable[str], column: int) -> list[str]:
    domains: list[str] = []
    for row in csv.reader(fh):
        if row and len(row) > column and row[column].strip():
            domains.append(row[column].strip())
    return domains


def write_results(
    results: Sequence[ProbeResult],
    out_dir: str | Path,
    stem: str,
    fmt: str = "csv",
) -> Path:
    """Write results to out_dir/<stem>.<fmt>. Returns the path written."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{stem}.{fmt}"

    rows = [r.to_row() for r in results]
    if fmt == "json":
        path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    elif fmt == "csv":
        with open(path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=ROW_FIELDS)
            writer.writeheader()
            for row in rows:
                row = {**row, "data": json.dumps(row["data"]) if row["data"] else ""}
                writer.writerow(row)
    else:
        raise ValueError(f"unknown format: {fmt!r}")
    return path
