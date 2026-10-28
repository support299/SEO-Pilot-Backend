import logging

from celery import shared_task
from django.conf import settings

logger = logging.getLogger("django")


@shared_task
def run_grid_scan(scan_id: int) -> None:
    from .services import execute_scan

    execute_scan(scan_id)


@shared_task
def scan_all_tracked_keywords() -> None:
    """Weekly refresh. Off unless LOCAL_VISIBILITY_AUTO_SCAN is set, because every scan costs money."""
    if not settings.LOCAL_VISIBILITY_AUTO_SCAN:
        return
    from .services import scan_all_businesses

    logger.info("Weekly local visibility refresh started %s scans", scan_all_businesses())
