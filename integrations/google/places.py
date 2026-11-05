"""
Google Places API (New) text search. Pure Python, no Django imports; the caller
passes the API key. Used to turn a business name/address into map coordinates.
"""

from __future__ import annotations

from dataclasses import dataclass

import requests

_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
_FIELD_MASK = "places.id,places.displayName,places.formattedAddress,places.location,places.websiteUri"


class PlacesError(Exception):
    pass


@dataclass
class Place:
    place_id: str
    name: str
    address: str
    latitude: float
    longitude: float
    website: str


def search_places(api_key: str, query: str, limit: int = 5) -> list[Place]:
    try:
        response = requests.post(
            _SEARCH_URL,
            json={"textQuery": query, "pageSize": limit},
            headers={"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": _FIELD_MASK},
            timeout=10,
        )
    except requests.RequestException as exc:
        raise PlacesError("Couldn't reach Google Places.") from exc
    if response.status_code != 200:
        message = ""
        try:
            message = response.json().get("error", {}).get("message", "")
        except ValueError:
            pass
        if response.status_code in (400, 403) and ("API key" in message or "not enabled" in message or "blocked" in message or response.status_code == 403):
            raise PlacesError("Google rejected the Places API key set on the server. Check the key, and that Places API (New) is enabled for it.")
        raise PlacesError(message or f"Google Places returned {response.status_code}.")

    places = []
    for item in response.json().get("places", []):
        location = item.get("location") or {}
        if "latitude" not in location or "longitude" not in location:
            continue
        places.append(
            Place(
                place_id=item.get("id", ""),
                name=(item.get("displayName") or {}).get("text", ""),
                address=item.get("formattedAddress", ""),
                latitude=location["latitude"],
                longitude=location["longitude"],
                website=item.get("websiteUri", ""),
            )
        )
    return places
