from django.db import models

from apps.businesses.models import Business


class CrawlRun(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="crawl_runs")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    seed_url = models.URLField(max_length=1000)
    score = models.PositiveSmallIntegerField(null=True, blank=True)
    pages_crawled = models.PositiveIntegerField(default=0)
    critical_issues = models.PositiveIntegerField(default=0)
    error = models.TextField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "site_health_crawl_run"
        ordering = ["-created_at"]


class CrawledPage(models.Model):
    crawl = models.ForeignKey(CrawlRun, on_delete=models.CASCADE, related_name="pages")
    url = models.URLField(max_length=1000)
    status_code = models.PositiveIntegerField(default=0)
    content_type = models.CharField(max_length=200, blank=True)
    title = models.CharField(max_length=500, blank=True)
    meta_description = models.TextField(blank=True)
    canonical = models.CharField(max_length=1000, blank=True)
    noindex = models.BooleanField(default=False)
    error = models.TextField(blank=True)

    class Meta:
        db_table = "site_health_crawled_page"


class Finding(models.Model):
    class Severity(models.TextChoices):
        CRITICAL = "critical", "Critical"
        HIGH = "high", "High"
        MEDIUM = "medium", "Medium"

    crawl = models.ForeignKey(CrawlRun, on_delete=models.CASCADE, related_name="findings")
    url = models.URLField(max_length=1000, blank=True)
    category = models.CharField(max_length=40)
    severity = models.CharField(max_length=20, choices=Severity.choices)
    title = models.CharField(max_length=200)
    detail = models.TextField()

    class Meta:
        db_table = "site_health_finding"
