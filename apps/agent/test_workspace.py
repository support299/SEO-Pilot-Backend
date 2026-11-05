import json

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APIClient

from apps.businesses.models import Account, Business
from apps.history.models import Event
from apps.subscriptions.checklist import EXPECTED_TOTALS, import_registry
from apps.subscriptions.models import ChecklistStatusChange, ScopeChangeRequest, Subscription

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
def business():
    account = Account.objects.create(name="Acme")
    site = Business.objects.create(account=account, name="Acme Gutters", website="https://acme.example")
    Subscription.objects.create(business=site, package="starter")
    return site


@pytest.fixture
def keys(admin_user):
    return {name: AgentKey.issue(name=name, created_by=admin_user)[1] for name in ("claude-daily", "codex-nightly")}


@pytest.fixture
def registry():
    def codes(total):
        return [c for c in "ICANX" for _ in range(total[c])]

    s, g, a = (codes(EXPECTED_TOTALS[t]) for t in ("starter", "growth", "authority"))
    s[5], s[50] = s[50], s[5]
    import_registry([{"id": f"SEO-{i + 1:03d}", "title": f"TEST item {i + 1}", "starter": s[i], "growth": g[i], "authority": a[i], "prerequisites": "needs access", "policy_notes": "no guarantees"} for i in range(100)], "test")
    from apps.subscriptions.models import FeatureDefinition

    return {f.feature_id: f for f in FeatureDefinition.objects.all()}


def _auth(raw):
    return {"HTTP_AUTHORIZATION": f"Agent {raw}"}


def _first(registry, package, code):
    return next(f.feature_id for f in registry.values() if f.code_for(package) == code)


class TestAgentReads:
    def test_plan_shows_what_is_included_and_hides_staff_fields(self, client, business, keys):
        business.subscription.internal_notes = "secret note"
        business.subscription.external_reference = "pay_999"
        business.subscription.save()
        response = client.get(reverse("agent:plan", args=[business.id]), **_auth(keys["claude-daily"]))
        assert response.status_code == 200
        assert response.data["package"] == "starter" and response.data["staff"] is None
        levels = {c["key"]: c["level"] for c in response.data["capabilities"]}
        assert levels["offsite_articles"] == "not_included" and levels["dashboard_baselines"] == "included"
        body = json.dumps(response.data).lower()
        for word in ("secret note", "pay_999", "minute", "overhead", "budget"):
            assert word not in body

    def test_checklist_includes_eligibility_prerequisites_and_policy_notes(self, client, business, keys, registry):
        response = client.get(reverse("agent:checklist", args=[business.id]), **_auth(keys["codex-nightly"]))
        assert response.status_code == 200 and response.data["total"] == 100
        row = response.data["rows"][0]
        assert row["eligibility"] in "ICANX" and row["prerequisites"] == "needs access" and row["policy_notes"] == "no guarantees"

    def test_benefits_are_readable(self, client, business, keys):
        states = {b["key"]: b["state"] for b in client.get(reverse("agent:benefits", args=[business.id]), **_auth(keys["claude-daily"])).data}
        assert states == {"hosting": "off", "reputation": "off"}

    def test_a_login_token_cannot_use_these_endpoints(self, client, business, admin_user):
        access = client.post(reverse("accounts:login"), {"email": "boss@agency.com", "password": "SuperSecret123!"}, format="json").data["access"]
        for name in ("plan", "checklist", "benefits", "scope-requests"):
            assert client.get(reverse(f"agent:{name}", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access}").status_code == 401


class TestAgentWritesChecklist:
    def _put(self, client, key, business, feature_id, **body):
        return client.put(reverse("agent:checklist-item", args=[business.id, feature_id]), body, format="json", **_auth(key))

    def test_update_is_attributed_to_the_agent_not_a_person(self, client, business, keys, registry):
        fid = _first(registry, "starter", "I")
        response = self._put(client, keys["claude-daily"], business, fid, status="verified", finding="Title tags present on all pages")
        assert response.status_code == 200 and response.data["status"] == "verified"
        change = ChecklistStatusChange.objects.get()
        assert change.actor is None and change.agent_name == "claude-daily"
        event = Event.objects.get(business=business, kind="checklist.updated")
        assert event.actor is None and "claude-daily" in event.summary and event.metadata["agent"] == "claude-daily"

    def test_claude_and_codex_use_the_same_endpoint_and_are_told_apart(self, client, business, keys, registry):
        fid = _first(registry, "starter", "I")
        self._put(client, keys["claude-daily"], business, fid, status="verified", finding="Checked")
        self._put(client, keys["codex-nightly"], business, fid, status="done", finding="Fixed", evidence="https://proof.example/1")
        assert list(ChecklistStatusChange.objects.order_by("id").values_list("agent_name", flat=True)) == ["claude-daily", "codex-nightly"]

    def test_the_same_rules_as_for_people_apply(self, client, business, keys, registry):
        fid, excluded = _first(registry, "starter", "I"), _first(registry, "starter", "N")
        key = keys["claude-daily"]
        assert self._put(client, key, business, fid, status="verified").data["code"] == "finding_required"
        assert self._put(client, key, business, fid, status="done", finding="x").data["code"] == "evidence_required"
        assert self._put(client, key, business, fid, status="blocked", blocker="b").data["code"] == "blocker_details_required"
        assert self._put(client, key, business, excluded, status="in_progress").data["code"] == "outside_package"

    def test_an_unchanged_repeat_adds_nothing(self, client, business, keys, registry):
        fid = _first(registry, "starter", "I")
        body = {"status": "verified", "finding": "Same"}
        self._put(client, keys["claude-daily"], business, fid, **body)
        self._put(client, keys["claude-daily"], business, fid, **body)
        assert ChecklistStatusChange.objects.count() == 1
        assert Event.objects.filter(business=business, kind="checklist.updated").count() == 1

    def test_daily_cap_stops_a_looping_agent(self, client, business, keys, registry, settings):
        settings.AGENT_MAX_CHECKLIST_UPDATES_PER_BUSINESS_PER_DAY = 2
        ids = [f.feature_id for f in registry.values() if f.starter_code == "I"][:3]
        assert self._put(client, keys["claude-daily"], business, ids[0], status="verified", finding="a").status_code == 200
        assert self._put(client, keys["claude-daily"], business, ids[1], status="verified", finding="b").status_code == 200
        assert self._put(client, keys["claude-daily"], business, ids[2], status="verified", finding="c").status_code == 429

    def test_inactive_subscription_and_paused_business_are_refused(self, client, business, keys, registry):
        fid = _first(registry, "starter", "I")
        Subscription.objects.filter(business=business).update(status="cancelled")
        assert self._put(client, keys["claude-daily"], business, fid, status="verified", finding="x").status_code == 403
        Subscription.objects.filter(business=business).update(status="active")
        Business.objects.filter(pk=business.pk).update(is_paused=True)
        assert self._put(client, keys["claude-daily"], business, fid, status="verified", finding="x").status_code == 400

    def test_history_names_the_agent(self, client, business, keys, registry, admin_user):
        fid = _first(registry, "starter", "I")
        self._put(client, keys["claude-daily"], business, fid, status="verified", finding="Checked")
        client.force_authenticate(admin_user)
        history = client.get(reverse("subscriptions:checklist-item", args=[business.id, fid])).data
        assert history[0]["actor"] == "agent: claude-daily"


class TestAgentScopeRequests:
    def test_agent_can_ask_and_it_is_attributed(self, client, business, keys):
        response = client.post(reverse("agent:scope-requests", args=[business.id]), {"capability": "SEO-060", "note": "Competitor gap suggests articles"}, format="json", **_auth(keys["codex-nightly"]))
        assert response.status_code == 201 and response.data["requested_by"] == "agent: codex-nightly"
        assert ScopeChangeRequest.objects.get().status == "open"
        assert client.post(reverse("agent:scope-requests", args=[business.id]), {"capability": "SEO-060"}, format="json", **_auth(keys["codex-nightly"])).status_code == 409

    def test_asking_for_something_already_included_is_refused(self, client, business, keys):
        response = client.post(reverse("agent:scope-requests", args=[business.id]), {"capability": "dashboard_baselines"}, format="json", **_auth(keys["claude-daily"]))
        assert response.status_code == 400 and response.data["code"] == "already_included"


class TestAgentCannotDoWhatOnlyPeopleDo:
    """None of these routes accept an agent key: approving, granting scope, consent and subscriptions are human-only."""

    def test_agent_key_is_rejected_on_every_human_only_route(self, client, business, keys):
        key = _auth(keys["claude-daily"])
        b = business.id
        assert client.put(reverse("subscriptions:subscription", args=[b]), {"package": "authority"}, format="json", **key).status_code == 401
        assert client.post(reverse("subscriptions:benefit-decision", args=[b, "reputation"]), {"decision": "declined"}, format="json", **key).status_code == 401
        assert client.post(reverse("subscriptions:scope-decision", args=[b, 1]), {"decision": "approve", "grant": True}, format="json", **key).status_code == 401
        assert client.post(reverse("subscriptions:plan-changes", args=[b]), {"requested_package": "growth"}, format="json", **key).status_code == 401
        assert client.put(reverse("subscriptions:checklist-item", args=[b, "SEO-001"]), {"status": "done"}, format="json", **key).status_code == 401
        business.subscription.refresh_from_db()
        assert business.subscription.package == "starter"
