import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.businesses.models import Business, Membership
from apps.site_health.models import CrawlRun
from apps.site_health.services import execute_crawl

from .models import Event
from .services import record_event

pytestmark = pytest.mark.django_db


@pytest.fixture
def client():
    return APIClient()


def _login(client, email):
    response = client.post(reverse("accounts:register"), {"email": email, "password": "SuperSecret123!"}, format="json")
    account_id = Membership.objects.get(user__email=email).account_id
    return response.data["access"], account_id


class TestHistory:
    def test_empty_until_something_happens(self, client):
        access, account_id = _login(client, "hist-empty@example.com")
        business = Business.objects.create(account_id=account_id, name="B", website="https://saasyway.com")

        response = client.get(reverse("history:list", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access}")

        assert response.status_code == 200
        assert response.data["results"] == []
        assert response.data["score_trend"] == []

    def test_newest_first_with_actor_and_foreign_business_hidden(self, client):
        access, account_id = _login(client, "hist-a@example.com")
        business = Business.objects.create(account_id=account_id, name="B", website="https://saasyway.com")
        record_event(business, "test.first", "First")
        record_event(business, "test.second", "Second", actor=business.account.memberships.first().user)

        results = client.get(reverse("history:list", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access}").data["results"]
        assert [row["summary"] for row in results] == ["Second", "First"]
        assert results[0]["actor"] == "hist-a@example.com"
        assert results[1]["actor"] is None

        other = APIClient()
        other_access, _ = _login(other, "hist-b@example.com")
        assert other.get(reverse("history:list", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {other_access}").status_code == 404

    def test_approval_decisions_are_recorded(self, client):
        from apps.site_health.models import Finding

        access, account_id = _login(client, "hist-appr@example.com")
        business = Business.objects.create(account_id=account_id, name="B", website="https://saasyway.com")
        crawl = CrawlRun.objects.create(business=business, seed_url=business.website, status=CrawlRun.Status.COMPLETED, score=96)
        Finding.objects.create(crawl=crawl, url="https://saasyway.com/", category="Titles", severity="high", title="Missing title", detail="d")
        auth = {"HTTP_AUTHORIZATION": f"Bearer {access}"}
        approval = client.get(reverse("approvals:list", args=[business.id]), **auth).data["approvals"][0]

        for decision in ("approved", "pending"):
            client.post(reverse("approvals:decision", args=[business.id, approval["id"]]), {"decision": decision}, format="json", **auth)

        kinds = list(Event.objects.filter(business=business).order_by("id").values_list("kind", flat=True))
        assert kinds == ["approval.approved", "approval.reopened"]
        data = client.get(reverse("history:list", args=[business.id]), **auth).data
        assert data["score_trend"][0]["score"] == 96

    def test_crawl_completion_and_failure_are_recorded(self, monkeypatch):
        account_id = Membership.objects.create(user=_make_user(), account=_make_account()).account_id
        business = Business.objects.create(account_id=account_id, name="B", website="https://saasyway.com")

        class Fake:
            def __init__(self, seed):
                pass

            def crawl(self):
                return []

        monkeypatch.setattr("apps.site_health.services.SiteCrawler", Fake)
        crawl = CrawlRun.objects.create(business=business, seed_url=business.website)
        execute_crawl(crawl.id)

        class Boom(Fake):
            def crawl(self):
                raise RuntimeError("site down")

        monkeypatch.setattr("apps.site_health.services.SiteCrawler", Boom)
        failed = CrawlRun.objects.create(business=business, seed_url=business.website)
        execute_crawl(failed.id)

        events = {event.kind: event.summary for event in Event.objects.filter(business=business)}
        assert events["crawl.completed"] == "Site crawl completed: score 100, 0 pages, 0 findings."
        assert events["crawl.failed"] == "Site crawl failed: site down"


def _make_user():
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(email="hist-crawl@example.com", password="SuperSecret123!")


def _make_account():
    from apps.businesses.models import Account

    return Account.objects.create(name="Acct")
