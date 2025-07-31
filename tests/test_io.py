"""Tests for reading domain lists / upstream results and writing output."""

import csv
import io
import json

import pytest

from reconkit.core.io import (
    DEAD_OUTCOMES,
    Upstream,
    read_domains,
    read_upstream,
    write_results,
)
from reconkit.core.models import ROW_FIELDS, Outcome, ProbeResult


# --- read_domains ----------------------------------------------------------

def test_read_domains_one_per_row(tmp_path):
    src = tmp_path / "in.csv"
    src.write_text("a.example\nb.example\n")
    assert read_domains(src) == ["a.example", "b.example"]


def test_read_domains_selects_column_and_strips(tmp_path):
    src = tmp_path / "in.csv"
    src.write_text("skip, a.example ,tail\nskip,b.example,tail\n")
    assert read_domains(src, column=1) == ["a.example", "b.example"]


def test_read_domains_skips_blank_and_short_rows(tmp_path):
    src = tmp_path / "in.csv"
    # blank line, whitespace-only cell, and a row with no column 1 at all
    src.write_text("a.example,x\n\n   ,x\nonly-one-field\nb.example,x\n")
    assert read_domains(src, column=0) == ["a.example", "only-one-field", "b.example"]


def test_read_domains_column_out_of_range_yields_nothing(tmp_path):
    src = tmp_path / "in.csv"
    src.write_text("a.example\n")
    assert read_domains(src, column=5) == []


def test_read_domains_reads_stdin_for_dash(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("a.example\nb.example\n"))
    assert read_domains("-") == ["a.example", "b.example"]


# --- read_upstream ---------------------------------------------------------

def _write_upstream(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=ROW_FIELDS)
        w.writeheader()
        for row in rows:
            w.writerow({**{k: "" for k in ROW_FIELDS}, **row})


def test_read_upstream_splits_live_from_dead(tmp_path):
    src = tmp_path / "live_check.csv"
    _write_upstream(src, [
        {"domain": "up.example", "outcome": "valid", "scheme": "https"},
        {"domain": "dns.example", "outcome": "error"},
        {"domain": "slow.example", "outcome": "timeout"},
    ])
    up = read_upstream(src)
    assert up.domains == ["up.example"]
    assert set(up.dead) == {"dns.example", "slow.example"}


def test_read_upstream_carries_scheme_only_when_present(tmp_path):
    src = tmp_path / "live_check.csv"
    _write_upstream(src, [
        {"domain": "tls.example", "outcome": "valid", "scheme": "https"},
        {"domain": "plain.example", "outcome": "valid", "scheme": "http"},
        {"domain": "unknown.example", "outcome": "valid"},   # blank scheme
    ])
    up = read_upstream(src)
    assert up.schemes == {"tls.example": "https", "plain.example": "http"}
    assert "unknown.example" in up.domains       # still probed, just unpinned


def test_read_upstream_treats_non_dead_outcomes_as_probeable(tmp_path):
    # Only ERROR/TIMEOUT are dead. An inconclusive or negative host is retried.
    src = tmp_path / "wp_detect.csv"
    _write_upstream(src, [
        {"domain": "blocked.example", "outcome": "inconclusive"},
        {"domain": "notwp.example", "outcome": "negative"},
    ])
    up = read_upstream(src)
    assert up.domains == ["blocked.example", "notwp.example"]
    assert up.dead == {}


def test_read_upstream_ignores_rows_without_a_domain(tmp_path):
    src = tmp_path / "live_check.csv"
    _write_upstream(src, [
        {"domain": "", "outcome": "valid"},
        {"domain": "   ", "outcome": "error"},
        {"domain": "real.example", "outcome": "valid"},
    ])
    up = read_upstream(src)
    assert up.domains == ["real.example"]
    assert up.dead == {}


def test_read_upstream_records_stage_in_skip_reason(tmp_path):
    src = tmp_path / "wp_detect.csv"
    _write_upstream(src, [{"domain": "dead.example", "outcome": "error"}])
    up = read_upstream(src, stage="wp-detect")
    assert up.dead["dead.example"] == "skipped: error in wp-detect"


def test_read_upstream_stage_defaults_to_live_check(tmp_path):
    src = tmp_path / "live_check.csv"
    _write_upstream(src, [{"domain": "dead.example", "outcome": "error"}])
    assert read_upstream(src).dead["dead.example"] == "skipped: error in live-check"


def test_dead_outcomes_are_exactly_the_unanswered_ones():
    assert DEAD_OUTCOMES == {Outcome.ERROR.value, Outcome.TIMEOUT.value}


# --- Upstream.skipped_results ---------------------------------------------

def test_skipped_results_builds_one_row_per_dead_domain():
    up = Upstream(
        domains=[],
        schemes={},
        dead={"a.example": "skipped: error in live-check"},
    )
    (row,) = up.skipped_results()
    assert row.domain == "a.example"
    assert row.outcome is Outcome.SKIPPED
    assert row.detail == "skipped: error in live-check"
    assert row.status is None


def test_skipped_results_is_empty_when_nothing_died():
    assert Upstream(domains=["a.example"], schemes={}, dead={}).skipped_results() == []


# --- write_results ---------------------------------------------------------

def test_write_results_csv_headers_and_values(tmp_path):
    results = [ProbeResult("a.example", Outcome.VALID, status=200, scheme="https")]
    path = write_results(results, tmp_path, "out", "csv")

    assert path == tmp_path / "out.csv"
    rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    assert list(rows[0]) == list(ROW_FIELDS)
    assert rows[0]["domain"] == "a.example"
    assert rows[0]["outcome"] == "valid"
    assert rows[0]["status"] == "200"


def test_write_results_csv_json_encodes_the_data_column(tmp_path):
    results = [ProbeResult("a.example", Outcome.VALID, data={"names": ["admin", "ed"]})]
    path = write_results(results, tmp_path, "out", "csv")

    (row,) = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    assert json.loads(row["data"]) == {"names": ["admin", "ed"]}


def test_write_results_csv_leaves_empty_data_blank(tmp_path):
    # Empty dict becomes "" rather than "{}" so the column reads cleanly.
    path = write_results([ProbeResult("a.example", Outcome.VALID)], tmp_path, "out", "csv")
    (row,) = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    assert row["data"] == ""


def test_write_results_json_keeps_data_structured(tmp_path):
    results = [ProbeResult("a.example", Outcome.VALID, status=200, data={"n": [1]})]
    path = write_results(results, tmp_path, "out", "json")

    (row,) = json.loads(path.read_text(encoding="utf-8"))
    assert row["data"] == {"n": [1]}     # not a JSON string, unlike CSV
    assert row["status"] == 200


def test_write_results_creates_missing_directories(tmp_path):
    out = tmp_path / "deep" / "nested"
    path = write_results([ProbeResult("a.example", Outcome.VALID)], out, "out", "csv")
    assert path.exists()


def test_write_results_writes_header_even_with_no_rows(tmp_path):
    path = write_results([], tmp_path, "empty", "csv")
    assert path.read_text(encoding="utf-8").strip() == ",".join(ROW_FIELDS)


def test_write_results_rejects_unknown_format(tmp_path):
    with pytest.raises(ValueError, match="unknown format"):
        write_results([], tmp_path, "out", "xml")
