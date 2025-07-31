"""Detect WordPress by probing /wp-json for a JSON response."""

from __future__ import annotations

from ..core.http import (
    DEFAULT_TIMEOUT,
    NO_VERDICT_STATUSES,
    classify_failure,
    fetch,
    make_session,
)
from ..core.models import Outcome, ProbeResult


def detect_wordpress(
    domains: list[str],
    timeout: float = DEFAULT_TIMEOUT,
    schemes: dict[str, str] | None = None,
) -> list[ProbeResult]:
    """Checks for WP installation based on the default REST API endpoint presence/absence.
    
    Possible outcomes are VALID, NEGATIVE, and INCONCLUSIVE.
    """
    session = make_session()
    schemes = schemes or {}
    results: list[ProbeResult] = []
    for domain in domains:
        got = fetch(
            session, domain, "/wp-json", timeout=timeout, scheme=schemes.get(domain)
        )
        if got.response is None:
            results.append(classify_failure(domain, got))
            continue

        common = {
            "status": got.status,
            "scheme": got.scheme,
            "final_url": got.final_url,
        }
        if got.status in NO_VERDICT_STATUSES or (isinstance(got.status, int) and got.status >= 500):
            results.append(
                ProbeResult(domain, Outcome.INCONCLUSIVE, detail="blocked", **common)
            )
            continue

        content_type = got.response.headers.get("Content-Type", "")
        if content_type.startswith("application/json"):
            results.append(ProbeResult(domain, Outcome.VALID, **common))
        else:
            results.append(
                ProbeResult(domain, Outcome.NEGATIVE, detail=content_type, **common)
            )
    return results
