"""Tests for error-spikes.

Every test uses a stub client. Nothing here touches GCP: the point of the
MetricClient Protocol is that this suite runs without credentials, without the
optional gcp extra installed, and without any live traffic data.
"""

from __future__ import annotations

import datetime as dt

import pytest
from typer.testing import CliRunner

from reconkit.cli import app
from reconkit.commands import error_spikes
from reconkit.core.models import Outcome

NOW = dt.datetime(2026, 9, 4, 12, 0, tzinfo=dt.UTC)


class _Value:
    def __init__(self, n: int) -> None:
        self.int64_value = n


class _Interval:
    def __init__(self, end: dt.datetime) -> None:
        self.end_time = end


class _Point:
    def __init__(self, end: dt.datetime, n: int) -> None:
        self.value = _Value(n)
        self.interval = _Interval(end)


class _Resource:
    def __init__(self, service: str) -> None:
        self.labels = {"service_name": service}


class _Series:
    def __init__(self, service: str, points: list[tuple[dt.datetime, int]]) -> None:
        self.resource = _Resource(service)
        self.points = [_Point(e, n) for e, n in points]


class StubClient:
    """Records the request it was given and replays canned series."""

    def __init__(self, series: list[_Series]) -> None:
        self._series = series
        self.request: dict | None = None

    def list_time_series(self, request):  # noqa: ANN001, ANN201
        self.request = request
        return list(self._series)


def test_project_is_used_verbatim_in_the_request() -> None:
    stub = StubClient([])
    error_spikes.find_spikes("my-proj", client=stub, now=NOW)
    assert stub.request["name"] == "projects/my-proj"


def test_empty_project_is_rejected() -> None:
    with pytest.raises(ValueError, match="never infers it"):
        error_spikes.find_spikes("", client=StubClient([]))


def test_aligner_is_align_delta_not_align_sum() -> None:
    """request_count is a DELTA metric; ALIGN_SUM returns plausible wrong data."""
    stub = StubClient([])
    error_spikes.find_spikes("p", client=stub, now=NOW)
    agg = stub.request["aggregation"]
    assert agg["per_series_aligner"] == "ALIGN_DELTA"


def test_filter_scopes_to_cloud_run_5xx() -> None:
    stub = StubClient([])
    error_spikes.find_spikes("p", client=stub, now=NOW)
    f = stub.request["filter"]
    assert error_spikes.METRIC_TYPE in f
    assert 'resource.type = "cloud_run_revision"' in f
    assert '"5xx"' in f


def test_window_ends_now_and_spans_requested_hours() -> None:
    stub = StubClient([])
    error_spikes.find_spikes("p", hours=6, client=stub, now=NOW)
    iv = stub.request["interval"]
    assert iv["end_time"] == NOW
    assert iv["start_time"] == NOW - dt.timedelta(hours=6)


def test_bucket_at_threshold_is_a_spike() -> None:
    stub = StubClient([_Series("api", [(NOW, 10)])])
    results = error_spikes.find_spikes("p", threshold=10, client=stub, now=NOW)
    assert [r.outcome for r in results] == [Outcome.VALID]
    assert results[0].domain == "api"
    assert results[0].data["total_errors"] == 10


def test_bucket_below_threshold_is_not_a_spike() -> None:
    stub = StubClient([_Series("api", [(NOW, 9)])])
    results = error_spikes.find_spikes("p", threshold=10, client=stub, now=NOW)
    assert [r.outcome for r in results] == [Outcome.NEGATIVE]


def test_clean_project_reports_negative_not_empty() -> None:
    """A clean scan must be distinguishable from a scan that never ran."""
    results = error_spikes.find_spikes("p", client=StubClient([]), now=NOW)
    assert len(results) == 1
    assert results[0].outcome is Outcome.NEGATIVE
    assert results[0].data["project"] == "p"


def test_multiple_services_each_report_their_own_spikes() -> None:
    stub = StubClient(
        [
            _Series("api", [(NOW, 50), (NOW - dt.timedelta(minutes=5), 1)]),
            _Series("web", [(NOW, 20)]),
        ]
    )
    results = error_spikes.find_spikes("p", threshold=10, client=stub, now=NOW)
    assert sorted(r.domain for r in results) == ["api", "web"]


def test_spikes_are_ordered_by_start_time() -> None:
    later, earlier = NOW, NOW - dt.timedelta(minutes=30)
    stub = StubClient([_Series("api", [(later, 11), (earlier, 12)])])
    results = error_spikes.find_spikes("p", threshold=10, client=stub, now=NOW)
    stamps = [r.data["started"] for r in results]
    assert stamps == sorted(stamps)


def test_missing_service_label_does_not_crash() -> None:
    series = _Series("api", [(NOW, 11)])
    series.resource.labels = {}
    results = error_spikes.find_spikes(
        "p", threshold=10, client=StubClient([series]), now=NOW
    )
    assert results[0].domain == "unknown"


def test_cli_requires_a_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    res = CliRunner().invoke(app, ["error-spikes"])
    assert res.exit_code != 0
    assert "does not fall back" in res.output


def test_cli_never_uses_the_adc_default_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """google.auth.default() must not be consulted for the project."""
    called = []

    def _boom() -> None:
        called.append(1)
        raise AssertionError("ADC default project must never be consulted")

    monkeypatch.setattr(
        error_spikes, "_load_metric_client", lambda: StubClient([]), raising=True
    )
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    res = CliRunner().invoke(
        app,
        ["error-spikes", "--project", "explicit-proj", "--out-dir", str(tmp_path)],
    )
    assert res.exit_code == 0, res.output
    assert not called


def test_cli_reads_project_from_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "env-proj")
    seen: dict[str, str] = {}

    def _fake(project: str, **kw):  # noqa: ANN003, ANN202
        seen["project"] = project
        return []

    monkeypatch.setattr(error_spikes, "find_spikes", _fake, raising=True)
    res = CliRunner().invoke(app, ["error-spikes", "--out-dir", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert seen["project"] == "env-proj"


def test_missing_extra_gives_an_actionable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise() -> None:
        raise RuntimeError(error_spikes.INSTALL_HINT)

    monkeypatch.setattr(error_spikes, "_load_metric_client", _raise, raising=True)
    res = CliRunner().invoke(app, ["error-spikes", "--project", "p"])
    assert res.exit_code != 0
    assert "reconkit[gcp]" in res.output


# ----------------------------------------------------------- spike grouping


def _mins(n: int) -> dt.datetime:
    return NOW + dt.timedelta(minutes=n)


def test_consecutive_buckets_collapse_into_one_spike() -> None:
    points = [(_mins(0), 20), (_mins(5), 30), (_mins(10), 25)]
    spikes = error_spikes.group_spikes("api", points, 10, 300)
    assert len(spikes) == 1
    assert spikes[0].buckets == 3
    assert spikes[0].peak == 30
    assert spikes[0].total == 75


def test_a_gap_splits_two_spikes() -> None:
    points = [(_mins(0), 20), (_mins(5), 1), (_mins(10), 40)]
    spikes = error_spikes.group_spikes("api", points, 10, 300)
    assert len(spikes) == 2
    assert [s.peak for s in spikes] == [20, 40]


def test_single_bucket_spike_lasts_one_bucket_width() -> None:
    """Duration must not be zero for a one-bucket spike."""
    spikes = error_spikes.group_spikes("api", [(_mins(0), 20)], 10, 300)
    assert spikes[0].duration_minutes == 5.0


def test_duration_spans_first_bucket_start_to_last_bucket_end() -> None:
    points = [(_mins(0), 20), (_mins(5), 20)]
    spikes = error_spikes.group_spikes("api", points, 10, 300)
    assert spikes[0].duration_minutes == 10.0


def test_result_carries_timing_and_duration() -> None:
    stub = StubClient([_Series("api", [(_mins(0), 20), (_mins(5), 20)])])
    r = error_spikes.find_spikes("p", threshold=10, client=stub, now=NOW)[0]
    assert r.data["duration_minutes"] == 10.0
    assert r.data["buckets"] == 2
    assert r.data["peak_errors"] == 20
    assert r.data["started"] < r.data["ended"]


# --------------------------------------------------------- request sampling


class _HttpRequest:
    def __init__(self) -> None:
        self.request_method = "GET"
        self.request_url = "https://x.example/checkout?token=SECRET&email=a@b.c"
        self.status = 503
        self.latency = _Latency(1, 500_000_000)
        self.remote_ip = "203.0.113.7"
        self.user_agent = "Mozilla/5.0 (secret device)"
        self.referer = "https://client-site.example/basket"


class _Latency:
    def __init__(self, seconds: int, nanos: int) -> None:
        self.seconds = seconds
        self.nanos = nanos


class _Entry:
    def __init__(self) -> None:
        self.http_request = _HttpRequest()
        self.timestamp = NOW


class StubLogClient:
    def __init__(self, count: int) -> None:
        self._count = count
        self.request: dict | None = None

    def list_log_entries(self, request):  # noqa: ANN001, ANN201
        self.request = request
        return [_Entry() for _ in range(self._count)]


def test_samples_are_redacted_by_default() -> None:
    s = error_spikes.redact_sample(_Entry(), include_client_detail=False)
    for banned in error_spikes.SENSITIVE_FIELDS:
        assert banned not in s
    assert s["path"] == "/checkout"
    assert "SECRET" not in str(s)
    assert "203.0.113.7" not in str(s)


def test_opt_in_includes_client_detail() -> None:
    s = error_spikes.redact_sample(_Entry(), include_client_detail=True)
    assert s["remote_ip"] == "203.0.113.7"
    assert s["query"] == "token=SECRET&email=a@b.c"


def test_sample_keeps_useful_non_identifying_fields() -> None:
    s = error_spikes.redact_sample(_Entry(), include_client_detail=False)
    assert s["method"] == "GET"
    assert s["status"] == 503
    assert s["latency"] == 1.5


def test_no_logs_are_read_when_samples_is_zero() -> None:
    logs = StubLogClient(5)
    stub = StubClient([_Series("api", [(_mins(0), 20)])])
    error_spikes.find_spikes(
        "p", threshold=10, samples=0, client=stub, log_client=logs, now=NOW
    )
    assert logs.request is None


def test_samples_are_attached_when_requested() -> None:
    logs = StubLogClient(3)
    stub = StubClient([_Series("api", [(_mins(0), 20)])])
    r = error_spikes.find_spikes(
        "p", threshold=10, samples=3, client=stub, log_client=logs, now=NOW
    )[0]
    assert r.data["sample_count"] == 3
    assert len(r.data["samples"]) == 3


def test_sample_limit_is_respected() -> None:
    logs = StubLogClient(50)
    stub = StubClient([_Series("api", [(_mins(0), 20)])])
    r = error_spikes.find_spikes(
        "p", threshold=10, samples=2, client=stub, log_client=logs, now=NOW
    )[0]
    assert len(r.data["samples"]) == 2


def test_sample_scan_is_hard_capped() -> None:
    logs = StubLogClient(5000)
    spike = error_spikes.Spike("api", NOW, NOW, 1, 1, 1)
    got = error_spikes.collect_samples(
        "p", spike, limit=10_000, client=logs
    )
    assert len(got) == error_spikes.MAX_SAMPLE_SCAN


def test_log_filter_scopes_to_service_and_window() -> None:
    f = error_spikes.build_log_filter("api", NOW, NOW + dt.timedelta(minutes=5))
    assert 'resource.labels.service_name="api"' in f
    assert "httpRequest.status>=500" in f
    assert "timestamp>=" in f and "timestamp<=" in f


def test_cli_rejects_client_detail_without_samples(tmp_path) -> None:
    res = CliRunner().invoke(
        app,
        ["error-spikes", "-p", "x", "--include-client-detail",
         "--out-dir", str(tmp_path)],
    )
    assert res.exit_code != 0
    assert "only applies with --samples" in res.output
