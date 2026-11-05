import json
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.analytics.models import AnalyticsConnection, AnalyticsDailyMetric
from apps.businesses.models import Account, Business
from apps.history.models import Event
from apps.search_console.models import SearchConsoleConnection, SearchConsoleDailyMetric, SearchConsoleTopQuery
from apps.site_health.models import CrawledPage, CrawlRun, Finding

from .models import AgentKey

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def admin_user():
    return User.objects.create_user(email="boss@agency.com", password="SuperSecret123!", is_agency_admin=True)


@pytest.fixture
def key(admin_user):
    return AgentKey.issue(name="codex-nightly", created_by=admin_user)[1]


@pytest.fixture
def business():
    return Business.objects.create(account=Account.objects.create(name="Acme"), name="Acme Gutters", website="https://acme.example")


def A(raw):
    return {"HTTP_AUTHORIZATION": f"Agent {raw}"}


def _secret_free(data) -> None:
    body = json.dumps(data, default=str).lower()
    for word in ("access_token", "refresh_token", "token_enc", "application_password"):
        assert word not in body


class TestPages:
    def test_every_crawled_page_with_its_findings(self, client, business, key):
        crawl = CrawlRun.objects.create(business=business, seed_url="https://acme.example", status=CrawlRun.Status.COMPLETED, score=82, pages_crawled=2, finished_at=timezone.now() - timedelta(hours=5))
        CrawledPage.objects.create(crawl=crawl, url="https://acme.example/", status_code=200, title="Home", meta_description="Gutters", canonical="https://acme.example/")
        CrawledPage.objects.create(crawl=crawl, url="https://acme.example/old", status_code=404)
        Finding.objects.create(crawl=crawl, url="https://acme.example/old", category="technical", severity="critical", title="Page did not load", detail="404")
        data = client.get(reverse("agent:pages", args=[business.id]), **A(key)).data
        assert data["crawl"]["score"] == 82 and data["crawl"]["age_hours"] == pytest.approx(5, abs=0.2)
        pages = {p["url"]: p for p in data["pages"]}
        assert pages["https://acme.example/"]["title"] == "Home" and pages["https://acme.example/"]["findings"] == []
        assert pages["https://acme.example/old"]["findings"][0]["title"] == "Page did not load"

    def test_uses_the_last_completed_crawl_while_a_new_one_runs(self, client, business, key):
        done = CrawlRun.objects.create(business=business, seed_url="https://acme.example", status=CrawlRun.Status.COMPLETED, finished_at=timezone.now())
        CrawledPage.objects.create(crawl=done, url="https://acme.example/", status_code=200)
        CrawlRun.objects.create(business=business, seed_url="https://acme.example", status=CrawlRun.Status.RUNNING)
        data = client.get(reverse("agent:pages", args=[business.id]), **A(key)).data
        assert data["crawl"]["id"] == done.id and data["latest_crawl_status"] == "running" and len(data["pages"]) == 1

    def test_never_crawled(self, client, business, key):
        assert client.get(reverse("agent:pages", args=[business.id]), **A(key)).data == {"crawl": None, "latest_crawl_status": "none", "pages": []}


class TestCrawl:
    def test_agent_starts_a_crawl_and_history_names_it(self, client, business, key, monkeypatch):
        monkeypatch.setattr("apps.site_health.views.crawl_business_site.delay", lambda crawl_id: None)
        response = client.post(reverse("agent:crawl", args=[business.id]), **A(key))
        assert response.status_code == 202
        event = Event.objects.get(kind="site_health.crawl_requested")
        assert event.actor is None and event.metadata["agent"] == "codex-nightly"
        assert client.post(reverse("agent:crawl", args=[business.id]), **A(key)).status_code == 409  # one at a time

    def test_daily_cap(self, client, business, key, monkeypatch, settings):
        settings.AGENT_MAX_CRAWLS_PER_BUSINESS_PER_DAY = 1
        CrawlRun.objects.create(business=business, seed_url="https://acme.example", status=CrawlRun.Status.COMPLETED)
        assert client.post(reverse("agent:crawl", args=[business.id]), **A(key)).status_code == 429


class TestSearchConsoleAndAnalytics:
    @pytest.fixture
    def connected(self, business):
        SearchConsoleConnection.objects.create(business=business, site_url="sc-domain:acme.example", access_token_enc="x", refresh_token_enc="y", token_expires_at=timezone.now(), scope="s", last_synced_at=timezone.now() - timedelta(hours=9))
        AnalyticsConnection.objects.create(business=business, property_id="1", property_name="Acme GA4", access_token_enc="x", refresh_token_enc="y", token_expires_at=timezone.now(), scope="s", last_synced_at=timezone.now() - timedelta(hours=30))
        today = timezone.localdate()
        for i in range(1, 8):
            SearchConsoleDailyMetric.objects.create(business=business, date=today - timedelta(days=i), clicks=10, impressions=100, ctr=0.1, position=8)
            AnalyticsDailyMetric.objects.create(business=business, date=today - timedelta(days=i), sessions=50, active_users=40)
        for i in range(30):
            SearchConsoleTopQuery.objects.create(business=business, query=f"gutter query {i}", clicks=30 - i, impressions=100)

    def test_trends_and_all_queries(self, client, business, key, connected):
        sc = client.get(reverse("agent:sc-overview", args=[business.id]) + "?range=28", **A(key))
        assert sc.status_code == 200 and sc.data["range_days"] == 28 and sc.data["connected"] is True
        assert sc.data["summary"]["clicks"] == 10 * len(sc.data["rows"]) and 0 < len(sc.data["rows"]) <= 7  # newest days excluded by Google's reporting lag
        assert len(client.get(reverse("agent:sc-queries", args=[business.id]), **A(key)).data) == 30  # more than context's 20
        assert len(client.get(reverse("agent:sc-queries", args=[business.id]) + "?limit=5", **A(key)).data) == 5
        ga = client.get(reverse("agent:ga-overview", args=[business.id]) + "?range=28", **A(key))
        assert ga.status_code == 200 and ga.data["summary"]["sessions"] == 50 * len(ga.data["rows"]) and len(ga.data["rows"]) > 0

    def test_connections_report_freshness_and_never_tokens(self, client, business, key, connected):
        data = client.get(reverse("agent:connections", args=[business.id]), **A(key)).data
        assert data["search_console"]["connected"] and data["search_console"]["age_hours"] == pytest.approx(9, abs=0.2)
        assert data["analytics"]["age_hours"] == pytest.approx(30, abs=0.2)
        assert data["wordpress"] == {"connected": False} and data["crawl"] == {"status": "never_run"}
        _secret_free(data)

    def test_not_connected_is_never_reported_as_zero(self, client, business, key):  # AC24
        sc = client.get(reverse("agent:sc-overview", args=[business.id]), **A(key)).data
        ga = client.get(reverse("agent:ga-overview", args=[business.id]), **A(key)).data
        assert sc["connected"] is False and ga["connected"] is False

    def test_sync_needs_a_connection(self, client, business, key):
        assert client.post(reverse("agent:sc-sync", args=[business.id]), **A(key)).status_code == 400
        assert client.post(reverse("agent:ga-sync", args=[business.id]), **A(key)).status_code == 400

    def test_sync_is_queued(self, client, business, key, connected, monkeypatch):
        monkeypatch.setattr("apps.search_console.views.sync_business_search_console.delay", lambda business_id: None)
        assert client.post(reverse("agent:sc-sync", args=[business.id]), **A(key)).status_code == 202


class TestHistory:
    def test_history_shows_what_people_and_agents_did(self, client, business, key, admin_user):
        Event.objects.create(business=business, kind="wordpress.change_applied", summary="Title updated", actor=admin_user)
        Event.objects.create(business=business, kind="checklist.updated", summary="SEO-013 verified (by claude-daily)", metadata={"agent": "claude-daily"})
        data = client.get(reverse("agent:history", args=[business.id]), **A(key)).data
        assert data["count"] == 2 and {e["kind"] for e in data["results"]} == {"wordpress.change_applied", "checklist.updated"}
        assert "score_trend" in data


class TestAccess:
    @pytest.mark.parametrize("name", ["pages", "sc-overview", "sc-queries", "sc-pages", "ga-overview", "history", "connections"])
    def test_login_tokens_are_refused_and_other_ids_404(self, client, business, key, admin_user, name):
        access = client.post(reverse("accounts:login"), {"email": "boss@agency.com", "password": "SuperSecret123!"}, format="json").data["access"]
        assert client.get(reverse(f"agent:{name}", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access}").status_code == 401
        assert client.get(reverse(f"agent:{name}", args=[999999]), **A(key)).status_code == 404
