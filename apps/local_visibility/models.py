from django.db import models

from apps.businesses.models import Business


class LocalVisibilityProfile(models.Model):
    """Where and how a business's Google Maps rank is measured: the centre of the grid and its size."""

    business = models.OneToOneField(Business, on_delete=models.CASCADE, related_name="local_visibility_profile")
    latitude = models.FloatField()
    longitude = models.FloatField()
    grid_size = models.PositiveSmallIntegerField(default=5)  # grid_size x grid_size points
    radius_km = models.FloatField(default=5)  # centre to the edge of the grid
    # Used to spot the business in Google Maps results. Defaults to the business website's domain.
    business_domain = models.CharField(max_length=255, blank=True)
    place_id = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "local_visibility_profile"

    @property
    def radius_miles(self) -> float:
        return round(self.radius_km / 1.609344, 2)


class TrackedKeyword(models.Model):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="tracked_keywords")
    keyword = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "local_visibility_keyword"
        ordering = ["keyword"]
        constraints = [models.UniqueConstraint(fields=["business", "keyword"], name="unique_keyword_per_business")]


class GridScan(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    keyword = models.ForeignKey(TrackedKeyword, on_delete=models.CASCADE, related_name="scans")
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="grid_scans")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    grid_size = models.PositiveSmallIntegerField()
    radius_km = models.FloatField()
    center_latitude = models.FloatField()
    center_longitude = models.FloatField()
    average_rank = models.FloatField(null=True, blank=True)  # over the points where the business ranks
    visibility_score = models.FloatField(null=True, blank=True)  # 0-100
    top3_coverage = models.FloatField(null=True, blank=True)  # % of points ranking 1-3
    top10_coverage = models.FloatField(null=True, blank=True)  # % of points ranking 1-10
    ranked_points = models.PositiveSmallIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=8, decimal_places=4, default=0)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "local_visibility_scan"
        ordering = ["-created_at"]

    @property
    def radius_miles(self) -> float:
        return round(self.radius_km / 1.609344, 2)


class GridPoint(models.Model):
    scan = models.ForeignKey(GridScan, on_delete=models.CASCADE, related_name="points")
    row = models.PositiveSmallIntegerField()
    col = models.PositiveSmallIntegerField()
    latitude = models.FloatField()
    longitude = models.FloatField()
    rank = models.PositiveSmallIntegerField(null=True, blank=True)  # None = not in the results we looked at
    failed = models.BooleanField(default=False)  # the check itself failed; rank is unknown, not "not ranking"
    top_results = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "local_visibility_point"
        ordering = ["row", "col"]
