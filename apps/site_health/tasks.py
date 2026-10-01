import logging

from celery import shared_task

logger = logging.getLogger("django")


@shared_task
def crawl_business_site(crawl_id: int) -> None:
    from .services import execute_crawl

    execute_crawl(crawl_id)
