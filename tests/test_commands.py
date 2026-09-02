"""Tests for the probe commands: live-check, wp-detect, wp-users, cf-subdomains."""

import requests
import responses

from reconkit.commands import cloudflare_subdomains, live_check, wp_detect, wp_users
from reconkit.core.models import Outcome

# --- live_check ------------------------------------------------------------

@responses.activate
def test_live_check_treats_any_answer_as_valid():
    # Reachability triage: a 404 host is still serving, so it is VALID.
    responses.get("https://up.example/", status=200)
    responses.get("https://notfound.example/", status=404)
    responses.get("https://blocked.example/", status=403)

    outcomes = {
        r.domain: r.outcome
        for r in live_check.check_live(["up.example", "notfound.example", "blocked.example"])
    }
    assert set(outcomes.values()) == {Outcome.VALID}


@responses.activate
def test_live_check_records_status_and_scheme():
    responses.get("https://up.example/", status=200)
    (result,) = live_check.check_live(["up.example"])
    assert result.status == 200
    assert result.scheme == "https"
    assert result.detail == ""


@responses.activate
def test_live_check_flags_http_only_hosts_as_no_tls():
    responses.get("https://plain.example/", body=requests.ConnectionError("no tls"))
    responses.get("http://plain.example/", status=200)

    (result,) = live_check.check_live(["plain.example"])
    assert result.outcome is Outcome.VALID
    assert result.scheme == "http"
    assert result.detail == "no tls"


@responses.activate
def test_live_check_unreachable_host_is_error():
    (result,) = live_check.check_live(["dead.example"])
    assert result.outcome is Outcome.ERROR


@responses.activate
def test_live_check_reports_a_refusing_host_as_valid():
    # 403 is not retried, so it reaches the verdict: the host is answering.
    responses.get("https://a.example/", status=403)
    (result,) = live_check.check_live(["a.example"])
    assert result.outcome is Outcome.VALID
    assert result.status == 403


@responses.activate
def test_live_check_returns_one_row_per_domain_in_order():
    responses.get("https://a.example/", status=200)
    responses.get("https://c.example/", status=200)
    results = live_check.check_live(["a.example", "b.example", "c.example"])
    assert [r.domain for r in results] == ["a.example", "b.example", "c.example"]


# --- wp_detect -------------------------------------------------------------

@responses.activate
def test_wp_detect_json_content_type_is_valid():
    responses.get(
        "https://wp.example/wp-json", json={"name": "site"},
        content_type="application/json",
    )
    (result,) = wp_detect.detect_wordpress(["wp.example"])
    assert result.outcome is Outcome.VALID
    assert result.status == 200


@responses.activate
def test_wp_detect_accepts_json_content_type_with_charset():
    responses.get(
        "https://wp.example/wp-json", body="{}",
        content_type="application/json; charset=UTF-8",
    )
    assert wp_detect.detect_wordpress(["wp.example"])[0].outcome is Outcome.VALID


@responses.activate
def test_wp_detect_html_is_negative_and_reports_the_type():
    responses.get("https://site.example/wp-json", body="<html>", content_type="text/html")
    (result,) = wp_detect.detect_wordpress(["site.example"])
    assert result.outcome is Outcome.NEGATIVE
    assert result.detail.startswith("text/html")


@responses.activate
def test_wp_detect_probes_the_wp_json_endpoint():
    responses.get("https://wp.example/wp-json", json={}, content_type="application/json")
    wp_detect.detect_wordpress(["wp.example"])
    assert responses.calls[0].request.url.endswith("/wp-json")


@responses.activate
def test_wp_detect_uses_a_pinned_scheme():
    responses.get("http://wp.example/wp-json", json={}, content_type="application/json")
    (result,) = wp_detect.detect_wordpress(["wp.example"], schemes={"wp.example": "http"})
    assert result.scheme == "http"
    assert len(responses.calls) == 1


@responses.activate
def test_wp_detect_ignores_a_pin_meant_for_another_domain():
    responses.get("https://wp.example/wp-json", json={}, content_type="application/json")
    (result,) = wp_detect.detect_wordpress(["wp.example"], schemes={"other.example": "http"})
    assert result.scheme == "https"


@responses.activate
def test_wp_detect_unreachable_host_is_error():
    assert wp_detect.detect_wordpress(["dead.example"])[0].outcome is Outcome.ERROR


# --- wp_detect: blocked / server-error statuses ----------------------------

@responses.activate
def test_wp_detect_auth_statuses_are_inconclusive():
    # 401/403 are not in RETRY_STATUSES, so they survive to the status check.
    for i, status in enumerate((401, 403)):
        responses.get(f"https://b{i}.example/wp-json", status=status, body="nope")

    results = wp_detect.detect_wordpress([f"b{i}.example" for i in range(2)])
    assert {r.outcome for r in results} == {Outcome.INCONCLUSIVE}
    assert {r.detail for r in results} == {"blocked"}


@responses.activate
def test_wp_detect_retryable_status_exhausts_retries_and_is_inconclusive():
    # 429/5xx are retried by the session; once exhausted the transport raises
    # RetryError, which classify_failure also lands on INCONCLUSIVE.
    responses.get("https://err.example/wp-json", status=503, body="boom")
    (result,) = wp_detect.detect_wordpress(["err.example"])
    assert result.outcome is Outcome.INCONCLUSIVE
    assert "retries exhausted" in result.detail


@responses.activate
def test_wp_detect_404_is_a_real_negative_verdict():
    # A 404 on /wp-json is an answer: this site is not WordPress.
    responses.get("https://site.example/wp-json", status=404, content_type="text/html")
    assert wp_detect.detect_wordpress(["site.example"])[0].outcome is Outcome.NEGATIVE


# --- wp_users --------------------------------------------------------------

@responses.activate
def test_wp_users_exposed_authors_are_valid():
    responses.get(
        "https://leaky.example/wp-json/wp/v2/users",
        json=[{"id": 1, "name": "admin"}, {"id": 2, "name": "editor"}],
    )
    (result,) = wp_users.check_wp_users(["leaky.example"])
    assert result.outcome is Outcome.VALID
    assert result.detail == "admin"                     # first name, for the CSV
    assert result.data == {"names": ["admin", "editor"]}


@responses.activate
def test_wp_users_probes_the_users_endpoint():
    responses.get("https://a.example/wp-json/wp/v2/users", json=[])
    wp_users.check_wp_users(["a.example"])
    assert responses.calls[0].request.url.endswith("/wp-json/wp/v2/users")


@responses.activate
def test_wp_users_empty_list_is_negative():
    responses.get("https://empty.example/wp-json/wp/v2/users", json=[])
    (result,) = wp_users.check_wp_users(["empty.example"])
    assert result.outcome is Outcome.NEGATIVE           # and no IndexError


@responses.activate
def test_wp_users_list_without_a_name_field_is_negative():
    responses.get("https://odd.example/wp-json/wp/v2/users", json=[{"id": 1}])
    (result,) = wp_users.check_wp_users(["odd.example"])
    assert result.outcome is Outcome.NEGATIVE
    assert result.detail == "no name field"


@responses.activate
def test_wp_users_json_error_object_is_negative_with_its_code():
    responses.get(
        "https://locked.example/wp-json/wp/v2/users",
        json={"code": "rest_user_cannot_view", "message": "Sorry."},
        status=200,
    )
    (result,) = wp_users.check_wp_users(["locked.example"])
    assert result.outcome is Outcome.NEGATIVE
    assert result.detail == "rest_user_cannot_view"


@responses.activate
def test_wp_users_non_json_body_is_negative():
    responses.get("https://html.example/wp-json/wp/v2/users", body="<html>", status=200)
    (result,) = wp_users.check_wp_users(["html.example"])
    assert result.outcome is Outcome.NEGATIVE
    assert result.detail == "JSONDecodeError"


@responses.activate
def test_wp_users_skips_non_dict_entries_when_collecting_names():
    responses.get(
        "https://mixed.example/wp-json/wp/v2/users",
        json=[{"name": "admin"}, "junk", {"id": 2}],
    )
    (result,) = wp_users.check_wp_users(["mixed.example"])
    assert result.data == {"names": ["admin", ""]}       # "junk" dropped, {"id": 2} -> ""


@responses.activate
def test_wp_users_blocked_statuses_are_inconclusive():
    for i, status in enumerate((401, 403)):
        responses.get(f"https://b{i}.example/wp-json/wp/v2/users", status=status, body="x")

    results = wp_users.check_wp_users([f"b{i}.example" for i in range(2)])
    assert {r.outcome for r in results} == {Outcome.INCONCLUSIVE}
    assert {r.detail for r in results} == {"blocked"}


@responses.activate
def test_wp_users_uses_a_pinned_scheme():
    responses.get("http://a.example/wp-json/wp/v2/users", json=[])
    (result,) = wp_users.check_wp_users(["a.example"], schemes={"a.example": "http"})
    assert result.scheme == "http"
    assert len(responses.calls) == 1


@responses.activate
def test_wp_users_unreachable_host_is_error():
    assert wp_users.check_wp_users(["dead.example"])[0].outcome is Outcome.ERROR


# --- cloudflare_subdomains -------------------------------------------------

def _zone_page(zones, page, total_pages):
    return {"result": zones, "result_info": {"page": page, "total_pages": total_pages}}


@responses.activate
def test_cf_returns_one_row_per_a_record_tagged_with_its_zone():
    responses.get(
        "https://api.cloudflare.com/client/v4/zones",
        json=_zone_page([{"id": "z1", "name": "example.com"}], 1, 1),
    )
    responses.get(
        "https://api.cloudflare.com/client/v4/zones/z1/dns_records",
        json={"result": [{"name": "www.example.com"}, {"name": "api.example.com"}]},
    )

    results = cloudflare_subdomains.enumerate_subdomains("tok")
    assert [r.domain for r in results] == ["www.example.com", "api.example.com"]
    assert {r.outcome for r in results} == {Outcome.VALID}
    assert results[0].data == {"zone": "example.com"}


@responses.activate
def test_cf_sends_the_bearer_token():
    responses.get("https://api.cloudflare.com/client/v4/zones", json=_zone_page([], 1, 1))
    cloudflare_subdomains.enumerate_subdomains("secret-token")
    assert responses.calls[0].request.headers["Authorization"] == "Bearer secret-token"


@responses.activate
def test_cf_requests_only_a_records():
    responses.get(
        "https://api.cloudflare.com/client/v4/zones",
        json=_zone_page([{"id": "z1", "name": "example.com"}], 1, 1),
    )
    responses.get(
        "https://api.cloudflare.com/client/v4/zones/z1/dns_records", json={"result": []}
    )
    cloudflare_subdomains.enumerate_subdomains("tok")
    assert "type=A" in responses.calls[1].request.url


@responses.activate
def test_cf_follows_zone_pagination():
    responses.get(
        "https://api.cloudflare.com/client/v4/zones",
        json=_zone_page([{"id": "z1", "name": "one.com"}], 1, 2),
    )
    responses.get(
        "https://api.cloudflare.com/client/v4/zones",
        json=_zone_page([{"id": "z2", "name": "two.com"}], 2, 2),
    )
    for zid in ("z1", "z2"):
        responses.get(
            f"https://api.cloudflare.com/client/v4/zones/{zid}/dns_records",
            json={"result": [{"name": f"www.{zid}.com"}]},
        )

    results = cloudflare_subdomains.enumerate_subdomains("tok")
    assert [r.domain for r in results] == ["www.z1.com", "www.z2.com"]


@responses.activate
def test_cf_no_zones_yields_no_rows():
    responses.get("https://api.cloudflare.com/client/v4/zones", json=_zone_page([], 1, 1))
    assert cloudflare_subdomains.enumerate_subdomains("tok") == []


@responses.activate
def test_cf_raises_on_an_auth_failure():
    responses.get(
        "https://api.cloudflare.com/client/v4/zones", status=403, json={"errors": ["bad token"]}
    )
    try:
        cloudflare_subdomains.enumerate_subdomains("bad")
    except requests.HTTPError:
        return
    raise AssertionError("expected an HTTPError for a rejected token")
