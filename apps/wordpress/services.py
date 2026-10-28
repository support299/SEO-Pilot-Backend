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
        },
    )
    record_event(business, "wordpress.connected", f"Connected WordPress ({website_url}).", metadata={"website_url": website_url})
    return connection


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


def prepare_change_packet(business: Business, *, user, target_object_type: str, target_object_id: int, proposed_changes: dict, reason: str = "", source: str = "manual", agent_name: str = "") -> ChangePacket:
    if not proposed_changes.get("title") and not proposed_changes.get("meta_description"):
        raise WordPressIntegrationError("Propose at least a title or a meta description change.")

    connection = require_connection(business)
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
    packet.before_snapshot = _snapshot(before)
    packet.save(update_fields=["status", "before_snapshot", "updated_at"])

    try:
        wp.update_object(connection.api_url, packet.target_object_type, packet.target_object_id, packet.proposed_changes, username, app_password)
        after = wp.fetch_object(connection.api_url, packet.target_object_type, packet.target_object_id, username, app_password)
    except wp.WordPressError as exc:
        _fail(packet, str(exc))
        raise WordPressIntegrationError(str(exc)) from exc

    after_snapshot = _snapshot(after)
    not_saved = [
        label
        for key, label in (("title", "title"), ("meta_description", "meta description"))
        if proposed_value_missing(packet.proposed_changes, after_snapshot, key)
    ]
    if not_saved:
        message = f"WordPress accepted the update but the {' and '.join(not_saved)} did not change on the page. The site needs a supported SEO plugin (All in One SEO, Yoast or Rank Math) that allows editing it from outside."
        _fail(packet, message)
        raise WordPressIntegrationError(message)

    packet.status = ChangePacket.Status.APPLIED
    packet.after_snapshot = after_snapshot
    packet.applied_at = timezone.now()
    packet.last_error = ""
    packet.save(update_fields=["status", "after_snapshot", "applied_at", "last_error", "updated_at"])
    record_event(packet.business, "wordpress.change_applied", f"Published a change to \"{after.title}\".", metadata={"packet_id": packet.id, "fields": list(packet.proposed_changes.keys())})
    return packet


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

    if _snapshot(current) != packet.after_snapshot:
        packet.status = ChangePacket.Status.ROLLBACK_BLOCKED
        packet.save(update_fields=["status", "updated_at"])
        record_event(packet.business, "wordpress.rollback_blocked", "A rollback was blocked — the page has changed again since it was published.", metadata={"packet_id": packet.id})
        raise WordPressIntegrationError("This page has changed again since the update — it needs manual review before it can be undone.")

    restore = {k: packet.before_snapshot.get(k) for k in packet.proposed_changes if k in packet.before_snapshot}
    try:
        wp.update_object(connection.api_url, packet.target_object_type, packet.target_object_id, restore, username, app_password)
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


def _snapshot(obj: wp.WpObject) -> dict:
    # On AIOSEO sites the title we manage is the SEO title, not the WordPress page title.
    title = obj.meta["_aioseo_title"] if "_aioseo_title" in obj.meta else obj.title
    return {"title": title, "meta_description": obj.meta.get("_aioseo_description") or obj.meta.get("rank_math_description") or obj.meta.get("_yoast_wpseo_metadesc") or ""}


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
