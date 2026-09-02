"""Tests for the shared session, scheme fallback, and failure classification."""

import requests
import responses

from reconkit.core.http import (
    RETRY_STATUSES,
    SCHEMES,
    Fetch,
    classify_failure,
    fetch,
    make_session,
)
from reconkit.core.models import Outcome

# --- make_session ----------------------------------------------------------


def test_make_session_mounts_retrying_adapters_for_both_schemes():
    session = make_session(retries=3)
    for prefix in ("http://", "https://"):
        retry = session.get_adapter(prefix).max_retries
        assert retry.total == 3
        assert set(RETRY_STATUSES) <= set(retry.status_forcelist)


def test_make_session_only_retries_get():
    assert make_session().get_adapter("https://").max_retries.allowed_methods == {"GET"}


# --- Fetch -----------------------------------------------------------------


def test_fetch_answered_reflects_presence_of_a_response():
    assert not Fetch(error=ValueError()).answered
    assert not Fetch().answered


def test_fetch_status_is_none_without_a_response():
    assert Fetch(error=ValueError()).status is None


def test_fetch_final_url_is_blank_without_a_response():
    assert Fetch(error=ValueError()).final_url == ""


@responses.activate
def test_fetch_reports_status_and_scheme_that_answered():
    responses.get("https://a.example/p", status=204)
    got = fetch(make_session(), "a.example", "/p")
    assert got.answered
    assert got.status == 204
    assert got.scheme == "https"


@responses.activate
def test_fetch_final_url_blank_when_not_redirected():
    # final_url exists to flag movement; an unmoved probe leaves it empty.
    responses.get("https://a.example/", status=200)
    assert fetch(make_session(), "a.example").final_url == ""


@responses.activate
def test_fetch_final_url_set_when_redirected():
    responses.get(
        "https://a.example/", status=302, headers={"Location": "https://b.example/"}
    )
    responses.get("https://b.example/", status=200)
    assert fetch(make_session(), "a.example").final_url == "https://b.example/"


# --- fetch: scheme order ---------------------------------------------------


@responses.activate
def test_fetch_prefers_https_then_falls_back_to_http():
    responses.get("https://a.example/", body=requests.ConnectionError("no tls"))
    responses.get("http://a.example/", status=200)

    got = fetch(make_session(), "a.example")
    assert got.scheme == "http"
    assert [c.request.url for c in responses.calls] == [
        "https://a.example/",
        "http://a.example/",
    ]


@responses.activate
def test_fetch_pinned_scheme_skips_the_other_one():
    responses.get("http://a.example/", status=200)
    got = fetch(make_session(), "a.example", scheme="http")
    assert got.scheme == "http"
    assert len(responses.calls) == 1  # https never attempted


@responses.activate
def test_fetch_stale_pin_still_falls_back():
    # A cached scheme can go stale between runs, so a failed pin retries the rest.
    responses.get("https://a.example/", body=requests.ConnectionError("no tls"))
    responses.get("http://a.example/", status=200)

    got = fetch(make_session(), "a.example", scheme="https")
    assert got.scheme == "http"


@responses.activate
def test_fetch_never_retries_the_same_scheme_twice():
    responses.get("https://a.example/", body=requests.ConnectionError("boom"))
    responses.get("http://a.example/", body=requests.ConnectionError("boom"))

    fetch(make_session(), "a.example", scheme="https")
    assert len(responses.calls) == len(SCHEMES)  # pin deduped against SCHEMES


@responses.activate
def test_fetch_reports_the_first_failure_when_every_scheme_fails():
    # HTTPS is the informative cause; HTTP is usually just "also down".
    responses.get("https://a.example/", body=requests.ConnectionError("tls cause"))
    responses.get("http://a.example/", body=requests.ConnectionError("http cause"))

    got = fetch(make_session(), "a.example")
    assert not got.answered
    assert got.scheme == ""
    assert "tls cause" in str(got.error)


@responses.activate
def test_fetch_appends_the_path():
    responses.get("https://a.example/wp-json", status=200)
    assert fetch(make_session(), "a.example", "/wp-json").status == 200


@responses.activate
def test_fetch_passes_the_timeout_through():
    seen = {}

    def record(request):
        seen["timeout"] = request.req_kwargs.get("timeout")
        return (200, {}, "")

    responses.add_callback(responses.GET, "https://a.example/", callback=record)
    fetch(make_session(), "a.example", timeout=1.25)
    assert seen["timeout"] == 1.25


# --- classify_failure ------------------------------------------------------


def test_classify_failure_maps_timeout():
    got = Fetch(error=requests.exceptions.ConnectTimeout())
    result = classify_failure("a.example", got)
    assert result.outcome is Outcome.TIMEOUT
    assert result.detail == "ConnectTimeout"


def test_classify_failure_maps_retry_exhaustion_to_inconclusive():
    # The server answered repeatedly, just never usefully — that is not silence.
    result = classify_failure(
        "a.example", Fetch(error=requests.exceptions.RetryError())
    )
    assert result.outcome is Outcome.INCONCLUSIVE
    assert "retries exhausted" in result.detail


def test_classify_failure_maps_connection_error():
    result = classify_failure("a.example", Fetch(error=requests.ConnectionError()))
    assert result.outcome is Outcome.ERROR
    assert result.detail == "ConnectionError"


def test_classify_failure_handles_a_missing_exception():
    result = classify_failure("a.example", Fetch())
    assert result.outcome is Outcome.ERROR
    assert result.detail == "no response"


def test_classify_failure_leaves_transport_fields_unset():
    result = classify_failure("a.example", Fetch(error=requests.ConnectionError()))
    assert (result.status, result.scheme, result.final_url) == (None, "", "")
