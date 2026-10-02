"""
Google OAuth + GA4 Admin/Data API client. Same shape as search_console.py:
callers pass client id, secret, and redirect URI. No Django imports.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlencode, urlparse

import requests

logger = logging.getLogger(__name__)

ANALYTICS_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_ADMIN_URL = "https://analyticsadmin.googleapis.com/v1beta"
_DATA_URL = "https://analyticsdata.googleapis.com/v1beta"


class GoogleApiError(Exception):
    pass


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_in: int
    scope: str


@dataclass
class Property:
    property_id: str
    property_name: str
    account_id: str
    website_url: str | None


@dataclass
class DailyOrganic:
    day: date
    sessions: int
    active_users: int
    conversions: int


def build_authorization_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": ANALYTICS_SCOPE,
        "access_type": "offline",
        "include_granted_scopes": "true",
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
    if not response.ok or "access_token" not in payload:
        raise GoogleApiError(_error_message(payload, "Google did not grant access."))
    if not payload.get("refresh_token"):
        raise GoogleApiError("Google did not return a refresh token. Remove this app's access in your Google Account permissions, then connect again.")
    return TokenSet(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        expires_in=payload.get("expires_in", 3600),
        scope=payload.get("scope", ANALYTICS_SCOPE),
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
        scope=payload.get("scope", ANALYTICS_SCOPE),
    )


def list_properties(access_token: str) -> list[Property]:
    properties = _properties_from_summaries(access_token)
    if properties:
        return properties
    return _properties_from_accounts(access_token)


def _properties_from_summaries(access_token: str) -> list[Property]:
    summaries = _paged(access_token, f"{_ADMIN_URL}/accountSummaries", "accountSummaries")
    properties: list[Property] = []
    for account in summaries:
        account_id = str(account.get("account", "")).removeprefix("accounts/")
        for summary in account.get("propertySummaries") or []:
            prop = _property_from_summary(access_token, account_id, summary)
            if prop:
                properties.append(prop)
    return properties


def _properties_from_accounts(access_token: str) -> list[Property]:
    """accountSummaries can be empty for some property-level grants. accounts.list is the second look."""
    try:
        accounts = _paged(access_token, f"{_ADMIN_URL}/accounts", "accounts")
    except GoogleApiError as exc:
        logger.warning("Could not list Analytics accounts: %s", exc)
        return []
    properties: list[Property] = []
    for account in accounts:
        account_name = account.get("name") or ""
        account_id = account_name.removeprefix("accounts/")
        if not account_id:
            continue
        try:
            rows = _paged(access_token, f"{_ADMIN_URL}/properties", "properties", {"filter": f"parent:{account_name}"})
        except GoogleApiError as exc:
            logger.warning("Could not list properties for account %s: %s", account_id, exc)
            continue
        for row in rows:
            property_id = str(row.get("name", "")).removeprefix("properties/")
            if not property_id:
                continue
            properties.append(
                Property(
                    property_id=property_id,
                    property_name=row.get("displayName") or f"Property {property_id}",
                    account_id=account_id,
                    website_url=_web_stream_uri(access_token, property_id),
                )
            )
    return properties


def _property_from_summary(access_token: str, account_id: str, summary: dict) -> Property | None:
    property_id = str(summary.get("property", "")).removeprefix("properties/")
    if not property_id:
        return None
    return Property(
        property_id=property_id,
        property_name=summary.get("displayName") or f"Property {property_id}",
        account_id=account_id,
        website_url=_web_stream_uri(access_token, property_id),
    )


def pick_best_property(properties: list[Property], website: str | None) -> Property | None:
    """Match the business website. One property with no conflicting site is used."""
    if not properties:
        return None

    host = _hostname(website) if website else None
    if not host:
        return properties[0] if len(properties) == 1 else None

    matches = [prop for prop in properties if _property_matches(prop, host)]
    if matches:

        def rank(prop: Property) -> tuple[int, str]:
            stream_host = _hostname(prop.website_url) if prop.website_url else None
            exact = 0 if stream_host == host else 1
            return (exact, prop.property_id)

        return sorted(matches, key=rank)[0]

    if len(properties) == 1 and not _conflicts(properties[0], host):
        return properties[0]
    return None


def list_key_events(access_token: str, property_id: str) -> list[str]:
    rows = _paged(access_token, f"{_ADMIN_URL}/properties/{property_id}/keyEvents", "keyEvents")
    names: list[str] = []
    for row in rows:
        name = (row.get("eventName") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def fetch_organic_daily(*, access_token: str, property_id: str, start_date: str, end_date: str) -> list[DailyOrganic]:
    """Organic Search sessions. `conversions` is GA4's key-event count and is only meaningful when key events exist."""
    raw_rows = _run_report(
        access_token=access_token,
        property_id=property_id,
        start_date=start_date,
        end_date=end_date,
        dimensions=["date"],
        metrics=["sessions", "activeUsers", "conversions"],
        dimension_filter={
            "filter": {
                "fieldName": "sessionDefaultChannelGroup",
                "stringFilter": {"matchType": "EXACT", "value": "Organic Search"},
            }
        },
    )
    daily: list[DailyOrganic] = []
    for row in raw_rows:
        dimensions = row.get("dimensionValues") or []
        metrics = row.get("metricValues") or []
        day_raw = dimensions[0].get("value") if dimensions else ""
        if not day_raw:
            continue
        daily.append(
            DailyOrganic(
                day=_parse_ga_date(day_raw),
                sessions=_metric_int(metrics, 0),
                active_users=_metric_int(metrics, 1),
                conversions=_metric_int(metrics, 2),
            )
        )
    return daily


def _property_matches(prop: Property, host: str) -> bool:
    if _conflicts(prop, host):
        return False
    if _hostname(prop.website_url):
        return True
    name = prop.property_name.lower()
    if host in name:
        return True
    label = host.split(".")[0]
    return len(label) >= 4 and label in name


def _conflicts(prop: Property, host: str) -> bool:
    stream_host = _hostname(prop.website_url) if prop.website_url else None
    if not stream_host:
        return False
    return not (stream_host == host or stream_host.endswith(f".{host}") or host.endswith(f".{stream_host}"))


def _hostname(url: str | None) -> str | None:
    if not url:
        return None
    try:
        netloc = urlparse(url if "://" in url else f"https://{url}").netloc.lower()
    except ValueError:
        return None
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc or None


def _web_stream_uri(access_token: str, property_id: str) -> str | None:
    try:
        streams = _paged(access_token, f"{_ADMIN_URL}/properties/{property_id}/dataStreams", "dataStreams")
    except GoogleApiError as exc:
        logger.warning("Could not list data streams for property %s: %s", property_id, exc)
        return None
    for stream in streams:
        web = stream.get("webStreamData") or {}
        uri = web.get("defaultUri")
        if uri:
            return uri
    return None


def _paged(access_token: str, url: str, key: str, extra_params: dict | None = None) -> list[dict]:
    rows: list[dict] = []
    page_token = None
    for _ in range(10):
        params = {"pageSize": 200, **(extra_params or {})}
        if page_token:
            params["pageToken"] = page_token
        payload = _get_json(access_token, url, params)
        rows.extend(payload.get(key) or [])
        page_token = payload.get("nextPageToken")
        if not page_token:
            break
    return rows


def _run_report(*, access_token: str, property_id: str, start_date: str, end_date: str, dimensions: list[str], metrics: list[str], dimension_filter: dict) -> list[dict]:
    url = f"{_DATA_URL}/properties/{property_id}:runReport"
    rows: list[dict] = []
    offset = 0
    while True:
        response = requests.post(
            url,
            headers={"authorization": f"Bearer {access_token}", "content-type": "application/json"},
            json={
                "dateRanges": [{"startDate": start_date, "endDate": end_date}],
                "dimensions": [{"name": name} for name in dimensions],
                "metrics": [{"name": name} for name in metrics],
                "dimensionFilter": dimension_filter,
                "keepEmptyRows": False,
                "limit": 10000,
                "offset": offset,
            },
            timeout=30,
        )
        payload = response.json() if response.content else {}
        if not response.ok:
            raise GoogleApiError(_error_message(payload, "Google Analytics request failed."))
        batch = payload.get("rows") or []
        rows.extend(batch)
        row_count = int(payload.get("rowCount") or len(rows))
        offset += len(batch)
        if not batch or offset >= row_count:
            break
    return rows


def _get_json(access_token: str, url: str, params: dict) -> dict:
    response = requests.get(url, headers={"authorization": f"Bearer {access_token}"}, params=params, timeout=20)
    payload = response.json() if response.content else {}
    if not response.ok:
        raise GoogleApiError(_error_message(payload, "Google Analytics request failed."))
    return payload


def _error_message(payload: object, fallback: str) -> str:
    if not isinstance(payload, dict):
        return fallback
    error = payload.get("error_description") or payload.get("error")
    if isinstance(error, dict):
        return error.get("message") or fallback
    if isinstance(error, str) and error:
        return error
    return fallback


def _metric_int(metrics: list[dict], index: int) -> int:
    if index >= len(metrics):
        return 0
    raw = metrics[index].get("value") or "0"
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return 0


def _parse_ga_date(value: str) -> date:
    if len(value) == 8 and value.isdigit():
        return date(int(value[0:4]), int(value[4:6]), int(value[6:8]))
    return date.fromisoformat(value)
