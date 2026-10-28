import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.businesses.models import Business, Membership
from integrations.wordpress.client import WordPressError, WpObject, WpUser

from . import services
from .models import ChangePacket, WordPressConnection

pytestmark = pytest.mark.django_db


@pytest.fixture
def client():
    return APIClient()


def _register_and_login(client, email):
    response = client.post(reverse("accounts:register"), {"email": email, "password": "SuperSecret123!"}, format="json")
    access = response.data["access"]
    account_id = Membership.objects.get(user__email=email).account_id
    business = Business.objects.create(account_id=account_id, name="Biz", website="https://example.com")
    return access, business


def _object(id=5, title="Old Title", status="publish", link="https://example.com/page", modified_gmt="2026-01-01T00:00:00", meta=None):
    return WpObject(id=id, title=title, slug="page", status=status, link=link, modified_gmt=modified_gmt, meta=meta or {})


class TestConnect:
    def test_connect_succeeds_and_stores_encrypted_password(self, client, monkeypatch):
        access, business = _register_and_login(client, "wp-connect@example.com")
        monkeypatch.setattr("apps.wordpress.services.wp.discover_api_url", lambda url: "https://example.com/wp-json")
        monkeypatch.setattr("apps.wordpress.services.wp.verify_user", lambda *a, **kw: WpUser(id=3, name="Admin", capabilities={"edit_pages": True}))

        response = client.post(
            reverse("wordpress:connect", args=[business.id]),
            {"website_url": "example.com", "username": "admin", "application_password": "abcd efgh ijkl"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {access}",
        )

        assert response.status_code == 204
        connection = WordPressConnection.objects.get(business=business)
        assert connection.application_password_enc != "abcd efgh ijkl"  # never stored in plaintext
        assert "abcd" not in connection.application_password_enc

    def test_connect_rejects_a_user_without_edit_permission(self, client, monkeypatch):
        access, business = _register_and_login(client, "wp-noperm@example.com")
        monkeypatch.setattr("apps.wordpress.services.wp.discover_api_url", lambda url: "https://example.com/wp-json")
        monkeypatch.setattr("apps.wordpress.services.wp.verify_user", lambda *a, **kw: WpUser(id=3, name="Subscriber", capabilities={}))

        response = client.post(
            reverse("wordpress:connect", args=[business.id]),
            {"website_url": "example.com", "username": "sub", "application_password": "x"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {access}",
        )

        assert response.status_code == 400
        assert not WordPressConnection.objects.filter(business=business).exists()

    def test_connect_surfaces_a_wordpress_error_cleanly(self, client, monkeypatch):
        access, business = _register_and_login(client, "wp-error@example.com")

        def boom(url):
            raise WordPressError("Could not reach that website.")

        monkeypatch.setattr("apps.wordpress.services.wp.discover_api_url", boom)

        response = client.post(
            reverse("wordpress:connect", args=[business.id]),
            {"website_url": "example.com", "username": "admin", "application_password": "x"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {access}",
        )

        assert response.status_code == 400
        assert "Could not reach" in response.data["detail"]

    def test_status_for_foreign_business_is_not_found(self, client):
        _access_a, business = _register_and_login(client, "wp-a@example.com")
        access_b, _ = _register_and_login(client, "wp-b@example.com")

        response = client.get(reverse("wordpress:status", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access_b}")

        assert response.status_code == 404


def _connect(business, user_id="7"):
    return WordPressConnection.objects.create(
        business=business,
        website_url="https://example.com/",
        api_url="https://example.com/wp-json",
        username="admin",
        application_password_enc=services.encrypt_secret({"application_password": "abcd efgh"}, "test-secret"),
        wp_user_id=user_id,
        wp_user_name="Admin",
    )


@pytest.fixture(autouse=True)
def _wordpress_secret(settings):
    settings.WORDPRESS_CREDENTIAL_ENCRYPTION_KEY = "test-secret"


class TestChangePacketLifecycle:
    def test_prepare_requires_a_connection(self, client):
        access, business = _register_and_login(client, "wp-prep-noconn@example.com")

        response = client.post(
            reverse("wordpress:change-packets", args=[business.id]),
            {"target_object_type": "page", "target_object_id": 5, "proposed_changes": {"title": "New Title"}},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {access}",
        )

        assert response.status_code == 400

    def test_full_approve_apply_flow(self, client, monkeypatch):
        access, business = _register_and_login(client, "wp-flow@example.com")
        _connect(business)

        live_page = {"title": "Old Title"}
        monkeypatch.setattr("apps.wordpress.services.wp.fetch_object", lambda *a, **kw: _object(title=live_page["title"]))

        prepare = client.post(
            reverse("wordpress:change-packets", args=[business.id]),
            {"target_object_type": "page", "target_object_id": 5, "proposed_changes": {"title": "New Title"}, "reason": "Fix missing title"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {access}",
        )
        assert prepare.status_code == 201
        packet_id = prepare.data["id"]
        assert prepare.data["status"] == ChangePacket.Status.AWAITING_APPROVAL

        decide = client.post(
            reverse("wordpress:change-packet-decision", args=[business.id, packet_id]),
            {"approve": True},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {access}",
        )
        assert decide.status_code == 200
        assert decide.data["status"] == ChangePacket.Status.APPROVED

        def fake_update(api_url, object_type, object_id, changes, username, app_password):
            live_page["title"] = changes.get("title", live_page["title"])
            return _object(title=live_page["title"])

        monkeypatch.setattr("apps.wordpress.services.wp.update_object", fake_update)
        packet = ChangePacket.objects.get(id=packet_id)
        applied = services.apply_change_packet(packet)

        assert applied.status == ChangePacket.Status.APPLIED
        assert applied.before_snapshot["title"] == "Old Title"
        assert applied.after_snapshot["title"] == "New Title"

    def test_apply_refuses_when_not_approved(self, client, monkeypatch):
        access, business = _register_and_login(client, "wp-notapproved@example.com")
        _connect(business)
        monkeypatch.setattr("apps.wordpress.services.wp.fetch_object", lambda *a, **kw: _object())

        prepare = client.post(
            reverse("wordpress:change-packets", args=[business.id]),
            {"target_object_type": "page", "target_object_id": 5, "proposed_changes": {"title": "X"}},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {access}",
        )
        packet = ChangePacket.objects.get(id=prepare.data["id"])

        with pytest.raises(services.WordPressIntegrationError, match="Approve"):
            services.apply_change_packet(packet)

    def test_apply_marks_stale_when_page_changed_since_prepare(self, monkeypatch, client):
        access, business = _register_and_login(client, "wp-stale@example.com")
        _connect(business)

        monkeypatch.setattr("apps.wordpress.services.wp.fetch_object", lambda *a, **kw: _object(modified_gmt="2026-01-01T00:00:00"))
        packet = services.prepare_change_packet(business, user=None, target_object_type="page", target_object_id=5, proposed_changes={"title": "X"})
        packet.status = ChangePacket.Status.APPROVED
        packet.save()

        # The live page has since changed — a different modified_gmt than what was captured at prepare time.
        monkeypatch.setattr("apps.wordpress.services.wp.fetch_object", lambda *a, **kw: _object(modified_gmt="2026-02-01T00:00:00"))

        with pytest.raises(services.WordPressIntegrationError, match="changed after"):
            services.apply_change_packet(packet)

        packet.refresh_from_db()
        assert packet.status == ChangePacket.Status.STALE

    def test_stale_packet_can_be_re_approved_and_then_applied(self, monkeypatch, client):
        """A stale packet must not be a dead end: re-approving it should pick up the page's new revision and allow applying."""
        access, business = _register_and_login(client, "wp-restale@example.com")
        _connect(business)

        monkeypatch.setattr("apps.wordpress.services.wp.fetch_object", lambda *a, **kw: _object(modified_gmt="2026-01-01T00:00:00"))
        packet = services.prepare_change_packet(business, user=None, target_object_type="page", target_object_id=5, proposed_changes={"title": "X"})
        packet.status = ChangePacket.Status.APPROVED
        packet.save()

        monkeypatch.setattr("apps.wordpress.services.wp.fetch_object", lambda *a, **kw: _object(modified_gmt="2026-02-01T00:00:00"))
        with pytest.raises(services.WordPressIntegrationError):
            services.apply_change_packet(packet)
        packet.refresh_from_db()
        assert packet.status == ChangePacket.Status.STALE

        # Re-approve — this should capture the page's *current* revision.
        reapproved = services.decide_change_packet(packet, user=None, approve=True)
        assert reapproved.status == ChangePacket.Status.APPROVED
        assert reapproved.current_revision == "2026-02-01T00:00:00"

        # Now applying should succeed since current_revision matches the live page.
        monkeypatch.setattr("apps.wordpress.services.wp.update_object", lambda *a, **kw: _object(title="X", modified_gmt="2026-02-01T00:00:00"))
        calls = {"n": 0}

        def fetch_then_show_update(*a, **kw):
            calls["n"] += 1
            return _object(title="X" if calls["n"] > 1 else "Old", modified_gmt="2026-02-01T00:00:00")

        monkeypatch.setattr("apps.wordpress.services.wp.fetch_object", fetch_then_show_update)
        applied = services.apply_change_packet(reapproved)
        assert applied.status == ChangePacket.Status.APPLIED

    def test_rollback_restores_before_snapshot(self, monkeypatch, client):
        access, business = _register_and_login(client, "wp-rollback@example.com")
        _connect(business)

        live_page = {"title": "Old Title"}
        monkeypatch.setattr("apps.wordpress.services.wp.fetch_object", lambda *a, **kw: _object(title=live_page["title"]))
        packet = services.prepare_change_packet(business, user=None, target_object_type="page", target_object_id=5, proposed_changes={"title": "New Title"})
        packet.status = ChangePacket.Status.APPROVED
        packet.save()

        def fake_update(api_url, object_type, object_id, changes, username, app_password):
            live_page["title"] = changes.get("title", live_page["title"])
            return _object(title=live_page["title"])

        monkeypatch.setattr("apps.wordpress.services.wp.update_object", fake_update)
        services.apply_change_packet(packet)
        assert live_page["title"] == "New Title"

        packet.refresh_from_db()
        rolled_back = services.rollback_change_packet(packet)

        assert rolled_back.status == ChangePacket.Status.ROLLED_BACK
        assert live_page["title"] == "Old Title"

    def test_rollback_blocked_when_page_changed_again(self, monkeypatch, client):
        access, business = _register_and_login(client, "wp-rollback-blocked@example.com")
        _connect(business)

        monkeypatch.setattr("apps.wordpress.services.wp.fetch_object", lambda *a, **kw: _object())
        packet = services.prepare_change_packet(business, user=None, target_object_type="page", target_object_id=5, proposed_changes={"title": "New Title"})
        packet.status = ChangePacket.Status.APPROVED
        packet.save()

        monkeypatch.setattr("apps.wordpress.services.wp.update_object", lambda *a, **kw: _object(title="New Title"))
        monkeypatch.setattr("apps.wordpress.services.wp.fetch_object", lambda *a, **kw: _object(title="New Title"))
        services.apply_change_packet(packet)

        # Someone/something else changed the title again after we applied it.
        monkeypatch.setattr("apps.wordpress.services.wp.fetch_object", lambda *a, **kw: _object(title="Someone Else's Edit"))
        packet.refresh_from_db()

        with pytest.raises(services.WordPressIntegrationError, match="manual review"):
            services.rollback_change_packet(packet)

        packet.refresh_from_db()
        assert packet.status == ChangePacket.Status.ROLLBACK_BLOCKED


    def test_apply_fails_when_wordpress_silently_ignores_the_change(self, monkeypatch, client):
        access, business = _register_and_login(client, "wp-ignored@example.com")
        _connect(business)

        monkeypatch.setattr("apps.wordpress.services.wp.fetch_object", lambda *a, **kw: _object(title="Old Title"))
        packet = services.prepare_change_packet(business, user=None, target_object_type="page", target_object_id=5, proposed_changes={"title": "New Title"})
        packet.status = ChangePacket.Status.APPROVED
        packet.save()
        monkeypatch.setattr("apps.wordpress.services.wp.update_object", lambda *a, **kw: _object(title="Old Title"))

        with pytest.raises(services.WordPressIntegrationError):
            services.apply_change_packet(packet)
        packet.refresh_from_db()
        assert packet.status == ChangePacket.Status.FAILED


class TestSnapshot:
    def test_aioseo_site_snapshots_the_seo_title_not_the_page_title(self):
        obj = _object(title="Blog", meta={"_aioseo_title": "Service Business CRM Tips | Blog", "_aioseo_description": "Desc"})

        assert services._snapshot(obj) == {"title": "Service Business CRM Tips | Blog", "meta_description": "Desc"}

    def test_empty_aioseo_title_stays_empty_instead_of_falling_back_to_page_title(self):
        obj = _object(title="Blog", meta={"_aioseo_title": ""})

        assert services._snapshot(obj)["title"] == ""

    def test_site_without_aioseo_uses_page_title(self):
        assert services._snapshot(_object(title="Blog", meta={"rank_math_description": "D"})) == {"title": "Blog", "meta_description": "D"}
