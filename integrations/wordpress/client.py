"""
WordPress REST API client, authenticated with an Application Password
(WordPress core since 5.6 — no OAuth needed). Pure Python, no Django imports.

Ported from the old Next.js app's src/lib/seo-pilot-wordpress.ts, which had
already solved the safety-critical parts of this: SSRF protection on the
website URL (so a customer can't point this at an internal network address),
response size/timeout limits, and REST API auto-discovery via the site's own
`Link` header. Same logic here, same reasons, just in Python.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin

import requests

REQUEST_TIMEOUT_SECONDS = 10
# Listing pages/posts fetches up to 100 items with extra statuses in one request,
# which some WordPress hosts answer much slower than single-object lookups.
LIST_REQUEST_TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 1_000_000
# Only ask WordPress for the fields we read. context=edit otherwise returns every
# page's full body, and `meta` carries page-builder data (_elementor_data can be
# 100KB+ per page) — together that made the 100-item list slow and over the size cap.
# The list view needs no meta; a single-object fetch does (current meta description).
WP_LIST_FIELDS = "id,title,slug,status,link,modified_gmt"
WP_OBJECT_FIELDS = WP_LIST_FIELDS + ",meta"


class WordPressError(Exception):
    pass


class UnsafeTargetError(WordPressError):
    """The given URL/host is not an allowed target (private IP, non-HTTPS, etc.) — always a user-facing message, never leaks *why* beyond that."""


@dataclass
class WpUser:
    id: int | None
    name: str
    capabilities: dict


@dataclass
class WpObject:
    id: int
    title: str
    slug: str
    status: str
    link: str | None
    modified_gmt: str | None
    meta: dict


def normalize_https_url(value: str) -> str:
    value = value.strip()
    if not value:
        raise WordPressError("Enter your website URL.")
    if not value.startswith("http"):
        value = f"https://{value}"
    from urllib.parse import urlparse

    parsed = urlparse(value)
    if parsed.scheme != "https":
        raise WordPressError("Use an HTTPS WordPress website URL.")
    return f"{parsed.scheme}://{parsed.netloc}/"


def assert_public_http_target(url: str) -> None:
    """
    SSRF guard: blocks anything that isn't a plain public HTTPS host on the
    standard port — in particular private/internal IP ranges, localhost, and
    credentials embedded in the URL. This runs before every single request
    this client makes, not just at connect time, since a DNS record or
    redirect could otherwise be used to retarget a later request.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise UnsafeTargetError("Use an HTTPS WordPress website URL.")
    if parsed.username or parsed.password:
        raise UnsafeTargetError("Do not include credentials in the website URL.")
    if parsed.port not in (None, 443):
        raise UnsafeTargetError("Use the standard HTTPS port for WordPress connections.")

    host = parsed.hostname or ""
    if host in ("localhost", "0.0.0.0", "::1") or host.endswith(".localhost"):
        raise UnsafeTargetError("That website address is not allowed.")

    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        addresses = set()

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise UnsafeTargetError("That website address is not allowed.")


def discover_api_url(website_url: str) -> str:
    assert_public_http_target(website_url)
    response = requests.get(website_url, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=False)
    link_header = response.headers.get("link", "")
    api_url = None
    for part in link_header.split(","):
        if 'rel="https://api.w.org/"' in part:
            api_url = part.split(";")[0].strip().strip("<>")
            break
    api_url = api_url or urljoin(website_url, "/wp-json")
    assert_public_http_target(api_url)
    return api_url.rstrip("/")


def _auth_header(username: str, application_password: str) -> dict:
    import base64

    token = base64.b64encode(f"{username}:{application_password}".encode("utf-8")).decode("ascii")
    return {"authorization": f"Basic {token}"}


def wp_fetch_json(api_url: str, route: str, *, username: str | None = None, application_password: str | None = None, method: str = "GET", body: dict | None = None, timeout: int = REQUEST_TIMEOUT_SECONDS):
    base = api_url.rstrip("/") + "/"
    url = urljoin(base, route.lstrip("/")) if route != "/" else api_url
    assert_public_http_target(url)

    headers = {"accept": "application/json"}
    if username and application_password:
        headers.update(_auth_header(username, application_password))
    if body is not None:
        headers["content-type"] = "application/json"

    response = requests.request(method, url, headers=headers, json=body, timeout=timeout, allow_redirects=False)

    content = response.content[: MAX_RESPONSE_BYTES + 1]
    if len(content) > MAX_RESPONSE_BYTES:
        raise WordPressError("The website response was too large to process safely.")

    try:
        payload = response.json() if content else {}
    except ValueError:
        payload = {}

    if not response.ok:
        message = payload.get("message") if isinstance(payload, dict) else None
        raise WordPressError(message or f"WordPress returned an error (HTTP {response.status_code}).")

    return payload


def verify_user(api_url: str, username: str, application_password: str) -> WpUser:
    payload = wp_fetch_json(api_url, "/wp/v2/users/me?context=edit", username=username, application_password=application_password)
    return WpUser(id=payload.get("id"), name=payload.get("name") or username, capabilities=payload.get("capabilities") or {})


def can_publish(user: WpUser) -> bool:
    caps = user.capabilities
    return bool(caps.get("edit_pages") or caps.get("edit_posts") or caps.get("publish_posts") or caps.get("administrator"))


def list_objects(api_url: str, object_type: str, username: str, application_password: str, *, limit: int = 100) -> list[WpObject]:
    endpoint = "pages" if object_type == "page" else "posts"
    payload = wp_fetch_json(
        api_url,
        f"/wp/v2/{endpoint}?context=edit&per_page={min(limit, 100)}&status=publish,draft,pending,private&_fields={WP_LIST_FIELDS}",
        username=username,
        application_password=application_password,
        timeout=LIST_REQUEST_TIMEOUT_SECONDS,
    )
    return [_to_wp_object(item) for item in (payload if isinstance(payload, list) else [])]


def fetch_object(api_url: str, object_type: str, object_id: int, username: str, application_password: str) -> WpObject:
    endpoint = "pages" if object_type == "page" else "posts"
    payload = wp_fetch_json(api_url, f"/wp/v2/{endpoint}/{object_id}?context=edit&_fields={WP_OBJECT_FIELDS}", username=username, application_password=application_password)
    return _to_wp_object(payload)


def update_object(api_url: str, object_type: str, object_id: int, changes: dict, username: str, application_password: str) -> WpObject:
    """
    `changes` may contain `title` and/or `meta_description`. The meta
    description is written to both Yoast's and RankMath's known field names
    unconditionally — neither plugin minds an unknown meta key existing, so
    this avoids needing to detect which (if either) is actually installed,
    same pragmatic approach the old app used.
    """
    endpoint = "pages" if object_type == "page" else "posts"
    body: dict = {}
    if "title" in changes and changes["title"]:
        body["title"] = changes["title"]
    if "meta_description" in changes and changes["meta_description"]:
        body["meta"] = {
            "rank_math_description": changes["meta_description"],
            "_yoast_wpseo_metadesc": changes["meta_description"],
        }
    payload = wp_fetch_json(api_url, f"/wp/v2/{endpoint}/{object_id}", username=username, application_password=application_password, method="POST", body=body)
    return _to_wp_object(payload)


def _to_wp_object(payload: dict) -> WpObject:
    title = payload.get("title", {})
    meta = payload.get("meta") or {}
    return WpObject(
        id=payload.get("id"),
        title=title.get("raw") or title.get("rendered") or "" if isinstance(title, dict) else str(title or ""),
        slug=payload.get("slug") or "",
        status=payload.get("status") or "unknown",
        link=payload.get("link"),
        modified_gmt=payload.get("modified_gmt"),
        meta=meta if isinstance(meta, dict) else {},
    )
