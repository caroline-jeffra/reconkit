"""Check whether each domain responds over HTTP/HTTPS."""

from __future__ import annotations

from ..core.http import DEFAULT_TIMEOUT, classify_failure, fetch, make_session
from ..core.models import Outcome, ProbeResult


def check_live(
    domains: list[str], timeout: float = DEFAULT_TIMEOUT
) -> list[ProbeResult]:
    """Reachability triage: any HTTP answer means the site is working.

    This command asks whether a site is serving here.
    A 403 or 503 host is responding, so it is VALID.
    This probe never returns INCONCLUSIVE.
    """
    session = make_session()
    results: list[ProbeResult] = []
    for domain in domains:
        got = fetch(session, domain, timeout=timeout)
        if not got.answered:
            results.append(classify_failure(domain, got))
            continue
        results.append(
            ProbeResult(
                domain,
                Outcome.VALID,
                detail="" if got.scheme == "https" else "no tls",
                status=got.status,
                scheme=got.scheme,
                final_url=got.final_url,
            )
        )
    return results
