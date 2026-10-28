import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.businesses.models import Business, Membership

from .crawl import FetchedPage, build_findings, category_summaries, parse_html, same_site, score_findings
from .models import CrawledPage, CrawlRun, Finding

pytestmark = pytest.mark.django_db


GOOD_TITLE = "Service Business CRM Tips and Growth Guides"  # 43 chars
GOOD_DESCRIPTION = "Practical CRM tips, automation strategies and growth insights for service businesses who want more booked jobs."  # 110 chars


def _page(url="https://saasyway.com/a", title=GOOD_TITLE, description=GOOD_DESCRIPTION, canonical="https://saasyway.com/a", h1_count=1, images_missing_alt=0, links=None, status_code=200, error=""):
    return FetchedPage(url, status_code, "text/html", title, description, canonical, False, status_code < 400 and not error, error, links or [], h1_count=h1_count, images_missing_alt=images_missing_alt)


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
        _page("https://saasyway.com/", canonical="https://saasyway.com/"),
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


def _titles(pages):
    return [finding.title for finding in build_findings(pages)]


def test_clean_page_has_no_findings():
    assert _titles([_page()]) == []


def test_title_and_description_length_findings():
    assert "Title too long" in _titles([_page(title="x" * 61)])
    assert "Title too short" in _titles([_page(title="Home")])
    assert "Meta description too long" in _titles([_page(description="x" * 161)])
    assert "Meta description too short" in _titles([_page(description="Short one")])
    assert _titles([_page(title="x" * 60, description="x" * 160)]) == []


def test_h1_findings():
    assert _titles([_page(h1_count=0)]) == ["Missing H1 heading"]
    assert _titles([_page(h1_count=2)]) == ["Multiple H1 headings"]


def test_images_missing_alt_and_missing_canonical():
    assert _titles([_page(images_missing_alt=3)]) == ["Images missing alt text"]
    assert _titles([_page(canonical="")]) == ["Missing canonical tag"]


def test_broken_internal_link_is_reported_on_the_linking_page():
    home = _page("https://saasyway.com/", canonical="https://saasyway.com/", links=["/gone", "/gone", "/ok", "mailto:hi@saasyway.com", "https://elsewhere.com/x"])
    ok = _page("https://saasyway.com/ok", canonical="https://saasyway.com/ok")
    gone = _page("https://saasyway.com/gone", status_code=404, error="HTTP 404")
    broken = [finding for finding in build_findings([home, ok, gone]) if finding.title == "Broken internal link"]

    assert [(finding.url, "https://saasyway.com/gone" in finding.detail) for finding in broken] == [("https://saasyway.com/", True)]


def test_parse_html_counts_h1_and_images_that_need_alt():
    parsed = parse_html(
        """
        <h1>Main</h1><h1>Second</h1>
        <img src="/a.png" alt="A chart">
        <img src="/b.png">
        <img src="/c.png" alt="  ">
        <img src="/d.png" alt="" role="presentation">
        <img src="/e.png" width="1" height="1">
        <img src="data:image/gif;base64,R0lGOD" alt="">
        """
    )
    assert parsed.h1_count == 2
    assert parsed.images_missing_alt == 2


def test_one_site_wide_issue_cannot_zero_the_score():
    pages = [_page(f"https://saasyway.com/{i}", title=f"{GOOD_TITLE} {i}", description=f"{GOOD_DESCRIPTION} {i}", canonical="") for i in range(25)]
    assert score_findings(build_findings(pages)) == 88


def test_category_summaries_flag_only_categories_with_findings():
    findings = build_findings([_page(h1_count=0)])
    cards = {card["label"]: card["status"] for card in category_summaries([_page(h1_count=0)], findings)}

    assert cards["Headings"] == "Needs work"
    assert cards["Titles"] == "Healthy"


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
