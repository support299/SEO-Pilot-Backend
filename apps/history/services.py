import logging

from apps.businesses.models import Business
from apps.site_health.models import CrawlRun

from .models import Event

logger = logging.getLogger("django")

SCORE_TREND_LIMIT = 20


def record_event(business: Business, kind: str, summary: str, *, actor=None, metadata: dict | None = None) -> None:
    """Best effort: a failure to write history must never break the action being recorded."""
    try:
        Event.objects.create(business=business, kind=kind, summary=summary[:500], actor=actor, metadata=metadata or {})
    except Exception:
        logger.exception("Could not record history event %s for business %s", kind, business.id)


def serialize_event(event: Event) -> dict:
    return {
        "id": event.id,
        "kind": event.kind,
        "summary": event.summary,
        "metadata": event.metadata,
        "actor": event.actor.email if event.actor else None,
        "occurred_at": event.occurred_at.isoformat(),
    }


def score_trend(business: Business) -> list[dict]:
    crawls = CrawlRun.objects.filter(business=business, status=CrawlRun.Status.COMPLETED, score__isnull=False).order_by("-finished_at", "-id")[:SCORE_TREND_LIMIT]
    return [{"finished_at": (crawl.finished_at or crawl.created_at).isoformat(), "score": crawl.score} for crawl in reversed(list(crawls))]


def crawl_event_fields(crawl: CrawlRun, findings_count: int | None = None) -> tuple[str, str, dict]:
    if crawl.status == CrawlRun.Status.COMPLETED:
        parts = [f"score {crawl.score}", f"{crawl.pages_crawled} pages"]
        metadata = {"score": crawl.score, "pages_crawled": crawl.pages_crawled}
        if findings_count is not None:
            parts.append(f"{findings_count} findings")
            metadata["findings"] = findings_count
        return "crawl.completed", f"Site crawl completed: {', '.join(parts)}.", metadata
    return "crawl.failed", f"Site crawl failed: {crawl.error or 'unknown error'}", {}
