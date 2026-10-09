import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.businesses.models import Business, Membership
from apps.site_health.models import CrawlRun, Finding

from .models import Approval

pytestmark = pytest.mark.django_db


@pytest.fixture
def client():
    return APIClient()


def _login(client, email):
    response = client.post(reverse("accounts:register"), {"email": email, "password": "SuperSecret123!"}, format="json")
    account_id = Membership.objects.get(user__email=email).account_id
    return response.data["access"], account_id


def _crawl_with_findings(business, titles):
    crawl = CrawlRun.objects.create(business=business, seed_url=business.website, status=CrawlRun.Status.COMPLETED, score=90)
    for index, title in enumerate(titles):
        Finding.objects.create(crawl=crawl, url=f"https://saasyway.com/p{index}", category="Titles", severity="high", title=title, detail="detail")
    return crawl


class TestApprovals:
    def test_empty_without_a_completed_crawl(self, client):
        access, account_id = _login(client, "appr-empty@example.com")
        business = Business.objects.create(account_id=account_id, name="B", website="https://saasyway.com")

        response = client.get(reverse("approvals:list", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access}")

        assert response.status_code == 200
        assert response.data["approvals"] == []

    def test_groups_findings_by_issue_type(self, client):
        access, account_id = _login(client, "appr-group@example.com")
        business = Business.objects.create(account_id=account_id, name="B", website="https://saasyway.com")
        _crawl_with_findings(business, ["Missing title", "Missing title"])

        response = client.get(reverse("approvals:list", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access}")

        assert len(response.data["approvals"]) == 1
        assert response.data["approvals"][0]["title"] == "Missing title"
        assert len(response.data["approvals"][0]["affected_urls"]) == 2
        assert response.data["approvals"][0]["status"] == "pending"

    def test_decision_survives_a_new_sync_and_resolves_when_issue_disappears(self, client):
        access, account_id = _login(client, "appr-decide@example.com")
        business = Business.objects.create(account_id=account_id, name="B", website="https://saasyway.com")
        _crawl_with_findings(business, ["Missing title", "Duplicate title"])
        auth = {"HTTP_AUTHORIZATION": f"Bearer {access}"}
        listing = client.get(reverse("approvals:list", args=[business.id]), **auth).data["approvals"]
        missing = next(item for item in listing if item["title"] == "Missing title")

        decided = client.post(reverse("approvals:decision", args=[business.id, missing["id"]]), {"decision": "approved"}, format="json", **auth)
        assert decided.status_code == 200
        assert decided.data["status"] == "approved"
        assert decided.data["decided_at"] is not None

        _crawl_with_findings(business, ["Duplicate title"])
        again = {item["title"]: item["status"] for item in client.get(reverse("approvals:list", args=[business.id]), **auth).data["approvals"]}
        assert again["Missing title"] == "approved"
        assert again["Duplicate title"] == "pending"

        _crawl_with_findings(business, [])
        final = {item["title"]: item["status"] for item in client.get(reverse("approvals:list", args=[business.id]), **auth).data["approvals"]}
        assert final["Duplicate title"] == "resolved"
        assert final["Missing title"] == "approved"

    def test_rejects_bad_decision_and_foreign_business(self, client):
        access, account_id = _login(client, "appr-a@example.com")
        business = Business.objects.create(account_id=account_id, name="B", website="https://saasyway.com")
        _crawl_with_findings(business, ["Missing title"])
        auth = {"HTTP_AUTHORIZATION": f"Bearer {access}"}
        approval = client.get(reverse("approvals:list", args=[business.id]), **auth).data["approvals"][0]

        bad = client.post(reverse("approvals:decision", args=[business.id, approval["id"]]), {"decision": "resolved"}, format="json", **auth)
        assert bad.status_code == 400

        other = APIClient()
        other_access, _ = _login(other, "appr-b@example.com")
        hidden = other.get(reverse("approvals:list", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {other_access}")
        assert hidden.status_code == 404
        assert Approval.objects.get(id=approval["id"]).status == "pending"
