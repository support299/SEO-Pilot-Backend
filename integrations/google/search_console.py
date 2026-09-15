"""
Google OAuth + Search Console API client. Pure Python, no Django imports —
this is the exact same contract as the old app's
src/lib/google/search-console.ts, ported rather than reinvented. Callers pass
in config (client id/secret, redirect uri) instead of this module reading
settings directly, so it stays independently testable and reusable from a
Celery task or a management command.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, urlencode

import requests

SEARCH_CONSOLE_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SITES_URL = "https://www.googleapis.com/webmasters/v3/sites"


class GoogleApiError(Exception):
    pass


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_in: int
    scope: str


@dataclass
class Site:
    site_url: str
    permission_level: str


@dataclass
class AnalyticsRow:
    key: str
    clicks: int
    impressions: int
    ctr: float
    position: float


def build_authorization_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SEARCH_CONSOLE_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{_AUTH_URL}?{urlencode(params)}"


def exchange_code_for_tokens(*, client_id: str, client_secret: str, code: str, redirect_uri: str) -> TokenSet:
    response = requests.post(
        _TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=15,
    )
    payload = response.json()
    if not response.ok or "access_token" not in payload or "refresh_token" not in payload:
        raise GoogleApiError(payload.get("error_description") or payload.get("error") or "Google did not grant access.")
    return TokenSet(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        expires_in=payload.get("expires_in", 3600),
        scope=payload.get("scope", SEARCH_CONSOLE_SCOPE),
    )


def refresh_access_token(*, client_id: str, client_secret: str, refresh_token: str) -> TokenSet:
    response = requests.post(
        _TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=15,
    )
    payload = response.json()
    if not response.ok or "access_token" not in payload:
        raise GoogleApiError(payload.get("error_description") or payload.get("error") or "Google access has expired. Please reconnect.")
    return TokenSet(
        access_token=payload["access_token"],
        refresh_token=refresh_token,
        expires_in=payload.get("expires_in", 3600),
        scope=payload.get("scope", SEARCH_CONSOLE_SCOPE),
    )


def list_sites(access_token: str) -> list[Site]:
    response = requests.get(_SITES_URL, headers={"authorization": f"Bearer {access_token}"}, timeout=15)
    payload = response.json() if response.content else {}
    if not response.ok:
        raise GoogleApiError(payload.get("error", {}).get("message") or "Could not list Search Console properties.")
    return [
        Site(site_url=entry["siteUrl"], permission_level=entry.get("permissionLevel", "unknown"))
        for entry in payload.get("siteEntry", [])
        if entry.get("siteUrl")
    ]


def pick_best_site(sites: list[Site], website: str | None) -> Site | None:
    """Picks the site matching the business's website's domain, or the first available one."""
    if not sites:
        return None
    if not website:
        return sites[0]

    host = _safe_hostname(website)
    if not host:
        return sites[0]

    for site in sites:
        site_host = site.site_url.replace("sc-domain:", "") if site.site_url.startswith("sc-domain:") else _safe_hostname(site.site_url)
        if site_host == host:
            return site
    return sites[0]


def _safe_hostname(url: str) -> str | None:
    from urllib.parse import urlparse

    try:
        netloc = urlparse(url if "://" in url else f"https://{url}").netloc
        return netloc[4:] if netloc.startswith("www.") else netloc or None
    except ValueError:
        return None


def _query_analytics(*, access_token: str, site_url: str, start_date: str, end_date: str, dimensions: list[str], row_limit: int) -> list[dict]:
    url = f"{_SITES_URL}/{quote(site_url, safe='')}/searchAnalytics/query"
    response = requests.post(
        url,
        headers={"authorization": f"Bearer {access_token}", "content-type": "application/json"},
        json={"startDate": start_date, "endDate": end_date, "dimensions": dimensions, "dataState": "final", "rowLimit": row_limit},
        timeout=30,
    )
    payload = response.json() if response.content else {}
    if not response.ok:
        raise GoogleApiError(payload.get("error", {}).get("message") or "Search Console request failed.")
    return payload.get("rows", [])


def fetch_daily_analytics(*, access_token: str, site_url: str, start_date: str, end_date: str) -> list[AnalyticsRow]:
    rows = _query_analytics(access_token=access_token, site_url=site_url, start_date=start_date, end_date=end_date, dimensions=["date"], row_limit=1000)
    return [_row_from_raw(row) for row in rows]


def fetch_top_queries(*, access_token: str, site_url: str, start_date: str, end_date: str, limit: int = 10) -> list[AnalyticsRow]:
    rows = _query_analytics(access_token=access_token, site_url=site_url, start_date=start_date, end_date=end_date, dimensions=["query"], row_limit=limit)
    return [_row_from_raw(row) for row in rows]


def fetch_top_pages(*, access_token: str, site_url: str, start_date: str, end_date: str, limit: int = 10) -> list[AnalyticsRow]:
    rows = _query_analytics(access_token=access_token, site_url=site_url, start_date=start_date, end_date=end_date, dimensions=["page"], row_limit=limit)
    return [_row_from_raw(row) for row in rows]


def _row_from_raw(row: dict) -> AnalyticsRow:
    keys = row.get("keys") or [""]
    return AnalyticsRow(
        key=keys[0],
        clicks=row.get("clicks", 0),
        impressions=row.get("impressions", 0),
        ctr=row.get("ctr", 0.0),
        position=row.get("position", 0.0),
    )
