"""Shared result types. Commands return these; the CLI renders them."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

#: Column order for tabular output. Explicit so CSV headers are stable.
ROW_FIELDS: tuple[str, ...] = (
    "domain",
    "outcome",
    "status",
    "scheme",
    "final_url",
    "detail",
    "data",
)


class Outcome(str, Enum):
    """Why a probe ended the way it did.

    The primary split is *did we get an HTTP answer at all*. INCONCLUSIVE sits on
    the answered side; ERROR and TIMEOUT on the unanswered side. That distinction
    is operational: a 403 will still be a 403 tomorrow, a timeout may not be.
    """

    VALID = "valid"  # condition met
    NEGATIVE = "negative"  # answered, condition not met
    INCONCLUSIVE = "inconclusive"  # answered, timed out
    TIMEOUT = "timeout"  # no response, timed out
    ERROR = "error"  # no response, DNS/refused/TLS/redirect loop
    SKIPPED = "skipped"  # not probed, upstream marked host as dead


@dataclass
class ProbeResult:
    """One probe against one domain.

    `domain` is always the bare domain, no scheme prefix.
    The scheme that is actually answered is recorded separately in `scheme`"""

    domain: str
    outcome: Outcome
    detail: str = ""
    status: int | None = None  # No HTTP response = None
    scheme: str = ""  # Either http or https depending on response
    final_url: str = ""  # Only set when domain is redirected to another URL
    data: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        """The structured row output capturing probe result data."""
        d = asdict(self)
        d["outcome"] = self.outcome.value
        d["status"] = "" if self.status is None else self.status
        return {k: d[k] for k in ROW_FIELDS}
