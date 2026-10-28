import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APIClient

from apps.businesses.models import Account, Business
from apps.history.models import Event
from apps.wordpress.models import ChangePacket, WordPressConnection
from integrations.wordpress.client import WpObject

from .models import AgentKey

pytestmark = pytest.mark.django_db


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def admin_user():
    return get_user_model().objects.create_user(email="boss@agency.com", password="SuperSecret123!", is_agency_admin=True)


@pytest.fixture
def business():
    account = Account.objects.create(name="Acme")
    return Business.objects.create(account=account, name="Acme Gutters", website="https://acme.example")


@pytest.fixture
def agent_key(admin_user):
    _key, raw = AgentKey.issue(name="claude-test", created_by=admin_user)
    return raw


def _auth(raw):
    return {"HTTP_AUTHORIZATION": f"Agent {raw}"}


def _wp_object(id=7, title="Old title"):
    return WpObject(id=id, title=title, slug="p", status="publish", link="https://acme.example/p", modified_gmt="2026-01-01T00:00:00", meta={})


@pytest.fixture
def wp_ready(monkeypatch, business):
    WordPressConnection.objects.create(
        business=business, website_url="https://acme.example/", api_url="https://acme.example/wp-json", wp_user_name="a", application_password_enc="x", status="connected"
    )
    monkeypatch.setattr("apps.wordpress.services._credentials", lambda connection: ("a", "b"))
    monkeypatch.setattr("apps.wordpress.services.wp.fetch_object", lambda *a, **kw: _wp_object())
    monkeypatch.setattr("apps.wordpress.services.wp.list_objects", lambda *a, **kw: [_wp_object()])


def _propose(client, key, business, **overrides):
    body = {
        "target_object_type": "page",
        "target_object_id": 7,
        "proposed_changes": {"meta_description": "A clear description of what we offer."},
        "reason": "Missing description",
    }
    body.update(overrides)
    return client.post(reverse("agent:proposals", args=[business.id]), body, format="json", **_auth(key))


class TestAuthentication:
    def test_no_key_is_rejected(self, client, business):
        assert client.get(reverse("agent:context", args=[business.id])).status_code == 401

    def test_wrong_key_is_rejected(self, client, business):
        assert client.get(reverse("agent:context", args=[business.id]), **_auth("sp_agent_nope")).status_code == 401

    def test_revoked_key_is_rejected(self, client, business, agent_key):
        from django.utils import timezone

        AgentKey.objects.update(revoked_at=timezone.now())
        assert client.get(reverse("agent:context", args=[business.id]), **_auth(agent_key)).status_code == 401

    def test_key_of_a_demoted_admin_stops_working(self, client, business, agent_key, admin_user):
        admin_user.is_agency_admin = False
        admin_user.save()
        assert client.get(reverse("agent:context", args=[business.id]), **_auth(agent_key)).status_code == 401

    def test_the_raw_key_is_never_stored(self, agent_key):
        assert agent_key not in AgentKey.objects.get().key_hash
        assert not AgentKey.objects.filter(key_hash=agent_key).exists()

    def test_a_normal_login_token_cannot_use_agent_endpoints(self, client, business, admin_user):
        access = client.post(reverse("accounts:login"), {"email": "boss@agency.com", "password": "SuperSecret123!"}, format="json").data["access"]
        assert client.get(reverse("agent:context", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access}").status_code == 401


class TestAgentCanReadAndPropose:
    def test_lists_businesses_and_reads_context(self, client, business, agent_key, wp_ready):
        listing = client.get(reverse("agent:businesses"), **_auth(agent_key))
        assert listing.status_code == 200
        assert listing.data[0]["name"] == "Acme Gutters" and listing.data[0]["wordpress_connected"] is True

        context = client.get(reverse("agent:context", args=[business.id]), **_auth(agent_key))
        assert context.status_code == 200
        assert {"business", "issues", "top_queries", "top_pages", "site_health", "open_proposals"} <= set(context.data)
        assert "pages" not in context.data["site_health"]

    def test_proposal_is_queued_for_approval_and_attributed_to_the_agent(self, client, business, agent_key, wp_ready):
        response = _propose(client, agent_key, business)

        assert response.status_code == 201
        packet = ChangePacket.objects.get()
        assert packet.status == ChangePacket.Status.AWAITING_APPROVAL  # never live until a person approves and applies
        assert packet.source == "agent" and packet.agent_name == "claude-test"
        event = Event.objects.get(business=business, kind="wordpress.change_proposed")
        assert event.actor is None and "claude-test" in event.summary

    def test_second_proposal_for_the_same_page_is_refused_while_one_is_open(self, client, business, agent_key, wp_ready):
        assert _propose(client, agent_key, business).status_code == 201
        assert _propose(client, agent_key, business).status_code == 409

    def test_daily_cap_is_enforced(self, client, business, agent_key, wp_ready, settings):
        settings.AGENT_MAX_PROPOSALS_PER_BUSINESS_PER_DAY = 1
        assert _propose(client, agent_key, business, target_object_id=7).status_code == 201
        assert _propose(client, agent_key, business, target_object_id=8).status_code == 429

    @pytest.mark.parametrize(
        "overrides",
        [
            {"proposed_changes": {}},
            {"proposed_changes": {"meta_description": "x" * 400}},
            {"proposed_changes": {"title": "x" * 300}},
            {"proposed_changes": {"title": 123}},
            {"target_object_type": "attachment"},
            {"target_object_id": "abc"},
            {"reason": "r" * 3000},
        ],
    )
    def test_bad_input_is_rejected_and_nothing_is_created(self, client, business, agent_key, wp_ready, overrides):
        assert _propose(client, agent_key, business, **overrides).status_code == 400
        assert ChangePacket.objects.count() == 0


class TestAgentCannotDecideOrApply:
    def test_a_key_cannot_approve_apply_or_roll_back(self, client, business, agent_key, wp_ready):
        packet_id = _propose(client, agent_key, business).data["id"]
        for name in ("change-packet-decision", "change-packet-apply", "change-packet-rollback"):
            response = client.post(reverse(f"wordpress:{name}", args=[business.id, packet_id]), {"approve": True}, format="json", **_auth(agent_key))
            assert response.status_code == 401, name
        assert ChangePacket.objects.get().status == ChangePacket.Status.AWAITING_APPROVAL

    def test_a_key_cannot_connect_or_change_a_customers_wordpress(self, client, business, agent_key):
        response = client.post(reverse("wordpress:connect", args=[business.id]), {"website_url": "x"}, format="json", **_auth(agent_key))
        assert response.status_code == 401


class TestAgentWorkflowEndToEnd:
    def test_admin_approves_what_the_agent_proposed(self, client, business, agent_key, wp_ready, admin_user):
        packet_id = _propose(client, agent_key, business).data["id"]
        access = client.post(reverse("accounts:login"), {"email": "boss@agency.com", "password": "SuperSecret123!"}, format="json").data["access"]

        response = client.post(
            reverse("wordpress:change-packet-decision", args=[business.id, packet_id]), {"approve": True}, format="json", HTTP_AUTHORIZATION=f"Bearer {access}"
        )

        assert response.status_code == 200
        assert ChangePacket.objects.get().status == ChangePacket.Status.APPROVED


class TestAgentReadsLocalVisibility:
    def test_reads_overview_and_keyword_detail(self, client, business, agent_key):
        from apps.local_visibility.models import TrackedKeyword

        keyword = TrackedKeyword.objects.create(business=business, keyword="gutter cleaning near me")

        overview = client.get(reverse("agent:local-visibility", args=[business.id]), **_auth(agent_key))
        assert overview.status_code == 200
        assert "cost_usd" not in str(overview.data)

        detail = client.get(reverse("agent:local-visibility", args=[business.id]), {"keyword": keyword.id}, **_auth(agent_key))
        assert detail.status_code == 200

        assert client.get(reverse("agent:local-visibility", args=[business.id]), {"keyword": 999999}, **_auth(agent_key)).status_code == 404

    def test_needs_a_key(self, client, business):
        assert client.get(reverse("agent:local-visibility", args=[business.id])).status_code == 401
