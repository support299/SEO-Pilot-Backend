"""A site with well over 25 pages must be crawled in full (up to SITE_CRAWL_PAGE_LIMIT), not cut off after ~20."""

import pytest

from apps.businesses.models import Account, Business

from .crawl import SiteCrawler
from .models import CrawledPage, CrawlRun
from .services import execute_crawl

SITE = "https://bigsite.example"
PAGE_COUNT = 160


class FakeResponse:
    def __init__(self, url, status_code=200, text="", content_type="text/html"):
        self.url, self.status_code, self.text = url, status_code, text
        self.headers = {"Content-Type": content_type}
        self.ok = 200 <= status_code < 400


class FakeSite:
    """160 pages listed in a sitemap index with two child sitemaps; every page links to the next one."""

    headers: dict = {}

    def get(self, url, timeout=None, allow_redirects=True):
        if url == f"{SITE}/robots.txt":
            return FakeResponse(url, 404)
        if url == f"{SITE}/sitemap.xml":
            return FakeResponse(url, text="".join(f"<sitemap><loc>{SITE}/sitemap-{n}.xml</loc></sitemap>" for n in (1, 2)), content_type="application/xml")
        if url.startswith(f"{SITE}/sitemap-"):
            half = 1 if url.endswith("-1.xml") else 2
            numbers = range(1, 81) if half == 1 else range(81, PAGE_COUNT + 1)
            return FakeResponse(url, text="".join(f"<url><loc>{SITE}/page-{n}</loc></url>" for n in numbers), content_type="application/xml")
        number = int(url.rsplit("-", 1)[1]) if "/page-" in url else 0
        next_link = f'<a href="/page-{number + 1}">next</a>' if number < PAGE_COUNT else ""
        return FakeResponse(url, text=f"<html><head><title>Page {number}</title><meta name='description' content='d{number}'></head><body><h1>P</h1>{next_link}</body></html>")


def test_all_pages_of_a_large_site_are_crawled():
    pages = SiteCrawler(SITE, limit=500, session=FakeSite()).crawl()
    urls = {page.url for page in pages}
    assert len(pages) == PAGE_COUNT + 1  # home + every page from both child sitemaps
    assert f"{SITE}/page-1" in urls and f"{SITE}/page-{PAGE_COUNT}" in urls


def test_the_limit_is_still_respected():
    assert len(SiteCrawler(SITE, limit=40, session=FakeSite()).crawl()) == 40


@pytest.mark.django_db
def test_background_crawl_uses_the_configured_limit(settings, monkeypatch):
    settings.SITE_CRAWL_PAGE_LIMIT = 120
    monkeypatch.setattr("apps.site_health.crawl.requests.Session", FakeSite)
    business = Business.objects.create(account=Account.objects.create(name="Big"), name="Big", website=SITE)
    crawl = CrawlRun.objects.create(business=business, seed_url=SITE)
    execute_crawl(crawl.id)
    crawl.refresh_from_db()
    assert crawl.status == CrawlRun.Status.COMPLETED
    assert CrawledPage.objects.filter(crawl=crawl).count() == 120
