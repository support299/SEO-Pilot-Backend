import logging

from celery import shared_task

logger = logging.getLogger("django")


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_business_search_console(self, business_id: int) -> None:
    """Runs a Search Console sync for one business — enqueued from the OAuth callback (first sync) and the manual "Sync now" button, and by sync_all_search_console_connections for the daily scheduled run."""
    from .models import SearchConsoleConnection
    from .services import sync_search_console

    connection = SearchConsoleConnection.objects.filter(business_id=business_id).first()
    if not connection:
        logger.warning("sync_business_search_console: no connection for business %s", business_id)
        return

    sync_search_console(connection)


@shared_task
def sync_all_search_console_connections() -> None:
    """Scheduled (Celery Beat) daily sync for every connected business — see config/settings/base.py CELERY_BEAT_SCHEDULE."""
    from .models import SearchConsoleConnection

    for connection_id in SearchConsoleConnection.objects.values_list("business_id", flat=True):
        sync_business_search_console.delay(connection_id)
