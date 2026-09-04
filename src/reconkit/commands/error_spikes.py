"""Collect historical Cloud Run error-spike data for one GCP project.

Standalone data collection: this command does not chain from a domain list.
It answers three questions about a window of history -- when did 5xx rates
spike, how long did each spike last, and what kind of requests were being
made while it happened.

Two deliberate constraints:

*Project is always explicit.* This module never consults the Application
Default Credentials project. ADC resolves to whichever project the operator
last configured in gcloud, which need not be the one under audit, and a wrong
project returns plausible data for the wrong service.

*Samples are centred on the peak.* The interesting requests are the ones
surrounding the worst moment of a spike, not the first N failures in the
window, so sampling finds the peak bucket and walks outwards in both
directions. All status codes are collected: a 200 served slowly, or a burst
of asset requests, is often what explains the 5xx beside it.

*Samples keep the request URL but drop the query string.* The full URL is the
most diagnostic field in a Cloud Run access log and infrastructure, so scheme,
host and path are kept. Query strings are not: they routinely carry tokens,
reset keys, emails and session identifiers. Client IP, user agent and referer
identify people and the pages they were reading, so those are dropped too.
``--include-client-detail`` opts back in to all four.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from ..core.models import Outcome, ProbeResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable

#: Cloud Run request counts. Verified live 2026-09-04: metricKind DELTA,
#: valueType INT64. The aligner must therefore be ALIGN_DELTA -- ALIGN_SUM
#: also returns data, but silently wrong data.
METRIC_TYPE = "run.googleapis.com/request_count"

#: Hard ceiling on log entries read per spike per direction, whatever
#: --samples asks for. entries:list paginates without bound, and the project
#: read quota is 120 requests/minute, so this must stay bounded.
MAX_SAMPLE_SCAN = 400

#: Retries and backoff for the entries.list read quota (120/min/project).
QUOTA_RETRIES = 3
QUOTA_BACKOFF_SECONDS = 20

#: How far either side of the peak bucket to look for surrounding requests.
#: The peak bucket itself is included, so the searched span is
#: bucket_width + 2 * SAMPLE_SPAN_BUCKETS * bucket_width.
SAMPLE_SPAN_BUCKETS = 2

INSTALL_HINT = (
    "error-spikes needs the GCP client libraries. "
    "Install them with: pip install 'reconkit[gcp]'"
)

#: Fields dropped from request samples unless the caller opts in. The query
#: string is in here because it carries tokens and personal identifiers; the
#: rest of the URL is not sensitive and is always kept.
SENSITIVE_FIELDS = ("remote_ip", "user_agent", "referer", "query")


class MetricClient(Protocol):
    """The slice of MetricServiceClient this command uses."""

    def list_time_series(self, request: dict[str, Any]) -> Iterable[Any]: ...


class LogClient(Protocol):
    """The slice of LoggingServiceV2Client this command uses."""

    def list_log_entries(self, request: dict[str, Any]) -> Iterable[Any]: ...


@dataclass(frozen=True)
class Window:
    """The closed time interval a scan covers."""

    start: dt.datetime
    end: dt.datetime

    @classmethod
    def ending_now(cls, hours: int, now: dt.datetime | None = None) -> Window:
        end = now or dt.datetime.now(dt.UTC)
        return cls(end - dt.timedelta(hours=hours), end)


@dataclass
class Spike:
    """One run of consecutive buckets that all breached the threshold.

    Duration is measured from the start of the first breaching bucket to the
    end of the last, so a single-bucket spike lasts one bucket width rather
    than zero.
    """

    service: str
    started: dt.datetime
    ended: dt.datetime
    peak: int
    total: int
    buckets: int
    #: End of the worst bucket. Sampling is centred here, not on `started`.
    peak_at: dt.datetime | None = None
    samples: list[dict[str, Any]] = field(default_factory=list)

    @property
    def duration_minutes(self) -> float:
        return (self.ended - self.started).total_seconds() / 60


def _load_metric_client() -> MetricClient:
    try:
        from google.cloud import monitoring_v3
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise RuntimeError(INSTALL_HINT) from exc
    return monitoring_v3.MetricServiceClient()


def _load_log_client() -> LogClient:
    try:
        from google.cloud.logging_v2.services.logging_service_v2 import (
            LoggingServiceV2Client,
        )
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise RuntimeError(INSTALL_HINT) from exc
    return LoggingServiceV2Client()


def _require_project(project: str) -> None:
    if not project:
        raise ValueError("project is required; error-spikes never infers it")


def build_metric_request(
    project: str, window: Window, alignment_seconds: int
) -> dict[str, Any]:
    """Return the listTimeSeries request body. `project` is used verbatim."""
    _require_project(project)
    return {
        "name": f"projects/{project}",
        "filter": (
            f'metric.type = "{METRIC_TYPE}" '
            'AND resource.type = "cloud_run_revision" '
            'AND metric.labels.response_code_class = "5xx"'
        ),
        "interval": {"start_time": window.start, "end_time": window.end},
        "aggregation": {
            "alignment_period": {"seconds": alignment_seconds},
            "per_series_aligner": "ALIGN_DELTA",
            "cross_series_reducer": "REDUCE_SUM",
            "group_by_fields": ["resource.labels.service_name"],
        },
        "view": "FULL",
    }


def build_log_filter(service: str, start: dt.datetime, end: dt.datetime) -> str:
    """Return an entries:list filter for one service over one interval.

    Deliberately does not filter on status. The requests surrounding a spike
    explain it, and most of those succeeded: a flood of asset requests or a
    slow 200 is context that a 5xx-only filter throws away.
    """
    return (
        'resource.type="cloud_run_revision" '
        f'AND resource.labels.service_name="{service}" '
        "AND httpRequest.requestMethod!=\"\" "
        f'AND timestamp>="{start.isoformat()}" '
        f'AND timestamp<="{end.isoformat()}"'
    )


def _service_of(series: Any) -> str:
    labels = getattr(getattr(series, "resource", None), "labels", None) or {}
    return dict(labels).get("service_name", "unknown")


def _as_datetime(stamp: Any) -> dt.datetime:
    if isinstance(stamp, (int, float)):
        return dt.datetime.fromtimestamp(float(stamp), dt.UTC)
    return stamp


def _points(series: Any) -> list[tuple[dt.datetime, int]]:
    out: list[tuple[dt.datetime, int]] = []
    for p in getattr(series, "points", []) or []:
        value = int(getattr(p.value, "int64_value", 0) or 0)
        out.append((_as_datetime(p.interval.end_time), value))
    return sorted(out, key=lambda kv: kv[0])


def group_spikes(
    service: str,
    points: list[tuple[dt.datetime, int]],
    threshold: int,
    alignment_seconds: int,
) -> list[Spike]:
    """Collapse consecutive breaching buckets into single spikes.

    A gap of one non-breaching bucket ends the spike. Buckets arrive already
    sorted by time.
    """
    width = dt.timedelta(seconds=alignment_seconds)
    spikes: list[Spike] = []
    run: list[tuple[dt.datetime, int]] = []

    def flush() -> None:
        if not run:
            return
        counts = [c for _, c in run]
        peak_stamp, peak_count = max(run, key=lambda kv: kv[1])
        spikes.append(
            Spike(
                service=service,
                started=run[0][0] - width,
                ended=run[-1][0],
                peak=peak_count,
                total=sum(counts),
                buckets=len(run),
                peak_at=peak_stamp,
            )
        )
        run.clear()

    for stamp, count in points:
        if count < threshold:
            flush()
            continue
        if run and stamp - run[-1][0] > width:
            flush()
        run.append((stamp, count))
    flush()
    return spikes


def redact_sample(entry: Any, *, include_client_detail: bool) -> dict[str, Any]:
    """Turn one log entry into a sample record.

    The request URL is kept, minus its query string: the URL is the single
    most diagnostic field in an access log, while the query string is where
    tokens, reset keys and email addresses live. Client IP, user agent and
    referer are dropped unless the caller opts in.
    """
    hr = getattr(entry, "http_request", None)
    url = getattr(hr, "request_url", "") or ""
    parts = urlsplit(url)
    clean_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    sample: dict[str, Any] = {
        "timestamp": _timestamp_of(entry),
        "method": getattr(hr, "request_method", "") or "",
        "url": clean_url,
        "path": parts.path or "",
        "status": int(getattr(hr, "status", 0) or 0),
        "latency": _latency_seconds(hr),
        "protocol": getattr(hr, "protocol", "") or "",
        "request_size": _int_or_none(getattr(hr, "request_size", None)),
        "response_size": _int_or_none(getattr(hr, "response_size", None)),
        "server_ip": getattr(hr, "server_ip", "") or "",
    }
    if include_client_detail:
        sample.update(
            {
                "query": parts.query or "",
                "remote_ip": getattr(hr, "remote_ip", "") or "",
                "user_agent": getattr(hr, "user_agent", "") or "",
                "referer": getattr(hr, "referer", "") or "",
            }
        )
    return sample


def _timestamp_of(entry: Any) -> str:
    stamp = _as_datetime(getattr(entry, "timestamp", None))
    return stamp.isoformat() if isinstance(stamp, dt.datetime) else ""


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _latency_seconds(hr: Any) -> float | None:
    lat = getattr(hr, "latency", None)
    if lat is None:
        return None
    if isinstance(lat, (int, float)):
        return float(lat)
    seconds = getattr(lat, "seconds", None)
    if seconds is None:
        return None
    nanos = getattr(lat, "nanos", 0) or 0
    return float(seconds) + float(nanos) / 1e9


def _is_quota_error(exc: BaseException) -> bool:
    """True when an exception looks like the entries.list read-quota limit."""
    text = str(exc).lower()
    return "quota" in text or "resource_exhausted" in text or "429" in text


def _fetch(
    api: LogClient,
    project: str,
    flt: str,
    limit: int,
    order_by: str,
    *,
    sleep: Any = None,
) -> list[Any]:
    """Read at most `limit` entries in one direction, hard-capped.

    Cloud Logging allows 120 entries.list reads per minute per project, and a
    multi-spike scan paginates well past that, so a quota rejection is
    expected rather than exceptional. Back off and retry a few times; give up
    with an actionable message rather than a raw protobuf dump.
    """
    capped = min(limit, MAX_SAMPLE_SCAN)
    if capped <= 0:
        return []
    request = {
        "resource_names": [f"projects/{project}"],
        "filter": flt,
        "order_by": order_by,
        "page_size": min(capped, 100),
    }
    pause = sleep or time.sleep
    last: BaseException | None = None
    for attempt in range(QUOTA_RETRIES):
        try:
            out: list[Any] = []
            for entry in api.list_log_entries(request=request):
                out.append(entry)
                if len(out) >= capped:
                    break
            return out
        except Exception as exc:  # noqa: BLE001 - re-raised below
            if not _is_quota_error(exc):
                raise
            last = exc
            if attempt < QUOTA_RETRIES - 1:
                pause(QUOTA_BACKOFF_SECONDS * (attempt + 1))
    raise RuntimeError(
        "Cloud Logging read quota exhausted (120 reads/minute per project). "
        "Lower --samples, narrow --hours, or raise --threshold so fewer "
        "spikes are sampled."
    ) from last


def sample_window(
    spike: Spike, alignment_seconds: int, span_buckets: int = SAMPLE_SPAN_BUCKETS
) -> tuple[dt.datetime, dt.datetime, dt.datetime]:
    """Return (start, centre, end) of the interval to sample around the peak.

    Centred on the peak bucket rather than the spike start, so a long spike
    with a sharp peak samples the moment that matters. Clamped to the spike's
    own extent only on the near side: context just before and after the spike
    is often what explains it, so the window may overhang both ends.
    """
    width = dt.timedelta(seconds=alignment_seconds)
    centre = spike.peak_at or spike.ended
    return centre - width * (span_buckets + 1), centre, centre + width * span_buckets


def collect_samples(
    project: str,
    spike: Spike,
    *,
    limit: int,
    alignment_seconds: int = 300,
    include_client_detail: bool = False,
    client: LogClient | None = None,
    sleep: Any = None,
) -> list[dict[str, Any]]:
    """Return up to `limit` samples straddling the peak of one spike.

    Half the budget is spent walking backwards from the peak and half
    forwards, so the result shows the run-up and the aftermath rather than
    just whichever entries the API returned first. All status codes are
    included.
    """
    _require_project(project)
    if limit <= 0:
        return []
    api = client or _load_log_client()

    start, centre, end = sample_window(spike, alignment_seconds)
    before_budget = limit // 2
    after_budget = limit - before_budget

    before = _fetch(
        api,
        project,
        build_log_filter(spike.service, start, centre),
        before_budget,
        "timestamp desc",
        sleep=sleep,
    )
    after = _fetch(
        api,
        project,
        build_log_filter(spike.service, centre, end),
        after_budget,
        "timestamp asc",
        sleep=sleep,
    )

    entries = list(reversed(before)) + list(after)
    samples = [
        redact_sample(e, include_client_detail=include_client_detail)
        for e in entries
    ]
    samples.sort(key=lambda s: s["timestamp"])
    return samples


def _to_result(spike: Spike, project: str, threshold: int) -> ProbeResult:
    data: dict[str, Any] = {
        "project": project,
        "service": spike.service,
        "started": spike.started.isoformat(),
        "ended": spike.ended.isoformat(),
        "duration_minutes": round(spike.duration_minutes, 1),
        "buckets": spike.buckets,
        "peak_errors": spike.peak,
        "peak_at": spike.peak_at.isoformat() if spike.peak_at else "",
        "total_errors": spike.total,
        "threshold": threshold,
    }
    if spike.samples:
        data["sample_count"] = len(spike.samples)
        data["samples"] = spike.samples
    return ProbeResult(
        spike.service,
        Outcome.VALID,
        detail=(
            f"{spike.total} 5xx over {spike.duration_minutes:.0f}m "
            f"(peak {spike.peak})"
        ),
        data=data,
    )


def find_spikes(
    project: str,
    *,
    hours: int = 24,
    threshold: int = 10,
    alignment_seconds: int = 300,
    samples: int = 0,
    include_client_detail: bool = False,
    client: MetricClient | None = None,
    log_client: LogClient | None = None,
    now: dt.datetime | None = None,
) -> list[ProbeResult]:
    """Return one ProbeResult per spike found in the window.

    `project` is mandatory and never defaulted. A project with no breaching
    bucket yields a single NEGATIVE row, so a clean scan stays distinguishable
    from a scan that never ran. Request sampling is off unless `samples` > 0.
    """
    _require_project(project)

    api = client or _load_metric_client()
    window = Window.ending_now(hours, now=now)
    request = build_metric_request(project, window, alignment_seconds)

    spikes: list[Spike] = []
    for series in api.list_time_series(request=request):
        spikes += group_spikes(
            _service_of(series), _points(series), threshold, alignment_seconds
        )
    spikes.sort(key=lambda s: (s.started, s.service))

    if samples > 0:
        for spike in spikes:
            spike.samples = collect_samples(
                project,
                spike,
                limit=samples,
                alignment_seconds=alignment_seconds,
                include_client_detail=include_client_detail,
                client=log_client,
            )

    if not spikes:
        return [
            ProbeResult(
                project,
                Outcome.NEGATIVE,
                detail=f"no bucket reached {threshold} 5xx in {hours}h",
                data={"project": project, "threshold": threshold, "hours": hours},
            )
        ]
    return [_to_result(s, project, threshold) for s in spikes]
