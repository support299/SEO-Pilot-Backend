from django.conf import settings
from django.db import models

from apps.businesses.models import Business


class WordPressConnection(models.Model):
    class Status(models.TextChoices):
        CONNECTED = "connected", "Connected"
        ERROR = "error", "Error"
        REVOKED = "revoked", "Revoked"

    business = models.OneToOneField(Business, on_delete=models.CASCADE, related_name="wordpress_connection")
    website_url = models.URLField(max_length=500)
    api_url = models.URLField(max_length=500)
    username = models.CharField(max_length=200)
    application_password_enc = models.TextField()
    wp_user_id = models.CharField(max_length=32, blank=True)
    wp_user_name = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.CONNECTED)
    connected_at = models.DateTimeField(auto_now_add=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wordpress_connection"

    def __str__(self) -> str:
        return f"{self.business_id} -> {self.website_url}"


class ChangePacket(models.Model):
    """
    A single proposed website change, carried safely from "idea" to "live" (or
    discarded) without ever touching the live site until explicitly approved.
    Mirrors the old Next.js app's WordPressChangePacket — same state machine,
    same staleness/rollback safety checks, ported rather than redesigned.
    """

    class Status(models.TextChoices):
        AWAITING_APPROVAL = "awaiting_approval", "Awaiting approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        APPLYING = "applying", "Applying"
        APPLIED = "applied", "Applied"
        FAILED = "failed", "Failed"
        STALE = "stale", "Stale"  # the live page changed after this was prepared — must be re-reviewed, never silently overwritten
        ROLLED_BACK = "rolled_back", "Rolled back"
        ROLLBACK_BLOCKED = "rollback_blocked", "Rollback needs manual review"  # the live page changed again after we applied — don't blindly stomp a newer edit

    class ObjectType(models.TextChoices):
        PAGE = "page", "Page"
        POST = "post", "Post"

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="wordpress_change_packets")
    target_object_type = models.CharField(max_length=10, choices=ObjectType.choices)
    target_object_id = models.PositiveIntegerField()
    target_url = models.URLField(max_length=1000, blank=True)

    # What's being proposed — may include "title" and/or "meta_description".
    proposed_changes = models.JSONField()
    reason = models.TextField(blank=True)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.AWAITING_APPROVAL)

    # The live object's modified_gmt at prepare time — if it's changed by apply
    # time, someone/something else touched the page since this was proposed.
    current_revision = models.CharField(max_length=60, blank=True)
    before_snapshot = models.JSONField(null=True, blank=True)
    after_snapshot = models.JSONField(null=True, blank=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    rolled_back_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wordpress_change_packet"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["business", "status"])]

    def __str__(self) -> str:
        return f"{self.get_target_object_type_display()} #{self.target_object_id} ({self.status})"
