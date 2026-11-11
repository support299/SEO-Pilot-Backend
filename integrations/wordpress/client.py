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

import html
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlparse

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
# `aioseo_meta_data` is only present when All in One SEO is installed; WordPress ignores unknown _fields.
WP_OBJECT_FIELDS = WP_LIST_FIELDS + ",meta,aioseo_meta_data"


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

    try:
        response = requests.request(method, url, headers=headers, json=body, timeout=timeout, allow_redirects=False)
    except requests.RequestException as exc:
        raise WordPressError("Couldn't reach the WordPress site. Check that it is online and try again.") from exc

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


# REST namespaces each supported SEO plugin registers. The site root lists every namespace, so one
# unauthenticated GET tells us which plugin (if any) will accept the title/description we write.
SEO_PLUGIN_NAMESPACES = (("aioseo", "aioseo/v1"), ("yoast", "yoast/v1"), ("rank_math", "rankmath/v1"))
SEO_PLUGIN_LABELS = {"aioseo": "All in One SEO", "yoast": "Yoast SEO", "rank_math": "Rank Math", "": "none detected"}


def detect_seo_plugin(api_url: str) -> str:
    """'aioseo', 'yoast', 'rank_math' or '' when none of the supported plugins is active."""
    payload = wp_fetch_json(api_url, "/")
    namespaces = set(payload.get("namespaces") or []) if isinstance(payload, dict) else set()
    for key, namespace in SEO_PLUGIN_NAMESPACES:
        if namespace in namespaces:
            return key
    return ""


def can_publish(user: WpUser) -> bool:
    caps = user.capabilities
    return bool(caps.get("edit_pages") or caps.get("edit_posts") or caps.get("publish_posts") or caps.get("administrator"))


MAX_LIST_PAGES = 10  # safety cap: 10 x 100 = 1000 items per type


def list_objects(api_url: str, object_type: str, username: str, application_password: str, *, limit: int = 100) -> list[WpObject]:
    """Fetch every page/post by walking WordPress's `page=N` pagination (max 100 per request)."""
    endpoint = "pages" if object_type == "page" else "posts"
    per_page = min(limit, 100)
    objects: list[WpObject] = []
    for page in range(1, MAX_LIST_PAGES + 1):
        try:
            payload = wp_fetch_json(
                api_url,
                f"/wp/v2/{endpoint}?context=edit&per_page={per_page}&page={page}&status=publish,draft,pending,private&_fields={WP_LIST_FIELDS}",
                username=username,
                application_password=application_password,
                timeout=LIST_REQUEST_TIMEOUT_SECONDS,
            )
        except WordPressError:
            if page == 1:
                raise
            break  # WordPress answers 400 for a page number past the last one
        items = payload if isinstance(payload, list) else []
        objects.extend(_to_wp_object(item) for item in items)
        if len(items) < per_page:
            break
    return objects


def fetch_object(api_url: str, object_type: str, object_id: int, username: str, application_password: str) -> WpObject:
    endpoint = "pages" if object_type == "page" else "posts"
    payload = wp_fetch_json(api_url, f"/wp/v2/{endpoint}/{object_id}?context=edit&_fields={WP_OBJECT_FIELDS}", username=username, application_password=application_password)
    return _to_wp_object(payload)


def update_object(api_url: str, object_type: str, object_id: int, changes: dict, username: str, application_password: str) -> WpObject:
    """
    `changes` may contain `title`, `meta_description`, `canonical_url` and
    `robots` ("index" or "noindex"). Where they go depends on the SEO plugin:

    - All in One SEO keeps these in its own table, not post meta, so they're
      written through AIOSEO's `aioseo_meta_data` field, falling back to AIOSEO's
      own `/aioseo/v1/post` endpoint if that field isn't writable on the site.
      The title here is the SEO title (the `<title>` tag), NOT the WordPress
      page title — themes reuse that one for menu labels, so editing it would
      rename navigation items.
    - Without AIOSEO, `title` is the WordPress page title, and the other values
      go to Yoast's and Rank Math's known post-meta names unconditionally —
      neither plugin minds an unknown meta key existing.

    Callers re-read the object afterwards and fail the change if the value
    didn't actually land, so a write that a plugin ignores is never reported
    as published. (`image_alt` is handled separately, see `write_image_alts`.)
    """
    endpoint = "pages" if object_type == "page" else "posts"
    route = f"/wp/v2/{endpoint}/{object_id}"
    auth = {"username": username, "application_password": application_password}
    wants = {key for key in ("title", "meta_description", "canonical_url", "robots") if changes.get(key) is not None}
    aioseo = _fetch_aioseo_data(api_url, route, auth) if wants else None

    body: dict = {}
    if aioseo is not None:
        # Empty strings are valid here: rolling back to "no value" must clear the field.
        wanted = _aioseo_wanted(changes)
        body["aioseo_meta_data"] = wanted
    else:
        meta: dict = {}
        if changes.get("title"):
            body["title"] = changes["title"]
        if changes.get("meta_description") is not None:
            meta["rank_math_description"] = meta["_yoast_wpseo_metadesc"] = changes["meta_description"]
        if changes.get("canonical_url") is not None:
            meta["rank_math_canonical_url"] = meta["_yoast_wpseo_canonical"] = changes["canonical_url"]
        if changes.get("robots") in ("index", "noindex"):
            meta["rank_math_robots"] = [changes["robots"]]
            meta["_yoast_wpseo_meta-robots-noindex"] = "1" if changes["robots"] == "noindex" else "2"
        if meta:
            body["meta"] = meta
    payload = wp_fetch_json(api_url, route, method="POST", body=body, **auth) if body else wp_fetch_json(api_url, f"{route}?context=edit&_fields={WP_OBJECT_FIELDS}", **auth)

    if aioseo is not None:
        wanted = body["aioseo_meta_data"]
        current = _fetch_aioseo_data(api_url, route, auth) or {}
        if not _aioseo_matches(current, wanted):
            # Fields weren't writable through the core endpoint — use AIOSEO's own, sending
            # the full existing record so no other AIOSEO setting is reset.
            wp_fetch_json(api_url, "/aioseo/v1/post", method="POST", body={**current, "id": object_id, **wanted}, **auth)
    return _to_wp_object(payload)


def _aioseo_wanted(changes: dict) -> dict:
    wanted: dict = {}
    for key, source in (("title", "title"), ("description", "meta_description"), ("canonical_url", "canonical_url")):
        if changes.get(source) is not None:
            wanted[key] = changes[source]
    robots = changes.get("robots")
    if robots in ("index", "noindex"):
        # robots_default=False means "use this page's own setting instead of the site-wide default".
        wanted["robots_default"] = False
        wanted["robots_noindex"] = robots == "noindex"
    elif robots == "default":
        wanted["robots_default"] = True
    return wanted


def _aioseo_matches(current: dict, wanted: dict) -> bool:
    for key, value in wanted.items():
        if isinstance(value, bool):
            if bool(current.get(key)) != value:
                return False
        elif html.unescape(current.get(key) or "").strip() != value.strip():
            return False
    return True


def _fetch_aioseo_data(api_url: str, route: str, auth: dict) -> dict | None:
    """AIOSEO's per-post SEO settings, or None if the site doesn't run All in One SEO."""
    payload = wp_fetch_json(api_url, f"{route}?context=edit&_fields=aioseo_meta_data", **auth)
    data = payload.get("aioseo_meta_data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else None


def _to_wp_object(payload: dict) -> WpObject:
    title = payload.get("title", {})
    meta = payload.get("meta") or {}
    meta = dict(meta) if isinstance(meta, dict) else {}
    aioseo = payload.get("aioseo_meta_data")
    if isinstance(aioseo, dict):
        # AIOSEO HTML-escapes stored values (e.g. "&amp;"); compare on the plain text.
        meta["_aioseo_title"] = html.unescape(aioseo.get("title") or "")
        meta["_aioseo_description"] = html.unescape(aioseo.get("description") or "")
        meta["_aioseo_canonical"] = aioseo.get("canonical_url") or ""
        meta["_aioseo_robots"] = "default" if aioseo.get("robots_default", True) else ("noindex" if aioseo.get("robots_noindex") else "index")
    return WpObject(
        id=payload.get("id"),
        title=title.get("raw") or title.get("rendered") or "" if isinstance(title, dict) else str(title or ""),
        slug=payload.get("slug") or "",
        status=payload.get("status") or "unknown",
        link=payload.get("link"),
        modified_gmt=payload.get("modified_gmt"),
        meta=meta,
    )


# --- Image alt text -----------------------------------------------------------

IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
ALT_ATTR = re.compile(r"""\salt\s*=\s*("[^"]*"|'[^']*')""", re.IGNORECASE)
SRC_ATTR = re.compile(r"""\ssrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
SIZE_SUFFIX = re.compile(r"-\d+x\d+(?=\.[a-z0-9]+$)", re.IGNORECASE)


def image_key(src: str) -> str:
    """The file name of an image without its query string or WordPress size suffix, so `photo-300x200.jpg` and `photo.jpg` match."""
    name = unquote(urlparse(src or "").path.rsplit("/", 1)[-1]).lower()
    return SIZE_SUFFIX.sub("", name)


def _tag_alt(tag: str) -> str:
    match = ALT_ATTR.search(tag)
    return html.unescape(match.group(1)[1:-1]) if match else ""


def _with_alt(tag: str, alt: str) -> str:
    value = 'alt="' + html.escape(alt, quote=True) + '"'
    if ALT_ATTR.search(tag):
        return ALT_ATTR.sub(lambda m: " " + value, tag, count=1)
    return re.sub(r"\s*/?>$", lambda m: f" {value}{m.group(0).lstrip()}" if m.group(0).strip().startswith("/") else f" {value}>", tag, count=1)


def _fetch_content_raw(api_url: str, route: str, auth: dict) -> str:
    payload = wp_fetch_json(api_url, f"{route}?context=edit&_fields=content", **auth)
    content = payload.get("content") if isinstance(payload, dict) else None
    return (content.get("raw") if isinstance(content, dict) else "") or ""


def _find_media(api_url: str, key: str, auth: dict) -> dict | None:
    stem = key.rsplit(".", 1)[0]
    try:
        items = wp_fetch_json(api_url, f"/wp/v2/media?context=edit&per_page=20&search={stem}&_fields=id,source_url,alt_text", **auth)
    except WordPressError:
        return None
    for item in items if isinstance(items, list) else []:
        if image_key(item.get("source_url") or "") == key:
            return item
    return None


def read_image_alts(api_url: str, object_type: str, object_id: int, srcs: list[str], username: str, application_password: str) -> list[dict]:
    """For each image: its alt text inside the page content and in the media library (None where the image isn't there)."""
    endpoint = "pages" if object_type == "page" else "posts"
    auth = {"username": username, "application_password": application_password}
    content = _fetch_content_raw(api_url, f"/wp/v2/{endpoint}/{object_id}", auth)
    tags = IMG_TAG.findall(content)
    result = []
    for src in srcs:
        key = image_key(src)
        found = [t for t in tags if image_key((SRC_ATTR.search(t) or [None, ""])[1]) == key]
        media = _find_media(api_url, key, auth) if key else None
        result.append(
            {
                "src": src,
                "content_alt": _tag_alt(found[0]) if found else None,
                "media_id": media["id"] if media else None,
                "media_alt": (media.get("alt_text") or "") if media else None,
            }
        )
    return result


def write_image_alts(api_url: str, object_type: str, object_id: int, entries: list[dict], username: str, application_password: str) -> None:
    """Set alt text on each image tag in the page content and on its media-library item. `None` values are left untouched."""
    endpoint = "pages" if object_type == "page" else "posts"
    route = f"/wp/v2/{endpoint}/{object_id}"
    auth = {"username": username, "application_password": application_password}
    content = _fetch_content_raw(api_url, route, auth)
    updated = content
    for entry in entries:
        if entry.get("content_alt") is None:
            continue
        key = image_key(entry["src"])
        updated = IMG_TAG.sub(lambda m: _with_alt(m.group(0), entry["content_alt"]) if image_key((SRC_ATTR.search(m.group(0)) or [None, ""])[1]) == key else m.group(0), updated)
    if updated != content:
        wp_fetch_json(api_url, route, method="POST", body={"content": updated}, **auth)
    for entry in entries:
        if entry.get("media_id") is not None and entry.get("media_alt") is not None:
            wp_fetch_json(api_url, f"/wp/v2/media/{entry['media_id']}", method="POST", body={"alt_text": entry["media_alt"]}, **auth)
