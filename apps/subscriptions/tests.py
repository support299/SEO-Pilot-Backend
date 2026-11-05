import json

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APIClient

from apps.businesses.models import Account, Business, Membership

from .entitlements import entitlement
from .models import Subscription
from .packages import CAPABILITIES, Level

pytestmark = pytest.mark.django_db
User = get_user_model()

# Words that would reveal internal capacity or economics. None may appear in any customer-facing response (AC03).
FORBIDDEN_IN_CUSTOMER_JSON = ("minute", "hour", "overhead", "planning", "budget", "reservation", "ledger", "internal_notes", "external_reference")


@pytest.fixture
def client():
    return APIClient()


def _customer(email, account=None):
    user = User.objects.create_user(email=email, password="SuperSecret123!")
    account = account or Account.objects.create(name=f"{email} co")
    Membership.objects.create(account=account, user=user, role=Membership.Role.VIEWER)
    return user, account


def _site(account, package=None, status=Subscription.Status.ACTIVE, name="Site"):
    business = Business.objects.create(account=account, name=name, website="https://example.com")
    if package:
        Subscription.objects.create(business=business, package=package, status=status, internal_notes="secret", external_reference="pay_123")
    return business


@pytest.fixture
def agency_admin():
    return User.objects.create_user(email="admin@example.com", password="SuperSecret123!", is_agency_admin=True)


class TestEntitlements:
    def test_starter_cannot_run_article_outreach(self):  # AC04
        _, account = _customer("a@example.com")
        assert entitlement(_site(account, "starter"), "SEO-060").level == Level.NOT_INCLUDED
        assert entitlement(_site(account, "starter", name="b"), "offsite_articles").can_execute is False

    def test_authority_includes_articles(self):
        _, account = _customer("a@example.com")
        assert entitlement(_site(account, "authority"), "SEO-060").can_execute is True

    def test_growth_local_pages_are_conditional_not_executable(self):  # AC05
        _, account = _customer("a@example.com")
        result = entitlement(_site(account, "growth"), "SEO-031")
        assert result.level == Level.CONDITIONAL
        assert result.can_execute is False

    def test_growth_identifies_mentions_but_cannot_do_outreach(self):  # AC06
        _, account = _customer("a@example.com")
        site = _site(account, "growth")
        assert entitlement(site, "unlinked_mention_identification").level == Level.CONDITIONAL
        assert entitlement(site, "unlinked_mention_outreach").level == Level.NOT_INCLUDED

    def test_hosting_and_reputation_are_optional_in_every_tier(self):  # AC07, AC09
        _, account = _customer("a@example.com")
        for package in ("starter", "growth", "authority"):
            site = _site(account, package, name=package)
            assert entitlement(site, "hosting").level == Level.OPTIONAL
            assert entitlement(site, "reputation_management").can_execute is False

    def test_seo_030_maps_to_the_stricter_capability(self):
        _, account = _customer("a@example.com")
        assert entitlement(_site(account, "starter"), "SEO-030").level == Level.CONDITIONAL

    def test_second_website_does_not_inherit_the_first_subscription(self):  # AC02
        _, account = _customer("a@example.com")
        paid = _site(account, "authority", name="paid")
        unpaid = _site(account, None, name="unpaid")
        assert entitlement(paid, "offsite_articles").can_execute is True
        result = entitlement(unpaid, "offsite_articles")
        assert result.level is None and result.reason == "no_subscription"

    def test_cancelled_or_pending_subscription_grants_nothing(self):
        _, account = _customer("a@example.com")
        for status in ("cancelled", "pending"):
            site = _site(account, "authority", status=status, name=status)
            assert entitlement(site, "dashboard_baselines").can_execute is False

    def test_unknown_capability_raises(self):
        _, account = _customer("a@example.com")
        with pytest.raises(ValueError):
            entitlement(_site(account, "starter"), "SEO-999")

    def test_every_capability_defines_all_three_packages(self):
        for capability in CAPABILITIES:
            assert set(p.value for p in capability.levels) == {"starter", "growth", "authority"}


class TestPlanEndpoint:
    def test_customer_plan_contains_no_internal_fields(self, client):  # AC03
        user, account = _customer("c@example.com")
        site = _site(account, "growth")
        client.force_authenticate(user)
        response = client.get(reverse("subscriptions:plan", args=[site.id]))
        assert response.status_code == 200
        assert response.data["package"] == "growth"
        assert response.data["monthly_price_usd"] == 957
        assert "staff" not in response.data
        body = json.dumps(response.data).lower()
        for word in FORBIDDEN_IN_CUSTOMER_JSON:
            assert word not in body

    def test_business_list_for_customer_has_no_internal_fields(self, client):  # AC03
        user, account = _customer("c@example.com")
        _site(account, "starter")
        client.force_authenticate(user)
        response = client.get(reverse("businesses:business-list"))
        assert response.status_code == 200
        assert response.data["results"][0]["package"] == "starter"
        body = json.dumps(response.data).lower()
        for word in FORBIDDEN_IN_CUSTOMER_JSON:
            assert word not in body

    def test_staff_sees_internal_fields(self, client, agency_admin):
        _, account = _customer("c@example.com")
        site = _site(account, "starter")
        client.force_authenticate(agency_admin)
        response = client.get(reverse("subscriptions:plan", args=[site.id]))
        assert response.data["staff"]["external_reference"] == "pay_123"

    def test_other_customers_site_is_not_found(self, client):  # AC01
        user, _ = _customer("c@example.com")
        _, other_account = _customer("d@example.com")
        other_site = _site(other_account, "starter")
        client.force_authenticate(user)
        assert client.get(reverse("subscriptions:plan", args=[other_site.id])).status_code == 404

    def test_site_without_subscription_reports_not_subscribed(self, client):
        user, account = _customer("c@example.com")
        site = _site(account, None)
        client.force_authenticate(user)
        response = client.get(reverse("subscriptions:plan", args=[site.id]))
        assert response.data["subscribed"] is False
        assert response.data["status"] == "none"


class TestSetSubscription:
    def test_customer_cannot_change_their_package(self, client):
        user, account = _customer("c@example.com")
        site = _site(account, "starter")
        client.force_authenticate(user)
        response = client.put(reverse("subscriptions:subscription", args=[site.id]), {"package": "authority"}, format="json")
        assert response.status_code == 403
        site.subscription.refresh_from_db()
        assert site.subscription.package == "starter"

    def test_admin_sets_and_changes_package_and_it_is_logged(self, client, agency_admin):
        _, account = _customer("c@example.com")
        site = _site(account, None)
        client.force_authenticate(agency_admin)
        url = reverse("subscriptions:subscription", args=[site.id])
        assert client.put(url, {"package": "starter"}, format="json").status_code == 201
        response = client.put(url, {"package": "growth"}, format="json")
        assert response.status_code == 200
        assert response.data["package"] == "growth"
        assert site.history_events.filter(kind="subscription.updated").count() == 2

    def test_invalid_package_rejected(self, client, agency_admin):
        _, account = _customer("c@example.com")
        site = _site(account, None)
        client.force_authenticate(agency_admin)
        assert client.put(reverse("subscriptions:subscription", args=[site.id]), {"package": "gold"}, format="json").status_code == 400


class TestAgencyOnboarding:
    def test_creating_a_client_with_a_package_creates_the_subscription(self, client, agency_admin):
        client.force_authenticate(agency_admin)
        response = client.post(
            reverse("businesses:agency-customers"),
            {"customer_name": "Acme", "website": "acme.com", "package": "growth"},
            format="json",
        )
        assert response.status_code == 201
        assert Business.objects.get(id=response.data["business"]["id"]).subscription.package == "growth"

    def test_creating_a_client_without_a_package_still_works(self, client, agency_admin):
        client.force_authenticate(agency_admin)
        response = client.post(reverse("businesses:agency-customers"), {"customer_name": "Acme"}, format="json")
        assert response.status_code == 201
        assert not Subscription.objects.exists()


# --- scope-change and plan-change requests -------------------------------------------------------------------------

from datetime import timedelta  # noqa: E402

from django.utils import timezone  # noqa: E402

from .models import PlanChangeRequest, ScopeChangeRequest  # noqa: E402
from .services import apply_due_plan_changes  # noqa: E402


class TestScopeRequests:
    def _url(self, site):
        return reverse("subscriptions:scope-requests", args=[site.id])

    def test_starter_asking_for_articles_records_a_request_and_starts_no_work(self, client):  # AC04
        user, account = _customer("c@example.com")
        site = _site(account, "starter")
        client.force_authenticate(user)  # a read-only customer may still ask
        response = client.post(self._url(site), {"capability": "SEO-060", "note": "Please add articles"}, format="json")
        assert response.status_code == 201
        assert response.data["capability"] == "offsite_articles"
        assert ScopeChangeRequest.objects.filter(business=site, status="open").count() == 1
        assert entitlement(site, "offsite_articles").can_execute is False

    def test_cannot_request_what_is_already_included(self, client):
        user, account = _customer("c@example.com")
        site = _site(account, "authority")
        client.force_authenticate(user)
        response = client.post(self._url(site), {"capability": "offsite_articles"}, format="json")
        assert response.status_code == 400 and response.data["code"] == "already_included"

    def test_optional_benefits_are_not_scope_changes(self, client):
        user, account = _customer("c@example.com")
        site = _site(account, "starter")
        client.force_authenticate(user)
        assert client.post(self._url(site), {"capability": "hosting"}, format="json").data["code"] == "optional_benefit"

    def test_duplicate_open_request_is_refused(self, client):
        user, account = _customer("c@example.com")
        site = _site(account, "starter")
        client.force_authenticate(user)
        assert client.post(self._url(site), {"capability": "SEO-060"}, format="json").status_code == 201
        assert client.post(self._url(site), {"capability": "SEO-060"}, format="json").status_code == 409

    def test_unknown_capability_and_no_subscription(self, client):
        user, account = _customer("c@example.com")
        site, bare = _site(account, "starter"), _site(account, None, name="bare")
        client.force_authenticate(user)
        assert client.post(self._url(site), {"capability": "SEO-999"}, format="json").data["code"] == "unknown_capability"
        assert client.post(self._url(bare), {"capability": "SEO-060"}, format="json").data["code"] == "no_active_subscription"

    def test_cannot_request_for_another_customers_site(self, client):  # AC01
        user, _ = _customer("c@example.com")
        _, other = _customer("d@example.com")
        client.force_authenticate(user)
        assert client.post(self._url(_site(other, "starter")), {"capability": "SEO-060"}, format="json").status_code == 404

    def test_customer_view_hides_who_asked_and_staff_view_shows_it(self, client, agency_admin):
        user, account = _customer("c@example.com")
        site = _site(account, "starter")
        client.force_authenticate(user)
        client.post(self._url(site), {"capability": "SEO-060"}, format="json")
        assert "requested_by" not in client.get(self._url(site)).data[0]
        client.force_authenticate(agency_admin)
        assert client.get(self._url(site)).data[0]["requested_by"] == "c@example.com"

    def test_customer_cannot_decide_but_staff_can_decline_without_granting(self, client, agency_admin):
        user, account = _customer("c@example.com")
        site = _site(account, "starter")
        client.force_authenticate(user)
        rid = client.post(self._url(site), {"capability": "SEO-060"}, format="json").data["id"]
        url = reverse("subscriptions:scope-decision", args=[site.id, rid])
        assert client.post(url, {"decision": "approve", "grant": True}, format="json").status_code == 403
        client.force_authenticate(agency_admin)
        assert client.post(url, {"decision": "decline", "decision_note": "Not now"}, format="json").data["status"] == "declined"
        assert entitlement(site, "offsite_articles").can_execute is False
        assert client.post(url, {"decision": "approve"}, format="json").status_code == 409  # already decided

    def test_approving_without_grant_changes_nothing_and_with_grant_unlocks_it(self, client, agency_admin):  # AC14
        user, account = _customer("c@example.com")
        site = _site(account, "starter")
        client.force_authenticate(user)
        first = client.post(self._url(site), {"capability": "SEO-060"}, format="json").data["id"]
        client.force_authenticate(agency_admin)
        client.post(reverse("subscriptions:scope-decision", args=[site.id, first]), {"decision": "approve"}, format="json")
        assert entitlement(site, "offsite_articles").can_execute is False
        second = client.post(self._url(site), {"capability": "SEO-060"}, format="json").data["id"]
        client.post(reverse("subscriptions:scope-decision", args=[site.id, second]), {"decision": "approve", "grant": True}, format="json")
        assert entitlement(Business.objects.get(pk=site.pk), "offsite_articles").can_execute is True
        plan = client.get(reverse("subscriptions:plan", args=[site.id])).data
        row = next(c for c in plan["capabilities"] if c["key"] == "offsite_articles")
        assert row["level"] == "included" and row["note"] == "Added by agreement."

    def test_grant_cannot_unlock_an_optional_benefit(self):
        _, account = _customer("c@example.com")
        site = _site(account, "starter")
        site.subscription.granted_capabilities = ["hosting"]
        site.subscription.save()
        site.refresh_from_db()
        assert entitlement(site, "hosting").can_execute is False


class TestPlanChanges:
    def _create(self, client, site, package="growth", **extra):
        return client.post(reverse("subscriptions:plan-changes", args=[site.id]), {"requested_package": package, **extra}, format="json")

    def test_request_is_recorded_with_no_effective_date_or_price_change(self, client, agency_admin):  # AC12
        _, account = _customer("c@example.com")
        site = _site(account, "starter")
        client.force_authenticate(agency_admin)
        response = self._create(client, site, notice_received_at=(timezone.now() - timedelta(days=3)).isoformat())
        assert response.status_code == 201
        assert response.data["status"] == "pending" and response.data["confirmed_effective_on"] is None
        site.subscription.refresh_from_db()
        assert site.subscription.package == "starter"

    def test_customer_sees_awaiting_confirmation_and_no_staff_note(self, client, agency_admin):
        user, account = _customer("c@example.com")
        site = _site(account, "growth")
        client.force_authenticate(agency_admin)
        self._create(client, site, package="starter", note="internal: asked by phone")
        client.force_authenticate(user)
        plan = client.get(reverse("subscriptions:plan", args=[site.id])).data
        assert plan["pending_change"]["effective_date_label"] == "Effective date awaiting confirmation"
        assert plan["pending_change"]["effective_on"] is None
        assert "internal" not in json.dumps(plan)

    def test_customer_cannot_record_or_list_plan_changes(self, client):
        user, account = _customer("c@example.com")
        site = _site(account, "starter")
        client.force_authenticate(user)
        assert self._create(client, site).status_code == 403
        assert client.get(reverse("subscriptions:plan-changes", args=[site.id])).status_code == 403

    def test_same_package_second_open_change_and_no_subscription_are_refused(self, client, agency_admin):
        _, account = _customer("c@example.com")
        site, bare = _site(account, "starter"), _site(account, None, name="bare")
        client.force_authenticate(agency_admin)
        assert self._create(client, site, package="starter").data["code"] == "same_package"
        assert self._create(client, bare).data["code"] == "no_active_subscription"
        assert self._create(client, site).status_code == 201
        assert self._create(client, site, package="authority").status_code == 409

    def _confirm(self, client, site, change_id, **body):
        return client.post(reverse("subscriptions:plan-change-action", args=[site.id, change_id]), body, format="json")

    def test_future_date_waits_then_applies_once_when_due(self, client, agency_admin):
        _, account = _customer("c@example.com")
        site = _site(account, "starter")
        client.force_authenticate(agency_admin)
        change_id = self._create(client, site).data["id"]
        future = timezone.localdate() + timedelta(days=10)
        response = self._confirm(client, site, change_id, action="confirm", effective_on=future.isoformat())
        assert response.data["status"] == "confirmed"
        site.subscription.refresh_from_db()
        assert site.subscription.package == "starter"
        assert apply_due_plan_changes() == 0
        PlanChangeRequest.objects.filter(pk=change_id).update(confirmed_effective_on=timezone.localdate())
        assert apply_due_plan_changes() == 1
        assert apply_due_plan_changes() == 0
        site.subscription.refresh_from_db()
        assert site.subscription.package == "growth"
        assert site.history_events.filter(kind="plan.change_applied").count() == 1

    def test_date_of_today_applies_immediately(self, client, agency_admin):
        _, account = _customer("c@example.com")
        site = _site(account, "starter")
        client.force_authenticate(agency_admin)
        change_id = self._create(client, site).data["id"]
        response = self._confirm(client, site, change_id, action="confirm", effective_on=timezone.localdate().isoformat())
        assert response.data["status"] == "applied"
        site.subscription.refresh_from_db()
        assert site.subscription.package == "growth"

    def test_backdating_is_refused(self, client, agency_admin):
        _, account = _customer("c@example.com")
        site = _site(account, "starter")
        client.force_authenticate(agency_admin)
        change_id = self._create(client, site).data["id"]
        past = (timezone.localdate() - timedelta(days=1)).isoformat()
        assert self._confirm(client, site, change_id, action="confirm", effective_on=past).data["code"] == "date_in_past"

    def test_downgrade_stops_future_ineligible_work_but_keeps_history(self, client, agency_admin):  # AC13
        _, account = _customer("c@example.com")
        site = _site(account, "authority")
        client.force_authenticate(agency_admin)
        site.history_events.create(kind="seo.article.published", summary="Old article")
        change_id = self._create(client, site, package="starter").data["id"]
        self._confirm(client, site, change_id, action="confirm", effective_on=timezone.localdate().isoformat())
        site = Business.objects.get(pk=site.pk)
        assert entitlement(site, "offsite_articles").can_execute is False
        assert site.history_events.filter(kind="seo.article.published").exists()

    def test_cancel_and_confirm_requires_date(self, client, agency_admin):
        _, account = _customer("c@example.com")
        site = _site(account, "starter")
        client.force_authenticate(agency_admin)
        change_id = self._create(client, site).data["id"]
        assert self._confirm(client, site, change_id, action="confirm").status_code == 400
        assert self._confirm(client, site, change_id, action="cancel").data["status"] == "cancelled"
        assert self._create(client, site).status_code == 201  # a new one is allowed after cancelling
