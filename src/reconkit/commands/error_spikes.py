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

*Request samples are redacted by default.* Cloud Run access logs carry
``remote_ip``, ``user_agent``, ``referer`` and a full ``request_url``
including its query string. Writing those to a results file turns an audit
artifact into a file of personal and client data, so the sample record keeps
method, redacted path, status and latency, and drops the rest unless the
caller explicitly opts in.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

from ..core.models import Outcome, ProbeResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Iterator

#: Cloud Run request counts. Verified live 2026-09-04: metricKind DELTA,
#: valueType INT64. The aligner must therefore be ALIGN_DELTA -- ALIGN_SUM
#: also returns data, but silently wrong data.
METRIC_TYPE = "run.googleapis.com/request_count"

#: Hard ceiling on log entries read per spike, whatever --samples asks for.
#: entries:list over a busy window is slow and paginates without bound.
MAX_SAMPLE_SCAN = 500

INSTALL_HINT = (
    "error-spikes needs the GCP client libraries. "
    "Install them with: pip install 'reconkit[gcp]'"
)

#: Fields dropped from request samples unless the caller opts in.
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
    """Return an entries:list filter for 5xx requests to one service."""
    return (
        'resource.type="cloud_run_revision" '
        f'AND resource.labels.service_name="{service}" '
        "AND httpRequest.status>=500 "
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
        spikes.append(
            Spike(
                service=service,
                started=run[0][0] - width,
                ended=run[-1][0],
                peak=max(counts),
                total=sum(counts),
                buckets=len(run),
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

    By default the query string, client IP, user agent and referer are
    dropped: they identify people and client sites, and an audit artifact
    should not become a file of personal data.
    """
    hr = getattr(entry, "http_request", None)
    url = getattr(hr, "request_url", "") or ""
    parts = urlsplit(url)
    sample: dict[str, Any] = {
        "method": getattr(hr, "request_method", "") or "",
        "path": parts.path or "",
        "status": int(getattr(hr, "status", 0) or 0),
        "latency": _latency_seconds(hr),
        "timestamp": _as_datetime(getattr(entry, "timestamp", None)),
    }
    if isinstance(sample["timestamp"], dt.datetime):
        sample["timestamp"] = sample["timestamp"].isoformat()
    else:
        sample["timestamp"] = ""
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


def _iter_entries(
    api: LogClient, project: str, flt: str, limit: int
) -> Iterator[Any]:
    request = {
        "resource_names": [f"projects/{project}"],
        "filter": flt,
        "page_size": min(limit, 100),
    }
    for i, entry in enumerate(api.list_log_entries(request=request)):
        if i >= min(limit, MAX_SAMPLE_SCAN):
            return
        yield entry


def collect_samples(
    project: str,
    spike: Spike,
    *,
    limit: int,
    include_client_detail: bool = False,
    client: LogClient | None = None,
) -> list[dict[str, Any]]:
    """Return up to `limit` redacted request samples from one spike window."""
    _require_project(project)
    if limit <= 0:
        return []
    api = client or _load_log_client()
    flt = build_log_filter(spike.service, spike.started, spike.ended)
    return [
        redact_sample(e, include_client_detail=include_client_detail)
        for e in _iter_entries(api, project, flt, limit)
    ]


def _to_result(spike: Spike, project: str, threshold: int) -> ProbeResult:
    data: dict[str, Any] = {
        "project": project,
        "service": spike.service,
        "started": spike.started.isoformat(),
        "ended": spike.ended.isoformat(),
        "duration_minutes": round(spike.duration_minutes, 1),
        "buckets": spike.buckets,
        "peak_errors": spike.peak,
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
