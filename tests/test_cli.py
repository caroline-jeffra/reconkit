"""Tests for CLI argument wiring, input resolution, and output emission."""

import csv
import json

import responses
from typer.testing import CliRunner

from reconkit.cli import app
from reconkit.core.models import ROW_FIELDS

runner = CliRunner()


def _domains_file(tmp_path, *domains):
    src = tmp_path / "domains.csv"
    src.write_text("".join(f"{d}\n" for d in domains))
    return src


def _upstream_file(tmp_path, rows, name="live_check.csv"):
    src = tmp_path / name
    with open(src, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=ROW_FIELDS)
        w.writeheader()
        for row in rows:
            w.writerow({**{k: "" for k in ROW_FIELDS}, **row})
    return src


def _rows(path):
    return list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))


# --- argument validation ---------------------------------------------------

def test_no_args_is_help():
    assert "Usage" in runner.invoke(app, []).output


def test_wp_detect_without_any_input_is_rejected(tmp_path):
    result = runner.invoke(app, ["wp-detect", "--out-dir", str(tmp_path)])
    assert result.exit_code != 0
    assert "Provide a domain list or --from" in result.output


def test_wp_detect_rejects_both_a_list_and_from(tmp_path):
    src = _domains_file(tmp_path, "a.example")
    up = _upstream_file(tmp_path, [{"domain": "a.example", "outcome": "valid"}])
    result = runner.invoke(
        app, ["wp-detect", str(src), "--from", str(up), "--out-dir", str(tmp_path)]
    )
    assert result.exit_code != 0
    assert "not both" in result.output


def test_cf_subdomains_requires_a_token(tmp_path, monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    result = runner.invoke(app, ["cf-subdomains", "--out-dir", str(tmp_path)])
    assert result.exit_code != 0
    assert "CLOUDFLARE_API_TOKEN" in result.output


def test_live_check_still_requires_its_positional_argument(tmp_path):
    # live-check takes no --from, so its domain list stays mandatory.
    result = runner.invoke(app, ["live-check", "--out-dir", str(tmp_path)])
    assert result.exit_code != 0
    assert "Missing argument" in result.output


# --- standalone runs -------------------------------------------------------

@responses.activate
def test_live_check_writes_csv_and_summarises(tmp_path):
    responses.get("https://up.example/", status=200)
    src = _domains_file(tmp_path, "up.example", "dead.example")

    result = runner.invoke(app, ["live-check", str(src), "--out-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "2 rows" in result.output
    assert "valid=1" in result.output and "error=1" in result.output

    rows = _rows(tmp_path / "live_check.csv")
    assert {r["domain"] for r in rows} == {"up.example", "dead.example"}


@responses.activate
def test_wp_detect_standalone_writes_its_own_stem(tmp_path):
    responses.get("https://wp.example/wp-json", json={}, content_type="application/json")
    src = _domains_file(tmp_path, "wp.example")

    result = runner.invoke(app, ["wp-detect", str(src), "--out-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert (tmp_path / "wp_detect.csv").exists()


@responses.activate
def test_wp_users_standalone_writes_its_own_stem(tmp_path):
    responses.get("https://wp.example/wp-json/wp/v2/users", json=[{"name": "admin"}])
    src = _domains_file(tmp_path, "wp.example")

    runner.invoke(app, ["wp-users", str(src), "--out-dir", str(tmp_path)])
    (row,) = _rows(tmp_path / "wp_users.csv")
    assert json.loads(row["data"]) == {"names": ["admin"]}


@responses.activate
def test_json_format_option(tmp_path):
    responses.get("https://up.example/", status=200)
    src = _domains_file(tmp_path, "up.example")

    runner.invoke(app, ["live-check", str(src), "--out-dir", str(tmp_path), "-f", "json"])
    (row,) = json.loads((tmp_path / "live_check.json").read_text(encoding="utf-8"))
    assert row["domain"] == "up.example"


@responses.activate
def test_column_option_selects_the_domain_field(tmp_path):
    responses.get("https://up.example/", status=200)
    src = tmp_path / "domains.csv"
    src.write_text("ignore,up.example\n")

    runner.invoke(
        app, ["live-check", str(src), "--column", "1", "--out-dir", str(tmp_path)]
    )
    (row,) = _rows(tmp_path / "live_check.csv")
    assert row["domain"] == "up.example"


@responses.activate
def test_stdin_source(tmp_path):
    responses.get("https://up.example/", status=200)
    result = runner.invoke(
        app, ["live-check", "-", "--out-dir", str(tmp_path)], input="up.example\n"
    )
    assert result.exit_code == 0
    assert "1 rows" in result.output


# --- chained runs (--from) -------------------------------------------------

@responses.activate
def test_wp_detect_from_upstream_reuses_scheme_and_skips_dead(tmp_path):
    responses.get("http://up.example/wp-json", json={}, content_type="application/json")
    up = _upstream_file(tmp_path, [
        {"domain": "up.example", "outcome": "valid", "scheme": "http"},
        {"domain": "dead.example", "outcome": "error"},
    ])

    result = runner.invoke(
        app, ["wp-detect", "--from", str(up), "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0

    rows = {r["domain"]: r for r in _rows(tmp_path / "wp_detect.csv")}
    assert rows["up.example"]["outcome"] == "valid"
    assert rows["up.example"]["scheme"] == "http"
    assert rows["dead.example"]["outcome"] == "skipped"
    assert len(responses.calls) == 1        # the dead host was never probed


@responses.activate
def test_wp_users_from_upstream_skips_dead(tmp_path):
    responses.get("https://up.example/wp-json/wp/v2/users", json=[{"name": "admin"}])
    up = _upstream_file(tmp_path, [
        {"domain": "up.example", "outcome": "valid"},
        {"domain": "dead.example", "outcome": "timeout"},
    ])

    result = runner.invoke(
        app, ["wp-users", "--from", str(up), "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0
    assert "skipped=1" in result.output


@responses.activate
def test_chained_run_preserves_the_upstream_row_count(tmp_path):
    # Skipped rows are carried through so a chain never silently loses domains.
    responses.get("https://a.example/wp-json", json={}, content_type="application/json")
    up = _upstream_file(tmp_path, [
        {"domain": "a.example", "outcome": "valid"},
        {"domain": "b.example", "outcome": "error"},
        {"domain": "c.example", "outcome": "timeout"},
    ])

    result = runner.invoke(
        app, ["wp-detect", "--from", str(up), "--out-dir", str(tmp_path)]
    )
    assert "3 rows" in result.output
    assert len(_rows(tmp_path / "wp_detect.csv")) == 3


# --- summary line ----------------------------------------------------------

@responses.activate
def test_summary_counts_are_sorted_and_totalled(tmp_path):
    responses.get("https://a.example/", status=200)
    responses.get("https://b.example/", status=200)
    src = _domains_file(tmp_path, "a.example", "b.example", "dead.example")

    result = runner.invoke(app, ["live-check", str(src), "--out-dir", str(tmp_path)])
    assert "3 rows (error=1, valid=2)" in result.output


@responses.activate
def test_out_dir_is_created(tmp_path):
    responses.get("https://a.example/", status=200)
    src = _domains_file(tmp_path, "a.example")
    out = tmp_path / "new" / "dir"

    assert runner.invoke(app, ["live-check", str(src), "--out-dir", str(out)]).exit_code == 0
    assert (out / "live_check.csv").exists()


@responses.activate
def test_cf_subdomains_writes_results_with_a_token(tmp_path):
    responses.get(
        "https://api.cloudflare.com/client/v4/zones",
        json={"result": [{"id": "z1", "name": "example.com"}],
              "result_info": {"page": 1, "total_pages": 1}},
    )
    responses.get(
        "https://api.cloudflare.com/client/v4/zones/z1/dns_records",
        json={"result": [{"name": "www.example.com"}]},
    )

    result = runner.invoke(
        app, ["cf-subdomains", "--api-token", "tok", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0
    (row,) = _rows(tmp_path / "cf_subdomains.csv")
    assert row["domain"] == "www.example.com"
    assert json.loads(row["data"]) == {"zone": "example.com"}


@responses.activate
def test_cf_subdomains_reads_the_token_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "env-token")
    responses.get(
        "https://api.cloudflare.com/client/v4/zones",
        json={"result": [], "result_info": {"page": 1, "total_pages": 1}},
    )

    result = runner.invoke(app, ["cf-subdomains", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert responses.calls[0].request.headers["Authorization"] == "Bearer env-token"
