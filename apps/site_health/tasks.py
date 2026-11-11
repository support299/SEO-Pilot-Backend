import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger("django")


@shared_task
def crawl_business_site(crawl_id: int) -> None:
    from .services import execute_crawl

    execute_crawl(crawl_id)


@shared_task
def crawl_all_business_sites() -> None:
    """Scheduled (Celery Beat) nightly crawl of every active website, so fresh data is ready before the daily AI audit. Skips paused sites, sites without a URL, and sites already crawling."""
    from apps.businesses.models import Business

    from .models import CrawlRun

    busy = set(CrawlRun.objects.filter(status__in=[CrawlRun.Status.QUEUED, CrawlRun.Status.RUNNING]).values_list("business_id", flat=True))
    for business in Business.objects.filter(is_paused=False, account__is_paused=False).exclude(website="").select_related("account"):
        if business.id in busy:
            continue
        crawl = CrawlRun.objects.create(business=business, seed_url=business.website, status=CrawlRun.Status.QUEUED)
        try:
            crawl_business_site.delay(crawl.id)
        except Exception:
            logger.exception("Could not enqueue nightly crawl for business %s", business.id)
            crawl.status = CrawlRun.Status.FAILED
            crawl.error = "The crawl queue is unavailable."
            crawl.finished_at = timezone.now()
            crawl.save(update_fields=["status", "error", "finished_at"])
