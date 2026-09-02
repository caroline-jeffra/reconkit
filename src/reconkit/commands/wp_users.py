"""Check whether a WordPress site exposes user data via the REST API."""

from __future__ import annotations

from ..core.http import (
    DEFAULT_TIMEOUT,
    NO_VERDICT_STATUSES,
    classify_failure,
    fetch,
    make_session,
)
from ..core.models import Outcome, ProbeResult


def check_wp_users(
    domains: list[str],
    timeout: float = DEFAULT_TIMEOUT,
    schemes: dict[str, str] | None = None,
) -> list[ProbeResult]:
    """Does /wp-json/wp/v2/users enumerate authors to an anonymous caller?

    Possible outcomes are VALID, NEGATIVE, and INCONCLUSIVE.
    """
    session = make_session()
    schemes = schemes or {}
    results: list[ProbeResult] = []
    for domain in domains:
        got = fetch(
            session,
            domain,
            "/wp-json/wp/v2/users",
            timeout=timeout,
            scheme=schemes.get(domain),
        )
        if got.response is None:
            results.append(classify_failure(domain, got))
            continue

        common = {
            "status": got.status,
            "scheme": got.scheme,
            "final_url": got.final_url,
        }
        if got.status in NO_VERDICT_STATUSES or (
            isinstance(got.status, int) and got.status >= 500
        ):
            results.append(
                ProbeResult(domain, Outcome.INCONCLUSIVE, detail="blocked", **common)
            )
            continue

        try:
            payload = got.response.json()
        except ValueError as exc:
            # Body was not JSON
            results.append(
                ProbeResult(
                    domain, Outcome.NEGATIVE, detail=type(exc).__name__, **common
                )
            )
            continue

        if isinstance(payload, list) and payload and "name" in payload[0]:
            names = [u.get("name", "") for u in payload if isinstance(u, dict)]
            results.append(
                ProbeResult(
                    domain,
                    Outcome.VALID,
                    detail=names[0],
                    data={"names": names},
                    **common,
                )
            )
        elif isinstance(payload, dict):
            # A JSON error object; the application itself declined.
            results.append(
                ProbeResult(
                    domain,
                    Outcome.NEGATIVE,
                    detail=str(payload.get("code", "")),
                    **common,
                )
            )
        else:
            results.append(
                ProbeResult(domain, Outcome.NEGATIVE, detail="no name field", **common)
            )
    return results
