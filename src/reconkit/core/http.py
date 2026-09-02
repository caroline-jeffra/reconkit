"""A single configured requests session shared by all commands."""

from __future__ import annotations

from dataclasses import dataclass

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..core.models import Outcome, ProbeResult

DEFAULT_TIMEOUT = 5.0

#: Statuses worth retrying: rate limiting and transient server-side faults.
RETRY_STATUSES = (429, 500, 502, 503, 504)

#: Tried in order. A domain is reachable if either scheme answers.
SCHEMES = ("https", "http")

#: Answered, but the answer does not provide a clear verdict.
NO_VERDICT_STATUSES = frozenset({401, 403, 429})


def make_session(retries: int = 2, backoff: float = 0.3) -> requests.Session:
    """A session for running command probes."""
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=RETRY_STATUSES,
        allowed_methods=frozenset({"GET"}),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


@dataclass
class Fetch:
    """The transport-level result of one probe, before any command-specific verdict.

    Exactly one of `response` / `error` is set. `scheme` records which scheme
    actually answered so callers can report it without re-deriving it.
    """

    scheme: str = ""
    response: requests.Response | None = None
    error: Exception | None = None

    @property
    def answered(self) -> bool:
        """Check whether or not there was a response."""
        return self.response is not None

    @property
    def status(self) -> int | None:
        """Identify the status code of the response, if one is returned."""
        return self.response.status_code if self.response is not None else None

    @property
    def final_url(self) -> str:
        """The landing URL for probes which were moved by redirects."""
        if self.response is None:
            return ""
        return self.response.url if self.response.history else ""


def fetch(
    session: requests.Session,
    domain: str,
    path: str = "",
    timeout: float = DEFAULT_TIMEOUT,
    scheme: str | None = None,
) -> Fetch:
    """GET `domain`/`path`, trying HTTPS then HTTP.

    `scheme` pins a single scheme, used when a previous `live-check` already
    discovered which one answers. A pinned scheme that fails still falls back
    as a cached scheme can go stale between runs.
    """
    order = (scheme, *SCHEMES) if scheme else SCHEMES
    tried: list[str] = []
    first_error: Exception | None = None

    for candidate in order:
        if candidate in tried:
            continue
        tried.append(candidate)
        try:
            resp = session.get(f"{candidate}://{domain}{path}", timeout=timeout)
        except requests.RequestException as exc:
            # Remember why HTTPS failed; if HTTP also fails we report the first cause.
            if first_error is None:
                first_error = exc
            continue
        return Fetch(scheme=candidate, response=resp)

    return Fetch(scheme="", error=first_error)


def classify_failure(domain: str, fetch_result: Fetch) -> ProbeResult:
    """Turn a no-response Fetch into the right unanswered Outcome."""
    exc = fetch_result.error
    if isinstance(exc, requests.exceptions.Timeout):
        return ProbeResult(domain, Outcome.TIMEOUT, detail=type(exc).__name__)
    if isinstance(exc, requests.exceptions.RetryError):
        # The server did answer repeatedly, but not usefully.
        return ProbeResult(
            domain,
            Outcome.INCONCLUSIVE,
            detail=f"retries exhausted on {'/'.join(str(s) for s in RETRY_STATUSES)}",
        )
    return ProbeResult(
        domain, Outcome.ERROR, detail=type(exc).__name__ if exc else "no response"
    )
