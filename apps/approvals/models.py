from django.conf import settings
from django.db import models

from apps.businesses.models import Business


class Approval(models.Model):
    """A decision the owner records about one issue type found by a completed crawl."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        RESOLVED = "resolved", "No longer found"

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="approvals")
    source_key = models.CharField(max_length=200)
    title = models.CharField(max_length=200)
    category = models.CharField(max_length=40)
    severity = models.CharField(max_length=20)
    detail = models.TextField()
    affected_urls = models.JSONField(default=list)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "approvals_approval"
        ordering = ["-created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["business", "source_key"], name="unique_business_approval_source"),
        ]
