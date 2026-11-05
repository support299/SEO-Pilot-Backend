from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.agent.models import AgentKey
from apps.businesses.models import Account, Business
from apps.history.models import Event
from apps.site_health.models import CrawledPage, CrawlRun, Finding
from apps.site_health.services import execute_crawl

from .checklist import apply_crawl_results
from .models import ChecklistItem, ChecklistStatusChange, Subscription

pytestmark = pytest.mark.django_db
REGISTRY = Path(settings.BASE_DIR) / "docs" / "checklist" / "SEO_Pilot_Package_Entitlements_SEO-001_to_SEO-100.csv"


@pytest.fixture(autouse=True)
def registry():
    call_command("import_checklist", str(REGISTRY))


@pytest.fixture
def site():
    business = Business.objects.create(account=Account.objects.create(name="Acme"), name="Acme", website="https://acme.example")
    Subscription.objects.create(business=business, package="starter")
    return business


def _crawl(site, findings=(), pages=10):
    crawl = CrawlRun.objects.create(business=site, seed_url=site.website, status=CrawlRun.Status.COMPLETED, pages_crawled=pages, finished_at=timezone.now())
    for n in range(pages):
        CrawledPage.objects.create(crawl=crawl, url=f"https://acme.example/p{n}", status_code=200)
    for url, title in findings:
        Finding.objects.create(crawl=crawl, url=url, category="x", severity="medium", title=title, detail="-")
    return crawl


def _item(site, feature_id):
    return ChecklistItem.objects.filter(business=site, feature__feature_id=feature_id).first()


def test_problems_become_needs_work_and_clean_items_become_checked(site):
    crawl = _crawl(site, [("https://acme.example/p1", "Missing meta description"), ("https://acme.example/p2", "Duplicate meta description"), ("https://acme.example/p3", "Page did not load")])
    assert apply_crawl_results(crawl) == 6
    meta = _item(site, "SEO-027")
    assert meta.status == "needs_work" and meta.source == "crawl"
    assert "2 of 10 crawled pages" in meta.finding and "https://acme.example/p1" in meta.finding and "missing meta description (1)" in meta.finding
    assert _item(site, "SEO-018").status == "needs_work"
    assert _item(site, "SEO-026").status == "verified" and "no problems with Title tags found on 10 crawled pages" in _item(site, "SEO-026").finding
    assert ChecklistStatusChange.objects.filter(source="crawl").count() == 6
    assert Event.objects.get(kind="checklist.prefilled").metadata["items"] == 6


def test_never_marks_anything_done_and_only_touches_crawl_items(site):
    apply_crawl_results(_crawl(site))
    statuses = set(ChecklistItem.objects.filter(business=site).values_list("status", flat=True))
    assert statuses == {"verified"}
    covered = set(ChecklistItem.objects.filter(business=site).values_list("feature__feature_id", flat=True))
    assert covered == {"SEO-016", "SEO-018", "SEO-026", "SEO-027", "SEO-028", "SEO-034"}


def test_a_later_crawl_updates_its_own_results(site):
    apply_crawl_results(_crawl(site, [("https://acme.example/p1", "Missing title")]))
    assert _item(site, "SEO-026").status == "needs_work"
    apply_crawl_results(_crawl(site))  # fixed since
    item = _item(site, "SEO-026")
    assert item.status == "verified" and item.source == "crawl"
    assert list(item.changes.order_by("id").values_list("to_status", flat=True)) == ["needs_work", "verified"]


def test_an_unchanged_recrawl_adds_no_history(site):
    findings = [("https://acme.example/p1", "Missing title")]
    apply_crawl_results(_crawl(site, findings))
    crawl = _crawl(site, findings)
    crawl.finished_at = CrawlRun.objects.order_by("id").first().finished_at  # same day, same finding text
    crawl.save()
    before = ChecklistStatusChange.objects.count()
    apply_crawl_results(crawl)
    assert ChecklistStatusChange.objects.count() == before


def test_what_an_agent_or_person_recorded_is_never_overwritten(site):
    admin = get_user_model().objects.create_user(email="arman@agency.com", password="SuperSecret123!", is_agency_admin=True)
    key = AgentKey.issue(name="claude-daily", created_by=admin)[1]
    apply_crawl_results(_crawl(site, [("https://acme.example/p1", "Missing title")]))
    client = APIClient()
    response = client.put(reverse("agent:checklist-item", args=[site.id, "SEO-026"]), {"status": "in_progress", "finding": "Rewriting 1 title"}, format="json", HTTP_AUTHORIZATION=f"Agent {key}")
    assert response.status_code == 200 and response.data["source"] == ""
    apply_crawl_results(_crawl(site))  # crawl now clean, but the agent owns the item
    item = _item(site, "SEO-026")
    assert item.status == "in_progress" and item.finding == "Rewriting 1 title"


def test_no_active_package_means_no_prefill(site):
    Subscription.objects.filter(business=site).update(status="cancelled")
    assert apply_crawl_results(_crawl(site)) == 0
    assert not ChecklistItem.objects.exists()


def test_history_names_the_crawl(site):
    admin = get_user_model().objects.create_user(email="arman@agency.com", password="SuperSecret123!", is_agency_admin=True)
    apply_crawl_results(_crawl(site, [("https://acme.example/p1", "Missing H1 heading")]))
    client = APIClient()
    client.force_authenticate(admin)
    history = client.get(reverse("subscriptions:checklist-item", args=[site.id, "SEO-028"])).data
    assert history[0]["actor"] == "site crawl"


def test_a_real_crawl_run_prefills_and_a_prefill_error_never_fails_the_crawl(site, monkeypatch):
    class Page:
        def __init__(self, url, title):
            self.url, self.status_code, self.content_type, self.title, self.meta_description = url, 200, "text/html", title, "A useful description of this page that is long enough to pass."
            self.canonical, self.noindex, self.is_html, self.error, self.links, self.h1_count, self.images_missing_alt = url, False, True, "", [], 1, 0

    class FakeCrawler:
        def __init__(self, seed, **kwargs):
            pass

        def crawl(self):
            return [Page("https://acme.example/", ""), Page("https://acme.example/about", "About Acme Gutters and Roofing Services")]

    monkeypatch.setattr("apps.site_health.services.SiteCrawler", FakeCrawler)
    crawl = CrawlRun.objects.create(business=site, seed_url=site.website)
    execute_crawl(crawl.id)
    assert _item(site, "SEO-026").status == "needs_work"  # the home page has no title

    monkeypatch.setattr("apps.subscriptions.checklist.apply_crawl_results", lambda crawl: (_ for _ in ()).throw(RuntimeError("boom")))
    second = CrawlRun.objects.create(business=site, seed_url=site.website)
    execute_crawl(second.id)
    second.refresh_from_db()
    assert second.status == CrawlRun.Status.COMPLETED
