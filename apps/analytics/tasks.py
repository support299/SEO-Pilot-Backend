import logging

from celery import shared_task

logger = logging.getLogger("django")


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_business_analytics(self, business_id: int) -> None:
    from .models import AnalyticsConnection
    from .services import sync_analytics

    connection = AnalyticsConnection.objects.filter(business_id=business_id).first()
    if not connection:
        logger.warning("sync_business_analytics: no connection for business %s", business_id)
        return

    sync_analytics(connection)


@shared_task
def sync_all_analytics_connections() -> None:
    from .models import AnalyticsConnection

    for business_id in AnalyticsConnection.objects.values_list("business_id", flat=True):
        sync_business_analytics.delay(business_id)
