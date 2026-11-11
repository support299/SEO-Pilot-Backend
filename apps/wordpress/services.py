from __future__ import annotations

from django.conf import settings
from django.utils import timezone

from apps.businesses.models import Business
from apps.history.services import record_event
from common.crypto import decrypt_secret, encrypt_secret
from integrations.wordpress import client as wp

from .models import ChangePacket, WordPressConnection


class WordPressIntegrationError(Exception):
    pass


def _encryption_secret() -> str:
    value = settings.WORDPRESS_CREDENTIAL_ENCRYPTION_KEY
    if not value:
        raise WordPressIntegrationError("WordPress connections are not configured on this server yet.")
    return value


# --- Connection ---------------------------------------------------------------


def connect(business: Business, *, website_url: str, username: str, application_password: str) -> WordPressConnection:
    website_url = wp.normalize_https_url(website_url)
    username = username.strip()
    application_password = application_password.strip()
    if not username or not application_password:
        raise WordPressIntegrationError("Enter the WordPress username and application password.")

    try:
        api_url = wp.discover_api_url(website_url)
        user = wp.verify_user(api_url, username, application_password)
    except wp.WordPressError as exc:
        raise WordPressIntegrationError(str(exc)) from exc

    if not wp.can_publish(user):
        raise WordPressIntegrationError("That WordPress user doesn't have permission to edit pages or posts.")
    seo_plugin = _detect_plugin(api_url)

    secret = _encryption_secret()
    connection, _created = WordPressConnection.objects.update_or_create(
        business=business,
        defaults={
            "website_url": website_url,
            "api_url": api_url,
            "username": username,
            "application_password_enc": encrypt_secret({"application_password": application_password}, secret),
            "wp_user_id": str(user.id or ""),
            "wp_user_name": user.name,
            "status": WordPressConnection.Status.CONNECTED,
            "last_verified_at": timezone.now(),
            "last_error": None,
            "seo_plugin": seo_plugin,
            "seo_plugin_checked_at": timezone.now(),
        },
    )
    record_event(business, "wordpress.connected", f"Connected WordPress ({website_url}). SEO plugin: {wp.SEO_PLUGIN_LABELS[seo_plugin]}.", metadata={"website_url": website_url, "seo_plugin": seo_plugin})
    return connection


def _detect_plugin(api_url: str) -> str:
    """Best effort: a site that hides its REST index must not block connecting."""
    try:
        return wp.detect_seo_plugin(api_url)
    except wp.WordPressError:
        return ""


def refresh_plugin_check(connection: WordPressConnection) -> WordPressConnection:
    connection.seo_plugin = _detect_plugin(connection.api_url)
    connection.seo_plugin_checked_at = timezone.now()
    connection.save(update_fields=["seo_plugin", "seo_plugin_checked_at", "updated_at"])
    return connection


def set_publishing(business: Business, enabled: bool, *, user) -> WordPressConnection:
    connection = require_connection(business)
    if connection.publishing_enabled != enabled:
        connection.publishing_enabled = enabled
        connection.save(update_fields=["publishing_enabled", "updated_at"])
        record_event(
            business,
            "wordpress.publishing_on" if enabled else "wordpress.publishing_off",
            "Turned publishing to WordPress on." if enabled else "Turned publishing to WordPress off: this WordPress does not control the live site.",
            actor=user,
        )
    return connection


PUBLISHING_OFF_MESSAGE = "Publishing is off for this website: its WordPress doesn't control the live site. Track the finding in the work plan instead."


def serialize_connection(connection: WordPressConnection) -> dict:
    return {
        "connected": True,
        "website_url": connection.website_url,
        "wp_user_name": connection.wp_user_name,
        "status": connection.status,
        "connected_at": connection.connected_at.isoformat(),
        "last_verified_at": connection.last_verified_at.isoformat() if connection.last_verified_at else None,
        "last_error": connection.last_error,
        "publishing_enabled": connection.publishing_enabled,
        "seo_plugin": connection.seo_plugin,
        "seo_plugin_label": wp.SEO_PLUGIN_LABELS.get(connection.seo_plugin, connection.seo_plugin),
        "seo_plugin_checked_at": connection.seo_plugin_checked_at.isoformat() if connection.seo_plugin_checked_at else None,
    }


def disconnect(business: Business) -> None:
    deleted, _ = WordPressConnection.objects.filter(business=business).delete()
    if deleted:
        record_event(business, "wordpress.disconnected", "Disconnected WordPress.")


def _credentials(connection: WordPressConnection) -> tuple[str, str]:
    secret = _encryption_secret()
    payload = decrypt_secret(connection.application_password_enc, secret)
    return connection.username, payload["application_password"]


def get_connection(business: Business) -> WordPressConnection | None:
    return WordPressConnection.objects.filter(business=business).first()


def require_connection(business: Business) -> WordPressConnection:
    connection = get_connection(business)
    if connection is None:
        raise WordPressIntegrationError("Connect WordPress first.")
    return connection


def list_inventory(business: Business, object_type: str) -> list[dict]:
    connection = require_connection(business)
    username, app_password = _credentials(connection)
    try:
        items = wp.list_objects(connection.api_url, object_type, username, app_password)
    except wp.WordPressError as exc:
        raise WordPressIntegrationError(str(exc)) from exc
    return [_serialize_wp_object(item, object_type) for item in items]


# --- Change packets ------------------------------------------------------------

CHANGE_FIELDS = ("title", "meta_description", "canonical_url", "robots", "image_alt")
TEXT_FIELD_LABELS = {"title": "title", "meta_description": "meta description", "canonical_url": "canonical URL", "robots": "index/noindex setting"}
MAX_IMAGES_PER_CHANGE = 20
MAX_CANONICAL_LENGTH = 500
MAX_ALT_LENGTH = 300


def clean_extra_changes(raw: dict) -> dict:
    """Validate the canonical_url, robots and image_alt parts of a proposal (title and meta description are checked by the callers)."""
    changes: dict = {}
    canonical = raw.get("canonical_url")
    if canonical not in (None, ""):
        if not isinstance(canonical, str) or not canonical.strip().lower().startswith(("http://", "https://")) or len(canonical.strip()) > MAX_CANONICAL_LENGTH:
            raise WordPressIntegrationError(f"canonical_url must be a full web address starting with http:// or https:// (max {MAX_CANONICAL_LENGTH} characters).")
        changes["canonical_url"] = canonical.strip()
    robots = raw.get("robots")
    if robots not in (None, ""):
        if robots not in ("index", "noindex"):
            raise WordPressIntegrationError('robots must be "index" or "noindex".')
        changes["robots"] = robots
    images = raw.get("image_alt")
    if images not in (None, [], ""):
        if not isinstance(images, list) or len(images) > MAX_IMAGES_PER_CHANGE:
            raise WordPressIntegrationError(f"image_alt must be a list of at most {MAX_IMAGES_PER_CHANGE} images.")
        cleaned = []
        for item in images:
            src, alt = (item.get("src"), item.get("alt")) if isinstance(item, dict) else (None, None)
            if not isinstance(src, str) or not src.strip() or not isinstance(alt, str) or not alt.strip() or len(alt.strip()) > MAX_ALT_LENGTH:
                raise WordPressIntegrationError(f'Each image_alt item needs "src" (the image address) and "alt" (text, max {MAX_ALT_LENGTH} characters).')
            cleaned.append({"src": src.strip(), "alt": alt.strip()})
        changes["image_alt"] = cleaned
    return changes


def prepare_change_packet(business: Business, *, user, target_object_type: str, target_object_id: int, proposed_changes: dict, reason: str = "", source: str = "manual", agent_name: str = "") -> ChangePacket:
    proposed_changes = {**{key: proposed_changes[key] for key in ("title", "meta_description") if proposed_changes.get(key)}, **clean_extra_changes(proposed_changes)}
    if not any(proposed_changes.get(key) for key in CHANGE_FIELDS):
        raise WordPressIntegrationError("Propose at least one change: a title, meta description, canonical URL, robots setting or image alt text.")

    connection = require_connection(business)
    if not connection.publishing_enabled:
        raise WordPressIntegrationError(PUBLISHING_OFF_MESSAGE)
    username, app_password = _credentials(connection)
    try:
        current = wp.fetch_object(connection.api_url, target_object_type, target_object_id, username, app_password)
    except wp.WordPressError as exc:
        raise WordPressIntegrationError(str(exc)) from exc

    packet = ChangePacket.objects.create(
        business=business,
        target_object_type=target_object_type,
        target_object_id=target_object_id,
        target_url=current.link or "",
        proposed_changes=proposed_changes,
        reason=reason,
        current_revision=current.modified_gmt or "",
        created_by=user,
        source=source,
        agent_name=agent_name,
    )
    if source == "agent":
        # Attribute it to the agent, not to the admin whose key it used.
        summary, actor = f"AI agent \"{agent_name}\" proposed a change to \"{current.title}\".", None
    else:
        summary, actor = f"Proposed a change to \"{current.title}\".", user
    record_event(
        business,
        "wordpress.change_proposed",
        summary,
        actor=actor,
        metadata={"packet_id": packet.id, "fields": list(proposed_changes.keys()), "source": source},
    )
    return packet


DECIDABLE_STATUSES = {ChangePacket.Status.AWAITING_APPROVAL, ChangePacket.Status.STALE}


def decide_change_packet(packet: ChangePacket, *, user, approve: bool) -> ChangePacket:
    if packet.status not in DECIDABLE_STATUSES:
        raise WordPressIntegrationError("This change has already been decided.")

    was_stale = packet.status == ChangePacket.Status.STALE
    packet.status = ChangePacket.Status.APPROVED if approve else ChangePacket.Status.REJECTED
    packet.approved_by = user
    packet.approved_at = timezone.now()
    update_fields = ["status", "approved_by", "approved_at", "updated_at"]

    if approve and was_stale:
        # Re-approving a stale change means "I've reviewed the page as it is
        # now" — refresh current_revision to the live page's current state,
        # or the very next apply would immediately go stale again.
        connection = require_connection(packet.business)
        username, app_password = _credentials(connection)
        try:
            current = wp.fetch_object(connection.api_url, packet.target_object_type, packet.target_object_id, username, app_password)
            packet.current_revision = current.modified_gmt or ""
            update_fields.append("current_revision")
        except wp.WordPressError as exc:
            raise WordPressIntegrationError(str(exc)) from exc

    packet.save(update_fields=update_fields)
    verb = "Approved" if approve else "Rejected"
    record_event(packet.business, f"wordpress.change_{packet.status}", f"{verb} a proposed WordPress change.", actor=user, metadata={"packet_id": packet.id})
    return packet


def apply_change_packet(packet: ChangePacket) -> ChangePacket:
    """
    Safety-checked before touching anything live: refuses to apply if the
    page changed since the proposal was prepared (`current_revision` no
    longer matches), so an approval granted days ago can't silently overwrite
    someone else's more recent edit.
    """
    if packet.status != ChangePacket.Status.APPROVED:
        raise WordPressIntegrationError("Approve this change before applying it.")

    connection = require_connection(packet.business)
    if not connection.publishing_enabled:
        _fail(packet, PUBLISHING_OFF_MESSAGE)
        raise WordPressIntegrationError(PUBLISHING_OFF_MESSAGE)
    username, app_password = _credentials(connection)

    try:
        before = wp.fetch_object(connection.api_url, packet.target_object_type, packet.target_object_id, username, app_password)
    except wp.WordPressError as exc:
        _fail(packet, str(exc))
        raise WordPressIntegrationError(str(exc)) from exc

    if packet.current_revision and before.modified_gmt != packet.current_revision:
        packet.status = ChangePacket.Status.STALE
        packet.last_error = "The page changed after this update was prepared."
        packet.save(update_fields=["status", "last_error", "updated_at"])
        record_event(packet.business, "wordpress.change_stale", "A proposed change could not be applied — the page changed since it was prepared.", metadata={"packet_id": packet.id})
        raise WordPressIntegrationError("That page changed after this update was prepared. Review it again before applying.")

    packet.status = ChangePacket.Status.APPLYING
    proposed = packet.proposed_changes
    extra = [key for key in ("canonical_url", "robots") if key in proposed]
    images = proposed.get("image_alt") or []
    target = (connection.api_url, packet.target_object_type, packet.target_object_id)
    packet.before_snapshot = _snapshot(before, extra)
    try:
        if images:
            packet.before_snapshot["image_alt"] = wp.read_image_alts(*target, [i["src"] for i in images], username, app_password)
    except wp.WordPressError as exc:
        _fail(packet, str(exc))
        raise WordPressIntegrationError(str(exc)) from exc
    packet.save(update_fields=["status", "before_snapshot", "updated_at"])

    try:
        if any(key in proposed for key in ("title", "meta_description", "canonical_url", "robots")):
            wp.update_object(*target, proposed, username, app_password)
        if images:
            wp.write_image_alts(*target, _image_entries(images, packet.before_snapshot["image_alt"]), username, app_password)
        after = wp.fetch_object(*target, username, app_password)
        after_images = wp.read_image_alts(*target, [i["src"] for i in images], username, app_password) if images else None
    except wp.WordPressError as exc:
        _fail(packet, str(exc))
        raise WordPressIntegrationError(str(exc)) from exc

    after_snapshot = _snapshot(after, extra)
    not_saved = [label for key, label in TEXT_FIELD_LABELS.items() if proposed_value_missing(proposed, after_snapshot, key)]
    if images:
        after_snapshot["image_alt"] = after_images
        missed = _images_not_saved(images, after_images)
        if missed:
            not_saved.append(f"alt text on {len(missed)} image(s) ({', '.join(missed[:3])})")
    if not_saved:
        message = f"WordPress accepted the update but the {' and '.join(not_saved)} did not change on the page. The site needs a supported SEO plugin (All in One SEO, Yoast or Rank Math) that allows editing it from outside, and images must be in the page content or media library."
        _fail(packet, message)
        raise WordPressIntegrationError(message)

    packet.status = ChangePacket.Status.APPLIED
    packet.after_snapshot = after_snapshot
    packet.applied_at = timezone.now()
    packet.last_error = ""
    packet.save(update_fields=["status", "after_snapshot", "applied_at", "last_error", "updated_at"])
    record_event(packet.business, "wordpress.change_applied", f"Published a change to \"{after.title}\".", metadata={"packet_id": packet.id, "fields": list(packet.proposed_changes.keys())})
    return packet


def _image_entries(images: list[dict], current: list[dict]) -> list[dict]:
    """What to write for each image: the new alt wherever the image was found (in the page content and/or the media library)."""
    entries = []
    for wanted, now in zip(images, current):
        entries.append(
            {
                "src": wanted["src"],
                "content_alt": wanted["alt"] if now["content_alt"] is not None else None,
                "media_id": now["media_id"],
                "media_alt": wanted["alt"] if now["media_id"] is not None else None,
            }
        )
    return entries


def _images_not_saved(images: list[dict], after: list[dict]) -> list[str]:
    """Images whose alt text is not as proposed. An image found nowhere counts as not saved."""
    missed = []
    for wanted, now in zip(images, after):
        places = [value for value in (now["content_alt"], now["media_alt"]) if value is not None]
        if not places or any(value.strip() != wanted["alt"].strip() for value in places):
            missed.append(wanted["src"].rsplit("/", 1)[-1])
    return missed


def proposed_value_missing(proposed: dict, snapshot: dict, key: str) -> bool:
    wanted = proposed.get(key)
    return bool(wanted) and (snapshot.get(key) or "").strip() != wanted.strip()


def rollback_change_packet(packet: ChangePacket) -> ChangePacket:
    """
    Only rolls back if the live page still matches what we applied — if it's
    been edited again since (by the customer, their developer, anything), we
    refuse rather than silently clobber a newer change.
    """
    if packet.status != ChangePacket.Status.APPLIED or not packet.before_snapshot or not packet.after_snapshot:
        raise WordPressIntegrationError("This change isn't ready for rollback.")

    connection = require_connection(packet.business)
    username, app_password = _credentials(connection)

    try:
        current = wp.fetch_object(connection.api_url, packet.target_object_type, packet.target_object_id, username, app_password)
    except wp.WordPressError as exc:
        raise WordPressIntegrationError(str(exc)) from exc

    extra = [key for key in ("canonical_url", "robots") if key in packet.proposed_changes]
    image_srcs = [image["src"] for image in packet.proposed_changes.get("image_alt") or []]
    try:
        live_images = wp.read_image_alts(connection.api_url, packet.target_object_type, packet.target_object_id, image_srcs, username, app_password) if image_srcs else None
    except wp.WordPressError as exc:
        raise WordPressIntegrationError(str(exc)) from exc
    live = _snapshot(current, extra)
    if live_images is not None:
        live["image_alt"] = live_images
    if live != packet.after_snapshot:
        packet.status = ChangePacket.Status.ROLLBACK_BLOCKED
        packet.save(update_fields=["status", "updated_at"])
        record_event(packet.business, "wordpress.rollback_blocked", "A rollback was blocked — the page has changed again since it was published.", metadata={"packet_id": packet.id})
        raise WordPressIntegrationError("This page has changed again since the update — it needs manual review before it can be undone.")

    restore = {k: packet.before_snapshot.get(k) for k in packet.proposed_changes if k in packet.before_snapshot and k != "image_alt"}
    if restore.get("robots") == "unknown":
        del restore["robots"]  # the plugin never told us the old value, so there is nothing to put back
    try:
        if restore:
            wp.update_object(connection.api_url, packet.target_object_type, packet.target_object_id, restore, username, app_password)
        if image_srcs:
            wp.write_image_alts(connection.api_url, packet.target_object_type, packet.target_object_id, packet.before_snapshot["image_alt"], username, app_password)
    except wp.WordPressError as exc:
        raise WordPressIntegrationError(str(exc)) from exc

    packet.status = ChangePacket.Status.ROLLED_BACK
    packet.rolled_back_at = timezone.now()
    packet.save(update_fields=["status", "rolled_back_at", "updated_at"])
    record_event(packet.business, "wordpress.change_rolled_back", "Rolled back a published WordPress change.", metadata={"packet_id": packet.id})
    return packet


def _fail(packet: ChangePacket, message: str) -> None:
    packet.status = ChangePacket.Status.FAILED
    packet.last_error = message
    packet.save(update_fields=["status", "last_error", "updated_at"])
    record_event(packet.business, "wordpress.change_failed", f"A WordPress change failed: {message}", metadata={"packet_id": packet.id})


def _snapshot(obj: wp.WpObject, extra: tuple | list = ()) -> dict:
    """Title and meta description always; canonical_url and robots only when a change touches them."""
    meta = obj.meta
    # On AIOSEO sites the title we manage is the SEO title, not the WordPress page title.
    title = meta["_aioseo_title"] if "_aioseo_title" in meta else obj.title
    snapshot = {"title": title, "meta_description": meta.get("_aioseo_description") or meta.get("rank_math_description") or meta.get("_yoast_wpseo_metadesc") or ""}
    if "canonical_url" in extra:
        snapshot["canonical_url"] = meta.get("_aioseo_canonical") or meta.get("rank_math_canonical_url") or meta.get("_yoast_wpseo_canonical") or ""
    if "robots" in extra:
        snapshot["robots"] = _robots_state(meta)
    return snapshot


def _robots_state(meta: dict) -> str:
    """"index", "noindex", "default" (the site-wide setting applies) or "unknown" (the plugin doesn't expose it)."""
    if "_aioseo_robots" in meta:
        return meta["_aioseo_robots"]
    rank_math = meta.get("rank_math_robots")
    if isinstance(rank_math, list) and rank_math:
        return "noindex" if "noindex" in rank_math else "index"
    yoast = str(meta.get("_yoast_wpseo_meta-robots-noindex") or "")
    return {"1": "noindex", "2": "index"}.get(yoast, "unknown")


def _serialize_wp_object(obj: wp.WpObject, object_type: str) -> dict:
    return {
        "id": obj.id,
        "object_type": object_type,
        "title": obj.title,
        "slug": obj.slug,
        "status": obj.status,
        "url": obj.link,
    }


def serialize_packet(packet: ChangePacket) -> dict:
    return {
        "id": packet.id,
        "target_object_type": packet.target_object_type,
        "target_object_id": packet.target_object_id,
        "target_url": packet.target_url,
        "proposed_changes": packet.proposed_changes,
        "reason": packet.reason,
        "source": packet.source,
        "agent_name": packet.agent_name,
        "status": packet.status,
        "before_snapshot": packet.before_snapshot,
        "after_snapshot": packet.after_snapshot,
        "last_error": packet.last_error,
        "created_at": packet.created_at.isoformat(),
        "approved_at": packet.approved_at.isoformat() if packet.approved_at else None,
        "applied_at": packet.applied_at.isoformat() if packet.applied_at else None,
        "rolled_back_at": packet.rolled_back_at.isoformat() if packet.rolled_back_at else None,
    }
