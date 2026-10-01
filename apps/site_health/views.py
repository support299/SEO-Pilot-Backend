import logging

from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from common.permissions import get_business_or_404

from .models import CrawlRun
from .services import latest_report
from .tasks import crawl_business_site

logger = logging.getLogger("django")


class SiteHealthView(APIView):
    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        return Response(latest_report(business))


class StartCrawlView(APIView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "sync"

    def post(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        if not business.website:
            return Response({"detail": "Add a website for this business before crawling."}, status=status.HTTP_400_BAD_REQUEST)

        active = CrawlRun.objects.filter(business=business, status__in=[CrawlRun.Status.QUEUED, CrawlRun.Status.RUNNING]).exists()
        if active:
            return Response({"detail": "A crawl is already running for this business."}, status=status.HTTP_409_CONFLICT)

        crawl = CrawlRun.objects.create(business=business, seed_url=business.website, status=CrawlRun.Status.QUEUED)
        try:
            crawl_business_site.delay(crawl.id)
        except Exception:
            logger.exception("Could not enqueue site crawl for business %s", business.id)
            crawl.status = CrawlRun.Status.FAILED
            crawl.error = "The crawl queue is unavailable. Start Redis and the Celery worker, then try again."
            crawl.finished_at = timezone.now()
            crawl.save(update_fields=["status", "error", "finished_at"])
            return Response({"detail": crawl.error}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response({"detail": "Crawl started.", "id": crawl.id}, status=status.HTTP_202_ACCEPTED)
