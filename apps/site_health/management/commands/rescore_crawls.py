"""
Recompute the crawl score of every completed crawl with the current formula.
Run once after the scoring rule changes so old crawls match new ones.
"""

from django.core.management.base import BaseCommand

from apps.site_health.crawl import CrawlFinding, score_findings
from apps.site_health.models import CrawlRun


class Command(BaseCommand):
    help = "Recompute crawl scores with the current formula."

    def handle(self, *args, **options):
        changed = 0
        for crawl in CrawlRun.objects.filter(status=CrawlRun.Status.COMPLETED).prefetch_related("findings"):
            findings = [CrawlFinding(url=f.url, category=f.category, severity=f.severity, title=f.title, detail=f.detail) for f in crawl.findings.all()]
            score = score_findings(findings, crawl.pages_crawled)
            if score != crawl.score:
                self.stdout.write(f"crawl {crawl.id} ({crawl.business_id}): {crawl.score} -> {score}")
                crawl.score = score
                crawl.save(update_fields=["score"])
                changed += 1
        self.stdout.write(f"{changed} crawl(s) re-scored.")
