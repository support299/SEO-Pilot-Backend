from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.businesses.models import Business


class Event(models.Model):
    """One thing that actually happened for a business. Append-only: rows are never edited."""

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="history_events")
    kind = models.CharField(max_length=60)
    summary = models.CharField(max_length=500)
    metadata = models.JSONField(default=dict, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "history_event"
        ordering = ["-occurred_at", "-id"]
        indexes = [models.Index(fields=["business", "-occurred_at"])]
