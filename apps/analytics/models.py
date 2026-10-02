from django.db import models

from apps.businesses.models import Business


class AnalyticsConnection(models.Model):
    business = models.OneToOneField(Business, on_delete=models.CASCADE, related_name="analytics_connection")
    property_id = models.CharField(max_length=32)
    property_name = models.CharField(max_length=300)
    account_id = models.CharField(max_length=32, blank=True)
    website_url = models.URLField(max_length=500, blank=True)
    access_token_enc = models.TextField()
    refresh_token_enc = models.TextField()
    token_expires_at = models.DateTimeField()
    scope = models.CharField(max_length=500)
    conversion_events = models.JSONField(default=list)
    connected_at = models.DateTimeField(auto_now_add=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_sync_error = models.TextField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "analytics_connection"

    def __str__(self) -> str:
        return f"{self.business_id} -> {self.property_id}"


class AnalyticsDailyMetric(models.Model):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="analytics_daily_metrics")
    date = models.DateField()
    sessions = models.PositiveIntegerField(default=0)
    active_users = models.PositiveIntegerField(default=0)
    # Null when this property has no key events. Zero is a real count, not a stand-in for "not configured".
    conversions = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "analytics_daily_metric"
        constraints = [models.UniqueConstraint(fields=["business", "date"], name="unique_analytics_business_date")]
        indexes = [models.Index(fields=["business", "-date"])]
        ordering = ["date"]
