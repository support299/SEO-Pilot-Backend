from django.db import models

from apps.businesses.models import Business


class SearchConsoleConnection(models.Model):
    business = models.OneToOneField(Business, on_delete=models.CASCADE, related_name="search_console_connection")
    site_url = models.CharField(max_length=500)
    access_token_enc = models.TextField()
    refresh_token_enc = models.TextField()
    token_expires_at = models.DateTimeField()
    scope = models.CharField(max_length=300)
    connected_at = models.DateTimeField(auto_now_add=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_sync_error = models.TextField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "search_console_connection"

    def __str__(self) -> str:
        return f"{self.business_id} -> {self.site_url}"


class SearchConsoleDailyMetric(models.Model):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="search_console_daily_metrics")
    date = models.DateField()
    clicks = models.PositiveIntegerField(default=0)
    impressions = models.PositiveIntegerField(default=0)
    ctr = models.FloatField(default=0)
    position = models.FloatField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "search_console_daily_metric"
        constraints = [models.UniqueConstraint(fields=["business", "date"], name="unique_business_date_metric")]
        indexes = [models.Index(fields=["business", "-date"])]
        ordering = ["date"]


class SearchConsoleTopQuery(models.Model):
    """
    A snapshot of the top queries as of the last sync — replaced wholesale on
    each sync rather than kept as daily history. Storing per-query-per-day
    history would be a large, mostly-unused table for what the product
    actually needs (a current "what's working" view, not a historical query
    trend), so this is a deliberate simplification, not an oversight.
    """

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="search_console_top_queries")
    query = models.CharField(max_length=500)
    clicks = models.PositiveIntegerField(default=0)
    impressions = models.PositiveIntegerField(default=0)
    ctr = models.FloatField(default=0)
    position = models.FloatField(default=0)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "search_console_top_query"
        ordering = ["-clicks"]


class SearchConsoleTopPage(models.Model):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="search_console_top_pages")
    page = models.CharField(max_length=1000)
    clicks = models.PositiveIntegerField(default=0)
    impressions = models.PositiveIntegerField(default=0)
    ctr = models.FloatField(default=0)
    position = models.FloatField(default=0)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "search_console_top_page"
        ordering = ["-clicks"]
