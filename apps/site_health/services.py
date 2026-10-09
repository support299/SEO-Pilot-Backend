import logging

from django.utils import timezone

from apps.businesses.models import Business
from apps.history.services import crawl_event_fields, record_event

from .crawl import CrawlFinding, SiteCrawler, build_findings, category_summaries, score_findings
from .models import CrawledPage, CrawlRun, Finding

logger = logging.getLogger("django")


def latest_report(business: Business) -> dict:
    crawl = CrawlRun.objects.filter(business=business).first()
    if crawl is None:
        return {
            "status": "none",
            "score": None,
            "pages_crawled": 0,
            "critical_issues": 0,
            "error": None,
            "seed_url": None,
            "finished_at": None,
            "categories": [],
            "findings": [],
            "pages": [],
        }
    return serialize_crawl(crawl)


def serialize_crawl(crawl: CrawlRun) -> dict:
    findings = list(crawl.findings.all())
    pages = list(crawl.pages.all()) if crawl.status == CrawlRun.Status.COMPLETED else []
    categories = []
    if crawl.status == CrawlRun.Status.COMPLETED:
        fetched = [
            _page_as_fetched(page)
            for page in pages
        ]
        categories = category_summaries(fetched, [_finding_as_crawl(finding) for finding in findings])
    return {
        "status": crawl.status,
        "score": crawl.score,
        "pages_crawled": crawl.pages_crawled,
        "critical_issues": crawl.critical_issues,
        "error": crawl.error,
        "seed_url": crawl.seed_url,
        "finished_at": crawl.finished_at.isoformat() if crawl.finished_at else None,
        "categories": categories,
        "findings": [
            {"url": finding.url, "category": finding.category, "severity": finding.severity, "title": finding.title, "detail": finding.detail}
            for finding in findings
        ],
        "pages": [
            {
                "url": page.url,
                "status_code": page.status_code,
                "title": page.title,
                "meta_description": page.meta_description,
                "noindex": page.noindex,
            }
            for page in pages
        ],
    }


def execute_crawl(crawl_id: int) -> None:
    crawl = CrawlRun.objects.filter(id=crawl_id).first()
    if crawl is None:
        return

    crawl.status = CrawlRun.Status.RUNNING
    crawl.started_at = timezone.now()
    crawl.error = None
    crawl.save(update_fields=["status", "started_at", "error"])

    try:
        pages = SiteCrawler(crawl.seed_url).crawl()
        findings = build_findings(pages)
        CrawledPage.objects.filter(crawl=crawl).delete()
        Finding.objects.filter(crawl=crawl).delete()
        CrawledPage.objects.bulk_create(
            [
                CrawledPage(
                    crawl=crawl,
                    url=page.url,
                    status_code=page.status_code,
                    content_type=page.content_type[:200],
                    title=page.title[:500],
                    meta_description=page.meta_description,
                    canonical=page.canonical[:1000],
                    noindex=page.noindex,
                    error=page.error,
                )
                for page in pages
            ]
        )
        Finding.objects.bulk_create(
            [
                Finding(crawl=crawl, url=finding.url[:1000], category=finding.category, severity=finding.severity, title=finding.title, detail=finding.detail)
                for finding in findings
            ]
        )
        crawl.status = CrawlRun.Status.COMPLETED
        crawl.score = score_findings(findings)
        crawl.pages_crawled = len(pages)
        crawl.critical_issues = sum(1 for finding in findings if finding.severity == "critical")
        crawl.finished_at = timezone.now()
        crawl.save(update_fields=["status", "score", "pages_crawled", "critical_issues", "finished_at"])
        kind, summary, metadata = crawl_event_fields(crawl, len(findings))
        record_event(crawl.business, kind, summary, metadata=metadata)
    except Exception as exc:
        logger.exception("Site crawl failed for crawl %s", crawl_id)
        crawl.status = CrawlRun.Status.FAILED
        crawl.error = str(exc)
        crawl.finished_at = timezone.now()
        crawl.save(update_fields=["status", "error", "finished_at"])
        kind, summary, metadata = crawl_event_fields(crawl)
        record_event(crawl.business, kind, summary, metadata=metadata)


def _page_as_fetched(page: CrawledPage):
    from .crawl import FetchedPage

    is_html = "html" in page.content_type.lower() or bool(page.title or page.meta_description)
    return FetchedPage(
        url=page.url,
        status_code=page.status_code,
        content_type=page.content_type,
        title=page.title,
        meta_description=page.meta_description,
        canonical=page.canonical,
        noindex=page.noindex,
        is_html=is_html and page.status_code < 400 and not page.error,
        error=page.error,
        links=[],
    )


def _finding_as_crawl(finding: Finding) -> CrawlFinding:
    return CrawlFinding(url=finding.url, category=finding.category, severity=finding.severity, title=finding.title, detail=finding.detail)
