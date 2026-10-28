"""
Thin DataForSEO client. Only what Local Visibility needs: a live Google Maps
search at a coordinate, which is how a geo-grid rank is measured.

Docs: https://docs.dataforseo.com/v3/serp/google/maps/live/advanced/
"""

from dataclasses import dataclass

import requests
from django.conf import settings

BASE_URL = "https://api.dataforseo.com/v3"
MAPS_ENDPOINT = "/serp/google/maps/live/advanced"
OK_STATUS = 20000
REQUEST_TIMEOUT = 60


class DataForSeoError(Exception):
    pass


class DataForSeoNotConfigured(DataForSeoError):
    pass


@dataclass
class MapsResult:
    items: list[dict]
    cost: float


def is_configured() -> bool:
    return bool(settings.DATAFORSEO_API_LOGIN and settings.DATAFORSEO_API_PASSWORD)


def search_maps(keyword: str, latitude: float, longitude: float, zoom: int, depth: int = 20, language_code: str = "en") -> MapsResult:
    """Google Maps results for `keyword` as seen from a point. `items` are ordered; each has rank, title, domain, place_id, rating, reviews."""
    if not is_configured():
        raise DataForSeoNotConfigured("DataForSEO credentials are not set on the server.")

    body = [
        {
            "keyword": keyword,
            "location_coordinate": f"{latitude:.7f},{longitude:.7f},{zoom}z",
            "language_code": language_code,
            "device": "mobile",
            "depth": depth,
        }
    ]
    try:
        response = requests.post(
            BASE_URL + MAPS_ENDPOINT,
            json=body,
            auth=(settings.DATAFORSEO_API_LOGIN, settings.DATAFORSEO_API_PASSWORD),
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise DataForSeoError(f"Could not reach DataForSEO: {exc}") from exc

    if response.status_code == 401:
        raise DataForSeoError("DataForSEO rejected the API login or password.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise DataForSeoError(f"DataForSEO returned an unreadable response (HTTP {response.status_code}).") from exc

    return parse_maps_response(payload)


def parse_maps_response(payload: dict) -> MapsResult:
    if payload.get("status_code") != OK_STATUS:
        raise DataForSeoError(payload.get("status_message") or "DataForSEO request failed.")
    tasks = payload.get("tasks") or []
    task = tasks[0] if tasks else {}
    if task.get("status_code") != OK_STATUS:
        raise DataForSeoError(task.get("status_message") or "DataForSEO task failed.")

    results = task.get("result") or []
    raw_items = (results[0].get("items") if results else None) or []
    items = []
    for item in raw_items:
        if item.get("type") != "maps_search":
            continue
        rating = item.get("rating") or {}
        items.append(
            {
                "rank": item.get("rank_absolute"),
                "title": item.get("title") or "",
                "domain": item.get("domain") or "",
                "place_id": item.get("place_id") or "",
                "rating": rating.get("value"),
                "reviews": rating.get("votes_count"),
                "address": item.get("address") or "",
            }
        )
    return MapsResult(items=items, cost=float(payload.get("cost") or task.get("cost") or 0))
