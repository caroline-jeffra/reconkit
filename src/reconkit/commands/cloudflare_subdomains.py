"""Enumerate A-record subdomains across all zones in a Cloudflare account."""

from __future__ import annotations

import requests

from ..core.http import make_session
from ..core.models import Outcome, ProbeResult

BASE_URL = "https://api.cloudflare.com/client/v4"


def _headers(api_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}


def _get_zones(session: requests.Session, api_token: str) -> list[dict]:
    zones: list[dict] = []
    page = 1
    while True:
        resp = session.get(
            f"{BASE_URL}/zones",
            headers=_headers(api_token),
            params={"page": page, "per_page": 50},
        )
        resp.raise_for_status()
        body = resp.json()
        zones += body["result"]
        info = body.get("result_info", {})
        if page >= info.get("total_pages", page):
            break
        page += 1
    return zones


def _get_a_records(session: requests.Session, api_token: str, zone_id: str) -> list[str]:
    resp = session.get(
        f"{BASE_URL}/zones/{zone_id}/dns_records",
        headers=_headers(api_token),
        params={"type": "A"},
    )
    resp.raise_for_status()
    return [r["name"] for r in resp.json()["result"]]


def enumerate_subdomains(api_token: str) -> list[ProbeResult]:
    """Return one ProbeResult per subdomain; `domain` is the subdomain, data.zone is its zone."""
    session = make_session()
    results: list[ProbeResult] = []
    for zone in _get_zones(session, api_token):
        for name in _get_a_records(session, api_token, zone["id"]):
            results.append(
                ProbeResult(name, Outcome.VALID, data={"zone": zone["name"]})
            )
    return results