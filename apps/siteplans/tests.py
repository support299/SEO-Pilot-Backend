import json
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.urls import reverse
from rest_framework.test import APIClient

from apps.agent.models import AgentKey
from apps.businesses.models import Account, Business, Membership
from apps.history.models import Event
from apps.subscriptions.models import ConsentRecord, Subscription

from .models import PlanTask, SitePlan

pytestmark = pytest.mark.django_db
User = get_user_model()
REGISTRY = Path(settings.BASE_DIR) / "docs" / "checklist" / "SEO_Pilot_Package_Entitlements_SEO-001_to_SEO-100.csv"


@pytest.fixture(autouse=True)
def registry():
    call_command("import_checklist", str(REGISTRY))


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def admin():
    return User.objects.create_user(email="arman@agency.com", password="SuperSecret123!", is_agency_admin=True)


@pytest.fixture
def key(admin):
    return AgentKey.issue(name="claude-daily", created_by=admin)[1]


@pytest.fixture
def customer():
    user = User.objects.create_user(email="owner@acme.com", password="SuperSecret123!")
    account = Account.objects.create(name="Acme")
    Membership.objects.create(account=account, user=user, role=Membership.Role.VIEWER)
    return user, account


@pytest.fixture
def site(customer):
    business = Business.objects.create(account=customer[1], name="Acme Gutters", website="https://acme.example")
    Subscription.objects.create(business=business, package="starter")
    return business


def A(key):
    return {"HTTP_AUTHORIZATION": f"Agent {key}"}


def _draft(client, key, site, summary="Fix the basics first"):
    return client.post(reverse("agent:site-plans", args=[site.id]), {"summary": summary}, format="json", **A(key))


def _task(client, key, site, plan_id, feature_id="SEO-026", **extra):
    body = {"feature_id": feature_id, "title": f"Work on {feature_id}", "completion_criteria": "Live page shows the change", "estimate_minutes": 30, **extra}
    return client.post(reverse("agent:site-plan-tasks", args=[site.id, plan_id]), body, format="json", **A(key))


def _submit(client, key, site, plan_id):
    return client.post(reverse("agent:site-plan-action", args=[site.id, plan_id, "submit"]), format="json", **A(key))


def _decide(client, admin, site, plan_id, decision, note=""):
    client.force_authenticate(admin)
    response = client.post(reverse("siteplans:decision", args=[site.id, plan_id]), {"decision": decision, "note": note}, format="json")
    client.force_authenticate(None)
    return response


def _approved_plan(client, key, admin, site, *features):
    plan_id = _draft(client, key, site).data["id"]
    for feature_id, extra in features or (("SEO-026", {}),):
        assert _task(client, key, site, plan_id, feature_id, **extra).status_code == 201
    assert _submit(client, key, site, plan_id).status_code == 200
    assert _decide(client, admin, site, plan_id, "approve").status_code == 200
    return plan_id


class TestDrafting:
    def test_agent_drafts_and_it_is_attributed(self, client, key, site):
        response = _draft(client, key, site)
        assert response.status_code == 201 and response.data["version"] == 1 and response.data["status"] == "draft"
        assert response.data["created_by"] == "agent: claude-daily"
        assert Event.objects.get(kind="siteplan.drafted").metadata["agent"] == "claude-daily"

    def test_only_one_open_draft_per_site(self, client, key, site):
        _draft(client, key, site)
        assert _draft(client, key, site).status_code == 409

    def test_no_active_package_no_plan(self, client, key, site):
        Subscription.objects.filter(business=site).update(status="cancelled")
        assert _draft(client, key, site).data["code"] == "no_active_subscription"

    def test_tasks_must_fit_the_package(self, client, key, site):  # AC04, AC05
        plan_id = _draft(client, key, site).data["id"]
        assert _task(client, key, site, plan_id, "SEO-026").status_code == 201  # included
        assert _task(client, key, site, plan_id, "SEO-060").data["code"] == "outside_package"  # articles: Authority only
        assert _task(client, key, site, plan_id, "SEO-017").data["code"] == "not_applicable"  # hreflang: X
        assert _task(client, key, site, plan_id, "SEO-047").data["code"] == "needs_consent"  # reputation is opt-in
        assert _task(client, key, site, plan_id, "SEO-030").data["code"] == "justification_required"  # conditional for Starter
        assert _task(client, key, site, plan_id, "SEO-030", scope_justification="Client asked in writing to scope their 3 service pages").status_code == 201
        assert _task(client, key, site, plan_id, "SEO-999").data["code"] == "unknown_feature"

    def test_reputation_tasks_allowed_once_the_customer_opted_in(self, client, key, site):
        ConsentRecord.objects.create(business=site, benefit="reputation", decision="accepted", consented_by_name="Sam", evidence="email")
        plan_id = _draft(client, key, site).data["id"]
        assert _task(client, key, site, plan_id, "SEO-047").status_code == 201

    def test_a_granted_scope_change_unlocks_the_item(self, client, key, site):
        Subscription.objects.filter(business=site).update(granted_capabilities=["offsite_articles"])
        plan_id = _draft(client, key, site).data["id"]
        assert _task(client, key, site, plan_id, "SEO-060").status_code == 201

    def test_draft_task_can_be_edited_and_deleted_but_progress_waits_for_approval(self, client, key, site):
        plan_id = _draft(client, key, site).data["id"]
        task_id = _task(client, key, site, plan_id).data["id"]
        url = reverse("agent:site-plan-task", args=[site.id, task_id])
        assert client.patch(url, {"title": "Rewrite homepage title", "priority": "high"}, format="json", **A(key)).data["priority"] == "high"
        assert client.patch(url, {"status": "done"}, format="json", **A(key)).status_code == 409
        assert client.patch(url, {"planned_start": "2026-12-10", "planned_end": "2026-12-01"}, format="json", **A(key)).data["code"] == "bad_dates"
        assert client.delete(url, **A(key)).status_code == 204

    def test_submit_needs_tasks_and_a_summary(self, client, key, site):
        plan_id = _draft(client, key, site, summary="").data["id"]
        assert _submit(client, key, site, plan_id).data["code"] == "no_tasks"
        _task(client, key, site, plan_id)
        assert _submit(client, key, site, plan_id).data["code"] == "summary_required"
        client.patch(reverse("agent:site-plan", args=[site.id, plan_id]), {"summary": "Basics first"}, format="json", **A(key))
        assert _submit(client, key, site, plan_id).data["status"] == "submitted"

    def test_submitted_plan_is_frozen_for_the_agent(self, client, key, site):
        plan_id = _draft(client, key, site).data["id"]
        task_id = _task(client, key, site, plan_id).data["id"]
        _submit(client, key, site, plan_id)
        assert _task(client, key, site, plan_id, "SEO-027").status_code == 409
        assert client.patch(reverse("agent:site-plan-task", args=[site.id, task_id]), {"title": "x"}, format="json", **A(key)).status_code == 409

    def test_unknown_action_is_404(self, client, key, site):
        plan_id = _draft(client, key, site).data["id"]
        assert client.post(reverse("agent:site-plan-action", args=[site.id, plan_id, "approve"]), format="json", **A(key)).status_code == 404


class TestDeciding:
    def test_agent_key_and_customer_cannot_decide(self, client, key, site, customer):
        plan_id = _draft(client, key, site).data["id"]
        _task(client, key, site, plan_id)
        _submit(client, key, site, plan_id)
        url = reverse("siteplans:decision", args=[site.id, plan_id])
        assert client.post(url, {"decision": "approve"}, format="json", **A(key)).status_code == 401
        client.force_authenticate(customer[0])
        assert client.post(url, {"decision": "approve"}, format="json").status_code == 403
        assert SitePlan.objects.get(pk=plan_id).status == "submitted"

    def test_approve_makes_it_active(self, client, key, admin, site):
        plan_id = _approved_plan(client, key, admin, site)
        plan = SitePlan.objects.get(pk=plan_id)
        assert plan.status == "active" and plan.decided_by == admin and plan.effective_from is not None

    def test_request_changes_needs_a_note_and_returns_it_to_the_agent(self, client, key, admin, site):
        plan_id = _draft(client, key, site).data["id"]
        _task(client, key, site, plan_id)
        _submit(client, key, site, plan_id)
        assert _decide(client, admin, site, plan_id, "request_changes").data["code"] == "note_required"
        assert _decide(client, admin, site, plan_id, "request_changes", "Do the GBP fixes first").status_code == 200
        workspace = client.get(reverse("agent:site-plans", args=[site.id]), **A(key)).data
        assert workspace["open"]["status"] == "draft" and workspace["open"]["decision_note"] == "Do the GBP fixes first"
        assert _task(client, key, site, plan_id, "SEO-043").status_code == 201  # editable again

    def test_reject_closes_it_and_a_new_draft_can_start(self, client, key, admin, site):
        plan_id = _draft(client, key, site).data["id"]
        _task(client, key, site, plan_id)
        _submit(client, key, site, plan_id)
        assert _decide(client, admin, site, plan_id, "reject", "Wrong priorities").status_code == 200
        assert SitePlan.objects.get(pk=plan_id).status == "rejected"
        assert _draft(client, key, site).data["version"] == 2

    def test_approval_rechecks_the_package(self, client, key, admin, site):
        Subscription.objects.filter(business=site).update(package="authority")
        plan_id = _draft(client, key, site).data["id"]
        _task(client, key, site, plan_id, "SEO-060")
        _submit(client, key, site, plan_id)
        Subscription.objects.filter(business=site).update(package="starter")  # downgraded while waiting
        assert _decide(client, admin, site, plan_id, "approve").data["code"] == "outside_package"


class TestProgress:
    def _patch(self, client, key, site, task_id, **body):
        return client.patch(reverse("agent:site-plan-task", args=[site.id, task_id]), body, format="json", **A(key))

    def test_progress_rules(self, client, key, admin, site):
        plan_id = _approved_plan(client, key, admin, site)
        task_id = PlanTask.objects.get(plan_id=plan_id).id
        assert self._patch(client, key, site, task_id, title="new").data["code"] == "plan_is_approved"
        assert self._patch(client, key, site, task_id, status="in_progress").data["status"] == "in_progress"
        assert self._patch(client, key, site, task_id, status="waiting").data["code"] == "note_required"
        assert self._patch(client, key, site, task_id, status="waiting", status_note="Waiting for CMS login").status_code == 200
        assert self._patch(client, key, site, task_id, status="done").data["code"] == "evidence_required"
        assert self._patch(client, key, site, task_id, status="done", completion_evidence="https://acme.example/ title updated").data["status"] == "done"

    def test_retry_adds_nothing(self, client, key, admin, site):
        plan_id = _approved_plan(client, key, admin, site)
        task_id = PlanTask.objects.get(plan_id=plan_id).id
        self._patch(client, key, site, task_id, status="in_progress")
        self._patch(client, key, site, task_id, status="in_progress")
        assert Event.objects.filter(kind="siteplan.task_updated").count() == 1

    def test_downgrade_stops_ineligible_work_but_keeps_the_task(self, client, key, admin, site, customer):  # AC13
        Subscription.objects.filter(business=site).update(package="growth")
        plan_id = _approved_plan(client, key, admin, site, ("SEO-031", {"scope_justification": "Real branch in Leeds"}))
        task_id = PlanTask.objects.get(plan_id=plan_id).id
        Subscription.objects.filter(business=site).update(package="starter")  # city pages are not in Starter
        assert self._patch(client, key, site, task_id, status="in_progress").data["code"] == "outside_package"
        client.force_authenticate(customer[0])
        assert client.get(reverse("siteplans:plans", args=[site.id])).data["active"]["tasks"][0]["feature_id"] == "SEO-031"

    def test_staff_can_record_progress_too(self, client, key, admin, site):
        plan_id = _approved_plan(client, key, admin, site)
        task_id = PlanTask.objects.get(plan_id=plan_id).id
        client.force_authenticate(admin)
        response = client.patch(reverse("siteplans:task-progress", args=[site.id, task_id]), {"status": "in_progress"}, format="json")
        assert response.status_code == 200 and response.data["status"] == "in_progress"


class TestVersions:
    def test_new_version_carries_unfinished_tasks_and_supersedes_on_approval(self, client, key, admin, site):
        v1 = _approved_plan(client, key, admin, site, ("SEO-026", {}), ("SEO-027", {}))
        done, open_task = PlanTask.objects.filter(plan_id=v1).order_by("id")
        client.patch(reverse("agent:site-plan-task", args=[site.id, done.id]), {"status": "done", "completion_evidence": "proof"}, format="json", **A(key))
        v2 = _draft(client, key, site).data
        assert [t["feature_id"] for t in v2["tasks"]] == ["SEO-027"] and v2["tasks"][0]["carried_from"] == open_task.id
        _submit(client, key, site, v2["id"])
        _decide(client, admin, site, v2["id"], "approve")
        assert SitePlan.objects.get(pk=v1).status == "superseded"
        assert PlanTask.objects.get(pk=done.id).status == "done"  # history untouched


class TestVisibility:
    def test_customer_sees_the_active_plan_without_internal_fields(self, client, key, admin, site, customer):  # AC03
        _approved_plan(client, key, admin, site, ("SEO-030", {"scope_justification": "secret justification"}))
        client.force_authenticate(customer[0])
        data = client.get(reverse("siteplans:plans", args=[site.id])).data
        assert set(data) == {"active"} and data["active"]["tasks"][0]["title"] == "Work on SEO-030"
        body = json.dumps(data).lower()
        for word in ("minute", "estimate", "evidence", "secret justification", "claude", "agent", "risk"):
            assert word not in body

    def test_customer_does_not_see_drafts(self, client, key, site, customer):
        _draft(client, key, site)
        client.force_authenticate(customer[0])
        assert client.get(reverse("siteplans:plans", args=[site.id])).data == {"active": None}

    def test_other_customer_gets_404(self, client, key, site):
        stranger = User.objects.create_user(email="x@y.com", password="SuperSecret123!")
        Membership.objects.create(account=Account.objects.create(name="Other"), user=stranger, role="viewer")
        client.force_authenticate(stranger)
        assert client.get(reverse("siteplans:plans", args=[site.id])).status_code == 404

    def test_staff_sees_estimates_and_eligibility(self, client, key, admin, site):
        plan_id = _draft(client, key, site).data["id"]
        _task(client, key, site, plan_id)
        client.force_authenticate(admin)
        open_plan = client.get(reverse("siteplans:plans", args=[site.id])).data["open"]
        assert open_plan["estimate_minutes_total"] == 30 and open_plan["tasks"][0]["eligibility"]["ok"] is True


class TestLimits:
    def test_daily_cap(self, client, key, site, settings):
        settings.AGENT_MAX_PLAN_WRITES_PER_BUSINESS_PER_DAY = 1
        assert _draft(client, key, site).status_code == 201
        plan_id = SitePlan.objects.get().id
        assert _task(client, key, site, plan_id).status_code == 429  # once the cap is reached every agent write waits a day
        assert _submit(client, key, site, plan_id).status_code == 429

    def test_paused_business_refused(self, client, key, site):
        Business.objects.filter(pk=site.pk).update(is_paused=True)
        assert _draft(client, key, site).status_code == 400
