import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.businesses.models import Business, Membership

from .crawl import FetchedPage, build_findings, parse_html, same_site, score_findings
from .models import CrawledPage, CrawlRun, Finding

pytestmark = pytest.mark.django_db


def test_parse_html_reads_title_description_and_noindex():
    parsed = parse_html(
        """
        <html><head>
          <title>Saasyway home</title>
          <meta name="description" content="Company site">
          <meta name="robots" content="noindex, nofollow">
          <link rel="canonical" href="https://saasyway.com/">
          <a href="/about">About</a>
        </head></html>
        """
    )
    assert parsed.title == "Saasyway home"
    assert parsed.meta_description == "Company site"
    assert parsed.noindex is True
    assert parsed.canonical == "https://saasyway.com/"
    assert parsed.links == ["/about"]


def test_same_site_treats_www_as_the_same_host():
    assert same_site("https://www.saasyway.com/about", "https://saasyway.com")
    assert not same_site("https://other.example/about", "https://saasyway.com")


def test_score_drops_for_a_page_that_did_not_load():
    pages = [
        FetchedPage("https://saasyway.com/", 200, "text/html", "Home", "Desc", "", False, True, "", []),
        FetchedPage("https://saasyway.com/missing", 404, "text/html", "", "", "", False, False, "HTTP 404", []),
    ]
    findings = build_findings(pages)
    assert any(finding.severity == "critical" for finding in findings)
    assert score_findings(findings) == 80


def test_missing_title_is_a_finding_and_duplicate_titles_are_too():
    pages = [
        FetchedPage("https://saasyway.com/a", 200, "text/html", "Same", "One", "", False, True, "", []),
        FetchedPage("https://saasyway.com/b", 200, "text/html", "Same", "Two", "", False, True, "", []),
        FetchedPage("https://saasyway.com/c", 200, "text/html", "", "Three", "", False, True, "", []),
    ]
    findings = build_findings(pages)
    titles = [finding.title for finding in findings]
    assert titles.count("Duplicate title") == 2
    assert "Missing title" in titles


@pytest.fixture
def client():
    return APIClient()


def _login(client, email):
    response = client.post(reverse("accounts:register"), {"email": email, "password": "SuperSecret123!"}, format="json")
    account_id = Membership.objects.get(user__email=email).account_id
    return response.data["access"], account_id


class TestSiteHealthEndpoints:
    def test_report_is_empty_until_a_crawl_exists(self, client):
        access, account_id = _login(client, "health-empty@example.com")
        business = Business.objects.create(account_id=account_id, name="Empty", website="https://example.com")

        response = client.get(reverse("site_health:report", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access}")

        assert response.status_code == 200
        assert response.data["status"] == "none"
        assert response.data["score"] is None
        assert response.data["pages"] == []

    def test_completed_crawl_lists_fetched_pages_and_findings(self, client):
        access, account_id = _login(client, "health-pages@example.com")
        business = Business.objects.create(account_id=account_id, name="Crawled", website="https://saasyway.com")
        crawl = CrawlRun.objects.create(
            business=business,
            seed_url="https://saasyway.com",
            status=CrawlRun.Status.COMPLETED,
            score=80,
            pages_crawled=1,
        )
        CrawledPage.objects.create(
            crawl=crawl,
            url="https://saasyway.com/about",
            status_code=200,
            title="About",
            meta_description="About the company",
            noindex=False,
        )
        Finding.objects.create(
            crawl=crawl,
            url="https://saasyway.com/about",
            category="Titles",
            severity=Finding.Severity.MEDIUM,
            title="Duplicate title",
            detail="This title is shared with 1 other crawled page(s).",
        )

        response = client.get(reverse("site_health:report", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access}")

        assert response.status_code == 200
        assert response.data["pages"] == [
            {
                "url": "https://saasyway.com/about",
                "status_code": 200,
                "title": "About",
                "meta_description": "About the company",
                "noindex": False,
            }
        ]
        assert response.data["findings"][0]["url"] == "https://saasyway.com/about"

    def test_crawl_requires_a_website(self, client):
        access, account_id = _login(client, "health-noweb@example.com")
        business = Business.objects.create(account_id=account_id, name="No site")

        response = client.post(reverse("site_health:crawl", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access}")

        assert response.status_code == 400

    def test_crawl_enqueues_and_foreign_business_is_hidden(self, client, monkeypatch):
        access, account_id = _login(client, "health-a@example.com")
        business = Business.objects.create(account_id=account_id, name="Mine", website="https://saasyway.com")
        queued = {}
        monkeypatch.setattr("apps.site_health.views.crawl_business_site.delay", lambda crawl_id: queued.setdefault("id", crawl_id))

        response = client.post(reverse("site_health:crawl", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access}")
        assert response.status_code == 202
        assert queued["id"] == response.data["id"]

        other = APIClient()
        other_access, _ = _login(other, "health-b@example.com")
        hidden = other.get(reverse("site_health:report", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {other_access}")
        assert hidden.status_code == 404
