import json

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from rest_framework.test import APIClient

from apps.businesses.models import Account, Business, Membership

from .checklist import EXPECTED_TOTALS, ImportInvalid, import_registry, validate_rows
from .consent import consent_state
from .entitlements import entitlement
from .models import ChecklistItem, FeatureDefinition, Subscription

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def admin():
    return User.objects.create_user(email="admin@example.com", password="x", is_agency_admin=True)


def _customer(email="c@example.com"):
    user = User.objects.create_user(email=email, password="x")
    account = Account.objects.create(name=f"{email} co")
    Membership.objects.create(account=account, user=user, role=Membership.Role.VIEWER)
    return user, account


def _site(account, package="starter", status="active"):
    business = Business.objects.create(account=account, name="Site", website="https://example.com")
    if package:
        Subscription.objects.create(business=business, package=package, status=status)
    return business


# --- consent -------------------------------------------------------------------------------------------------------

class TestConsent:
    def _post(self, client, site, benefit, **body):
        return client.post(reverse("subscriptions:benefit-decision", args=[site.id, benefit]), body, format="json")

    def test_nothing_is_on_by_default(self, client):
        user, account = _customer()
        site = _site(account)
        client.force_authenticate(user)
        states = {b["key"]: b["state"] for b in client.get(reverse("subscriptions:benefits", args=[site.id])).data}
        assert states == {"hosting": "off", "reputation": "off"}

    def test_declining_hosting_changes_nothing_else(self, client, admin):  # AC07
        _, account = _customer()
        site = _site(account)
        client.force_authenticate(admin)
        assert self._post(client, site, "hosting", decision="declined", note="Keep my current host").status_code == 201
        site = Business.objects.get(pk=site.pk)
        assert consent_state(site, "hosting") == "declined"
        assert site.subscription.status == "active"
        assert entitlement(site, "dashboard_baselines").can_execute is True
        assert entitlement(site, "hosting").can_execute is False

    def test_accepting_hosting_needs_who_and_evidence(self, client, admin):
        _, account = _customer()
        site = _site(account)
        client.force_authenticate(admin)
        assert self._post(client, site, "hosting", decision="accepted").data["code"] == "missing_consenting_person"
        assert self._post(client, site, "hosting", decision="accepted", consented_by_name="Sam").data["code"] == "missing_evidence"
        assert self._post(client, site, "hosting", decision="accepted", consented_by_name="Sam", evidence="Email 3 Nov").status_code == 201
        assert entitlement(Business.objects.get(pk=site.pk), "hosting").can_execute is True

    def test_reputation_needs_terms_and_authority(self, client, admin):  # AC09
        _, account = _customer()
        site = _site(account)
        client.force_authenticate(admin)
        base = {"decision": "accepted", "consented_by_name": "Sam", "evidence": "Email"}
        assert self._post(client, site, "reputation", **base).data["code"] == "terms_not_accepted"
        assert self._post(client, site, "reputation", **base, terms_accepted=True, terms_reference="Review terms v1").data["code"] == "authority_not_confirmed"
        assert entitlement(Business.objects.get(pk=site.pk), "reputation_management").can_execute is False
        ok = self._post(client, site, "reputation", **base, terms_accepted=True, terms_reference="Review terms v1", workflow_authority_confirmed=True)
        assert ok.status_code == 201
        assert entitlement(Business.objects.get(pk=site.pk), "reputation_management").can_execute is True

    def test_revoking_turns_it_off_and_keeps_the_trail(self, client, admin):
        _, account = _customer()
        site = _site(account)
        client.force_authenticate(admin)
        self._post(client, site, "hosting", decision="accepted", consented_by_name="Sam", evidence="Email")
        assert self._post(client, site, "hosting", decision="revoked").status_code == 201
        site = Business.objects.get(pk=site.pk)
        assert consent_state(site, "hosting") == "revoked"
        assert entitlement(site, "hosting").can_execute is False
        assert site.consent_records.count() == 2

    def test_cannot_accept_twice_or_revoke_when_off(self, client, admin):
        _, account = _customer()
        site = _site(account)
        client.force_authenticate(admin)
        assert self._post(client, site, "hosting", decision="revoked").status_code == 409
        self._post(client, site, "hosting", decision="accepted", consented_by_name="Sam", evidence="Email")
        assert self._post(client, site, "hosting", decision="accepted", consented_by_name="Sam", evidence="Email").status_code == 409

    def test_after_cancellation_hosting_needs_its_own_explicit_yes(self, client, admin):  # AC08
        _, account = _customer()
        active = _site(account)
        client.force_authenticate(admin)
        assert self._post(client, active, "hosting_after_cancellation", decision="accepted", consented_by_name="Sam", evidence="E").data["code"] == "not_cancelled"
        _, other = _customer("d@example.com")
        cancelled = _site(other, status="cancelled")
        keys = [b["key"] for b in client.get(reverse("subscriptions:benefits", args=[cancelled.id])).data]
        assert "hosting_after_cancellation" in keys and "hosting_after_cancellation" not in [b["key"] for b in client.get(reverse("subscriptions:benefits", args=[active.id])).data]
        assert consent_state(cancelled, "hosting_after_cancellation") == "off"  # offered, never started automatically
        assert self._post(client, cancelled, "hosting_after_cancellation", decision="accepted", consented_by_name="Sam", evidence="E").data["code"] == "charge_not_acknowledged"
        assert self._post(client, cancelled, "hosting_after_cancellation", decision="accepted", consented_by_name="Sam", evidence="E", recurring_charge_acknowledged=True).status_code == 201

    def test_customer_cannot_record_and_does_not_see_evidence(self, client, admin):
        user, account = _customer()
        site = _site(account)
        client.force_authenticate(admin)
        self._post(client, site, "hosting", decision="accepted", consented_by_name="Sam", evidence="secret evidence")
        client.force_authenticate(user)
        assert self._post(client, site, "hosting", decision="declined").status_code == 403
        body = json.dumps(client.get(reverse("subscriptions:benefits", args=[site.id])).data)
        assert "secret evidence" not in body and "Sam" not in body

    def test_other_customers_site_is_not_found(self, client):  # AC01
        user, _ = _customer()
        _, other = _customer("d@example.com")
        client.force_authenticate(user)
        assert client.get(reverse("subscriptions:benefits", args=[_site(other).id])).status_code == 404

    def test_unknown_benefit_rejected(self, client, admin):
        _, account = _customer()
        site = _site(account)
        client.force_authenticate(admin)
        assert self._post(client, site, "magic", decision="accepted").data["code"] == "unknown_benefit"


# --- checklist import ----------------------------------------------------------------------------------------------

def _rows_matching_totals():
    """100 synthetic rows whose tier counts equal the handoff's totals. Titles say TEST: this is not the real registry."""
    def codes(total):
        return [c for c in "ICANX" for _ in range(total[c])]

    s, g, a = (codes(EXPECTED_TOTALS[t]) for t in ("starter", "growth", "authority"))
    s[5], s[50] = s[50], s[5]  # SEO-006 is already conditional for Starter, so the registry override is a no-op here
    return [{"id": f"SEO-{i + 1:03d}", "title": f"TEST item {i + 1}", "starter": s[i], "growth": g[i], "authority": a[i]} for i in range(100)]


class TestImport:
    def test_valid_registry_imports_exactly_100(self):  # AC26
        assert import_registry(_rows_matching_totals(), "v11") == 100
        assert FeatureDefinition.objects.count() == 100
        assert FeatureDefinition.objects.get(feature_id="SEO-030").category == "On-page and content"

    def test_wrong_count_duplicates_and_extras_are_refused(self):
        rows = _rows_matching_totals()
        assert any("exactly 100" in p for p in validate_rows(rows[:99]))
        dup = rows[:99] + [dict(rows[0])]
        assert any("Duplicate" in p for p in validate_rows(dup))
        extra = rows[:99] + [{"id": "HOST-001", "title": "Hosting", "starter": "I", "growth": "I", "authority": "I"}]
        assert any("HOST-001" in p for p in validate_rows(extra))

    def test_bad_code_and_wrong_totals_are_refused_and_nothing_is_written(self):
        rows = _rows_matching_totals()
        rows[5]["growth"] = "Z"
        with pytest.raises(ImportInvalid):
            import_registry(rows, "v11")
        rows = _rows_matching_totals()
        rows[0]["starter"] = "N" if rows[0]["starter"] != "N" else "I"
        with pytest.raises(ImportInvalid) as error:
            import_registry(rows, "v11")
        assert any("totals" in p for p in error.value.problems)
        assert FeatureDefinition.objects.count() == 0

    def test_command_reports_problems(self, tmp_path):
        path = tmp_path / "r.json"
        path.write_text(json.dumps(_rows_matching_totals()[:10]))
        with pytest.raises(CommandError):
            call_command("import_checklist", str(path))
        path.write_text(json.dumps(_rows_matching_totals()))
        call_command("import_checklist", str(path))
        assert FeatureDefinition.objects.count() == 100


# --- checklist behaviour -------------------------------------------------------------------------------------------

@pytest.fixture
def registry():
    import_registry(_rows_matching_totals(), "v11")
    return {f.feature_id: f for f in FeatureDefinition.objects.all()}


def _find(registry, package, code):
    return next(f.feature_id for f in registry.values() if f.code_for(package) == code)


class TestChecklist:
    def _put(self, client, site, feature_id, **body):
        return client.put(reverse("subscriptions:checklist-item", args=[site.id, feature_id]), body, format="json")

    def test_unchecked_items_are_not_checked_not_missing(self, client, registry):
        user, account = _customer()
        site = _site(account)
        client.force_authenticate(user)
        data = client.get(reverse("subscriptions:checklist", args=[site.id])).data
        assert data["loaded"] and data["total"] == 100
        assert data["summary"]["not_checked"] == 100
        assert {r["status"] for r in data["rows"]} == {"not_checked"}

    def test_registry_not_loaded_is_reported(self, client):
        user, account = _customer()
        site = _site(account)
        client.force_authenticate(user)
        assert client.get(reverse("subscriptions:checklist", args=[site.id])).data["loaded"] is False

    def test_checked_needs_a_finding_and_done_needs_evidence(self, client, admin, registry):
        _, account = _customer()
        site = _site(account)
        fid = _find(registry, "starter", "I")
        client.force_authenticate(admin)
        assert self._put(client, site, fid, status="verified").data["code"] == "finding_required"
        assert self._put(client, site, fid, status="verified", finding="HTTPS works on all pages").status_code == 200
        assert self._put(client, site, fid, status="done").data["code"] == "evidence_required"
        assert self._put(client, site, fid, status="done", evidence="https://proof.example/1").data["status"] == "done"

    def test_blocked_needs_blocker_owner_and_next_action(self, client, admin, registry):
        _, account = _customer()
        site = _site(account)
        fid = _find(registry, "starter", "I")
        client.force_authenticate(admin)
        assert self._put(client, site, fid, status="blocked", blocker="No login").data["code"] == "blocker_details_required"
        ok = self._put(client, site, fid, status="blocked", blocker="No login", owner="Customer", next_action="Send CMS login")
        assert ok.data["status"] == "blocked" and ok.data["blocker"] == "No login"

    def test_not_applicable_needs_a_reason(self, client, admin, registry):
        _, account = _customer()
        site = _site(account)
        fid = _find(registry, "starter", "I")
        client.force_authenticate(admin)
        assert self._put(client, site, fid, status="n_a").data["code"] == "rationale_required"
        assert self._put(client, site, fid, status="n_a", reason="No shop on this site").status_code == 200

    def test_items_outside_the_package_cannot_be_worked(self, client, admin, registry):  # AC04
        _, account = _customer()
        site = _site(account, "starter")
        fid = _find(registry, "starter", "N")
        client.force_authenticate(admin)
        assert self._put(client, site, fid, status="in_progress").data["code"] == "outside_package"
        assert self._put(client, site, fid, status="out_of_package", finding="Found while auditing").status_code == 200

    def test_reopening_keeps_earlier_completion_in_history(self, client, admin, registry):
        _, account = _customer()
        site = _site(account)
        fid = _find(registry, "starter", "I")
        client.force_authenticate(admin)
        self._put(client, site, fid, status="done", finding="Fixed", evidence="proof")
        self._put(client, site, fid, status="needs_work", reason="Reassessed: broke again")
        history = client.get(reverse("subscriptions:checklist-item", args=[site.id, fid])).data
        assert [(h["from_status"], h["to_status"]) for h in history] == [("done", "needs_work"), ("not_checked", "done")]

    def test_package_change_updates_eligibility_not_history(self, client, admin, registry):  # AC13
        _, account = _customer()
        site = _site(account, "authority")
        fid = _find(registry, "authority", "I")
        client.force_authenticate(admin)
        self._put(client, site, fid, status="verified", finding="Checked")
        Subscription.objects.filter(business=site).update(package="starter")
        row = next(r for r in client.get(reverse("subscriptions:checklist", args=[site.id])).data["rows"] if r["feature_id"] == fid)
        assert row["eligibility"] == registry[fid].starter_code and row["status"] == "verified"

    def test_customer_cannot_write_and_never_sees_evidence(self, client, admin, registry):
        user, account = _customer()
        site = _site(account)
        fid = _find(registry, "starter", "I")
        client.force_authenticate(admin)
        self._put(client, site, fid, status="done", finding="Fixed", evidence="private-proof-link")
        client.force_authenticate(user)
        assert self._put(client, site, fid, status="done").status_code == 403
        body = json.dumps(client.get(reverse("subscriptions:checklist", args=[site.id])).data)
        assert "private-proof-link" not in body
        for word in ("minute", "overhead", "budget"):
            assert word not in body.lower()

    def test_no_package_cannot_be_worked(self, client, admin, registry):
        _, account = _customer()
        site = _site(account, None)
        client.force_authenticate(admin)
        assert self._put(client, site, "SEO-001", status="verified", finding="x").data["code"] == "no_subscription"

    def test_other_customers_checklist_is_not_found(self, client, registry):  # AC01
        user, _ = _customer()
        _, other = _customer("d@example.com")
        client.force_authenticate(user)
        assert client.get(reverse("subscriptions:checklist", args=[_site(other).id])).status_code == 404


class TestClientExportFormat:
    def test_client_headers_are_understood_and_delivery_mode_kept(self, tmp_path):
        import csv as csv_module

        rows = _rows_matching_totals()
        path = tmp_path / "export.csv"
        with path.open("w", newline="") as handle:
            writer = csv_module.writer(handle)
            writer.writerow(["ID", "Title", "Starter", "Growth", "Authority", "Delivery mode", "Prerequisites / limits", "Policy notes"])
            for row in rows:
                writer.writerow([row["id"], row["title"], row["starter"], row["growth"], row["authority"], "Recurring", "Needs access", "No guarantees"])
        call_command("import_checklist", str(path))
        feature = FeatureDefinition.objects.get(feature_id="SEO-005")
        assert (feature.delivery_mode, feature.prerequisites, feature.policy_notes) == ("Recurring", "Needs access", "No guarantees")

    def test_check_flag_writes_nothing(self, tmp_path):
        path = tmp_path / "r.json"
        path.write_text(json.dumps(_rows_matching_totals()))
        call_command("import_checklist", str(path), "--check")
        assert FeatureDefinition.objects.count() == 0

    def test_wrong_totals_refused_unless_explicitly_ignored(self, tmp_path):
        rows = _rows_matching_totals()
        rows[0]["starter"] = "N" if rows[0]["starter"] != "N" else "I"
        path = tmp_path / "r.json"
        path.write_text(json.dumps(rows))
        with pytest.raises(CommandError):
            call_command("import_checklist", str(path))
        assert FeatureDefinition.objects.count() == 0
        call_command("import_checklist", str(path), "--ignore-totals")
        assert FeatureDefinition.objects.count() == 100


class TestRealRegistryFile:
    def test_the_clients_file_loads_with_the_accepted_totals_and_the_seo_006_override(self):
        from pathlib import Path

        from django.conf import settings

        path = Path(settings.BASE_DIR) / "docs" / "checklist" / "SEO_Pilot_Package_Entitlements_SEO-001_to_SEO-100.csv"
        call_command("import_checklist", str(path))
        assert FeatureDefinition.objects.count() == 100
        assert FeatureDefinition.objects.get(feature_id="SEO-006").starter_code == "C"  # PDF is more cautious than the CSV
        assert FeatureDefinition.objects.get(feature_id="SEO-060").authority_code == "I"
        assert FeatureDefinition.objects.get(feature_id="SEO-060").starter_code == "N"
        assert not FeatureDefinition.objects.filter(feature_id__in=["HOST-001", "MSG-001"]).exists()
