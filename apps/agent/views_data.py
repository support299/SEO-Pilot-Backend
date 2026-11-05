"""
Read access for agents to the data the app already shows people: every crawled page, Search Console trends, Google
Analytics, the work history and connection freshness. Plus starting a crawl or a data sync.

The read views reuse the app's own views (same queries, same output) under the agent key, so the two never drift.
Local-visibility scans stay human-only: each one costs money.
"""

from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from apps.analytics import views as analytics_views
from apps.analytics.models import AnalyticsConnection
from apps.history import views as history_views
from apps.history.services import record_event
from apps.local_visibility.models import GridScan
from apps.search_console import views as sc_views
from apps.search_console.models import SearchConsoleConnection, SearchConsoleTopPage, SearchConsoleTopQuery
from apps.search_console.serializers import TopPageSerializer, TopQuerySerializer
from apps.site_health import views as site_health_views
from apps.site_health.models import CrawlRun
from apps.wordpress.models import WordPressConnection
from common.permissions import get_business_or_404

from .views import AgentView

MAX_TOP_ROWS = 500


def _age_hours(moment) -> float | None:
    return round((timezone.now() - moment).total_seconds() / 3600, 1) if moment else None


def _limit(request, default: int) -> int:
    try:
        return max(1, min(int(request.query_params.get("limit", default)), MAX_TOP_ROWS))
    except (TypeError, ValueError):
        return default


# --- crawled pages ----------------------------------------------------------------------------------------------

class AgentPagesView(AgentView):
    """
    GET — every page from the latest completed crawl: URL, HTTP status, title, meta description, canonical, noindex,
    plus the crawl findings for that URL. Works for any website, with or without WordPress.
    """

    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        crawl = CrawlRun.objects.filter(business=business, status=CrawlRun.Status.COMPLETED).order_by("-finished_at", "-id").first()
        latest = CrawlRun.objects.filter(business=business).first()
        if crawl is None:
            return Response({"crawl": None, "latest_crawl_status": latest.status if latest else "none", "pages": []})
        findings = defaultdict(list)
        for finding in crawl.findings.all():
            findings[finding.url].append({"category": finding.category, "severity": finding.severity, "title": finding.title, "detail": finding.detail})
        pages = [
            {
                "url": page.url,
                "status_code": page.status_code,
                "content_type": page.content_type,
                "title": page.title,
                "meta_description": page.meta_description,
                "canonical": page.canonical,
                "noindex": page.noindex,
                "error": page.error,
                "findings": findings.get(page.url, []),
            }
            for page in crawl.pages.all()
        ]
        return Response(
            {
                "crawl": {"id": crawl.id, "seed_url": crawl.seed_url, "score": crawl.score, "pages_crawled": crawl.pages_crawled, "finished_at": crawl.finished_at.isoformat() if crawl.finished_at else None, "age_hours": _age_hours(crawl.finished_at)},
                "latest_crawl_status": latest.status if latest else "none",  # a newer crawl may be queued or running
                "site_wide_findings": findings.get("", []),
                "pages": pages,
            }
        )


class AgentStartCrawlView(AgentView, site_health_views.StartCrawlView):
    """POST — start a fresh crawl (free). Capped per website per day; the history says which agent asked."""

    def post(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        cap = settings.AGENT_MAX_CRAWLS_PER_BUSINESS_PER_DAY
        if CrawlRun.objects.filter(business=business, created_at__gte=timezone.now() - timedelta(days=1)).count() >= cap:
            return Response({"detail": f"At most {cap} crawls per website per day."}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        response = super().post(request, business_id)
        if response.status_code == status.HTTP_202_ACCEPTED:
            record_event(business, "site_health.crawl_requested", f"Crawl requested (by {request.auth.name}).", metadata={"agent": request.auth.name, "crawl": response.data["id"]})
        return response


# --- Search Console and Analytics (the app's own views, agent-authenticated) -----------------------------------

class AgentSearchConsoleOverviewView(AgentView, sc_views.OverviewView):
    """
    GET ?range=7|28|90 (days) — daily clicks, impressions, CTR and position, with the previous-period comparison.
    Search Console data lags ~2-3 days. `connected: false` means "no data source", never "zero clicks".
    """

    def get(self, request, business_id):
        response = super().get(request, business_id)
        response.data = {"connected": SearchConsoleConnection.objects.filter(business_id=business_id).exists(), **response.data}
        return response


class AgentSearchConsoleQueriesView(AgentView):
    """GET ?limit=N — every stored top query (not just the 20 in `context/`)."""

    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        return Response(TopQuerySerializer(SearchConsoleTopQuery.objects.filter(business=business)[: _limit(request, MAX_TOP_ROWS)], many=True).data)


class AgentSearchConsolePagesView(AgentView):
    """GET ?limit=N — every stored top page by search performance."""

    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        return Response(TopPageSerializer(SearchConsoleTopPage.objects.filter(business=business)[: _limit(request, MAX_TOP_ROWS)], many=True).data)


class AgentSearchConsoleSyncView(AgentView, sc_views.SyncView):
    """POST — refresh Search Console data now instead of waiting for the daily sync."""


class AgentAnalyticsOverviewView(AgentView, analytics_views.OverviewView):
    """GET ?range=7|28|90 (days) — organic sessions, active users and conversions (when measurable) per day. Check `connected`."""

    def get(self, request, business_id):
        response = super().get(request, business_id)
        response.data = {"connected": AnalyticsConnection.objects.filter(business_id=business_id).exists(), **response.data}
        return response


class AgentAnalyticsSyncView(AgentView, analytics_views.SyncView):
    """POST — refresh Google Analytics data now."""


# --- history and freshness --------------------------------------------------------------------------------------

class AgentHistoryView(AgentView, history_views.HistoryView):
    """GET ?page=N — everything done on this website, newest first (people and agents), plus the crawl score trend."""


class AgentConnectionsView(AgentView):
    """
    GET — for every data source: connected or not, when it last refreshed and how old that is (`age_hours`), and the
    last error. Check this before relying on data: a recent sync is not proof that a change is live.
    """

    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        sc = SearchConsoleConnection.objects.filter(business=business).first()
        ga = AnalyticsConnection.objects.filter(business=business).first()
        wp = WordPressConnection.objects.filter(business=business).first()
        crawl = CrawlRun.objects.filter(business=business).first()
        scan = GridScan.objects.filter(business=business).order_by("-created_at").first()
        return Response(
            {
                "search_console": {"connected": True, "site_url": sc.site_url, "last_synced_at": sc.last_synced_at.isoformat() if sc.last_synced_at else None, "age_hours": _age_hours(sc.last_synced_at), "last_error": sc.last_sync_error} if sc else {"connected": False},
                "analytics": {"connected": True, "property_name": ga.property_name, "last_synced_at": ga.last_synced_at.isoformat() if ga.last_synced_at else None, "age_hours": _age_hours(ga.last_synced_at), "last_error": ga.last_sync_error, "conversions_measurable": bool(ga.conversion_events)} if ga else {"connected": False},
                "wordpress": {"connected": wp.status == "connected", "status": wp.status, "last_verified_at": wp.last_verified_at.isoformat() if wp.last_verified_at else None, "age_hours": _age_hours(wp.last_verified_at), "last_error": wp.last_error} if wp else {"connected": False},
                "crawl": {"status": crawl.status, "finished_at": crawl.finished_at.isoformat() if crawl.finished_at else None, "age_hours": _age_hours(crawl.finished_at), "error": crawl.error} if crawl else {"status": "never_run"},
                "local_visibility": {"last_scan_at": scan.created_at.isoformat(), "age_hours": _age_hours(scan.created_at), "status": scan.status} if scan else {"last_scan_at": None},
            }
        )
