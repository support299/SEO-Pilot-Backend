import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APIClient

from .models import Account, Business, Membership

pytestmark = pytest.mark.django_db


@pytest.fixture
def client():
    return APIClient()


def _register_and_login(client, email):
    response = client.post(reverse("accounts:register"), {"email": email, "password": "SuperSecret123!"}, format="json")
    access = response.data["access"]
    account_id = Membership.objects.get(user__email=email).account_id
    return access, account_id


class TestTenantIsolation:
    """
    These test the exact scenario the whole permission model exists for: two
    unrelated accounts must never be able to see or touch each other's data,
    even though both users are legitimately authenticated.
    """

    def test_user_only_sees_their_own_account_businesses(self, client):
        _access_a, account_a = _register_and_login(client, "iso-a@example.com")
        Business.objects.create(account_id=account_a, name="A's Business")

        access_b, _account_b = _register_and_login(client, "iso-b@example.com")
        response = client.get(reverse("businesses:business-list"), HTTP_AUTHORIZATION=f"Bearer {access_b}")

        assert response.status_code == 200
        assert response.data["count"] == 0

    def test_direct_fetch_of_foreign_business_is_not_found(self, client):
        _access_a, account_a = _register_and_login(client, "iso-c@example.com")
        business = Business.objects.create(account_id=account_a, name="C's Business")

        access_b, _ = _register_and_login(client, "iso-d@example.com")
        response = client.get(reverse("businesses:business-detail", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access_b}")

        # 404, not 403 — a 403 would confirm the business exists at all.
        assert response.status_code == 404

    def test_cannot_create_business_under_a_foreign_account(self, client):
        _access_a, account_a = _register_and_login(client, "iso-e@example.com")
        access_b, _ = _register_and_login(client, "iso-f@example.com")

        response = client.post(
            reverse("businesses:business-list"),
            {"account": account_a, "name": "Hijack attempt"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {access_b}",
        )

        assert response.status_code == 400
        assert "account" in response.data["error"]["details"]

    def test_owner_can_manage_their_own_business(self, client):
        access, account_id = _register_and_login(client, "owner@example.com")

        create_response = client.post(
            reverse("businesses:business-list"),
            {"account": account_id, "name": "My Business", "website": "https://example.com"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {access}",
        )
        assert create_response.status_code == 201

        business_id = create_response.data["id"]
        update_response = client.patch(
            reverse("businesses:business-detail", args=[business_id]),
            {"name": "Renamed Business"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {access}",
        )
        assert update_response.status_code == 200
        assert update_response.data["name"] == "Renamed Business"

    def test_requires_authentication(self, client):
        response = client.get(reverse("businesses:business-list"))
        assert response.status_code == 401


class TestAccountModel:
    def test_membership_unique_per_account_and_user(self):
        account = Account.objects.create(name="Shared Account")
        from apps.accounts.models import User

        user = User.objects.create_user(email="dup-member@example.com", password="x")
        Membership.objects.create(account=account, user=user, role=Membership.Role.OWNER)

        with pytest.raises(Exception):
            Membership.objects.create(account=account, user=user, role=Membership.Role.MEMBER)


def _make_admin(email="boss@agency.com"):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(email=email, password="SuperSecret123!", is_agency_admin=True)


def _login(client, email, password="SuperSecret123!"):
    return client.post(reverse("accounts:login"), {"email": email, "password": password}, format="json").data["access"]


class TestAgencyAdminAndViewerRoles:
    def test_agency_admin_sees_every_customers_businesses_with_edit_rights(self, client):
        _a, account_a = _register_and_login(client, "cust-a@example.com")
        _b, account_b = _register_and_login(client, "cust-b@example.com")
        Business.objects.create(account_id=account_a, name="A Co")
        Business.objects.create(account_id=account_b, name="B Co")
        _make_admin()

        response = client.get(reverse("businesses:business-list"), HTTP_AUTHORIZATION=f"Bearer {_login(client, 'boss@agency.com')}")

        assert response.status_code == 200
        assert {b["name"] for b in response.data["results"]} == {"A Co", "B Co"}
        assert all(b["can_edit"] for b in response.data["results"])

    def test_regular_user_is_not_an_admin_and_cannot_reach_agency_endpoints(self, client):
        access, _account = _register_and_login(client, "plain@example.com")
        response = client.get(reverse("businesses:agency-customers"), HTTP_AUTHORIZATION=f"Bearer {access}")
        assert response.status_code == 403

    def test_admin_onboards_a_customer_with_a_read_only_login(self, client):
        _make_admin()
        admin_access = _login(client, "boss@agency.com")

        response = client.post(
            reverse("businesses:agency-customers"),
            {"customer_name": "Acme Gutters", "website": "https://acme.example", "email": "owner@acme.example", "password": "SuperSecret123!"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {admin_access}",
        )

        assert response.status_code == 201
        membership = Membership.objects.get(user__email="owner@acme.example")
        assert membership.role == Membership.Role.VIEWER
        assert Business.objects.get(id=response.data["business"]["id"]).name == "Acme Gutters"

    def test_customer_login_requires_a_password_for_a_new_user(self, client):
        _make_admin()
        response = client.post(
            reverse("businesses:agency-customers"),
            {"customer_name": "Acme", "email": "new@acme.example"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {_login(client, 'boss@agency.com')}",
        )
        assert response.status_code == 400

    def test_viewer_can_read_but_not_write(self, client):
        _make_admin()
        admin_access = _login(client, "boss@agency.com")
        created = client.post(
            reverse("businesses:agency-customers"),
            {"customer_name": "Acme", "email": "viewer@acme.example", "password": "SuperSecret123!"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {admin_access}",
        ).data
        business_id, account_id = created["business"]["id"], created["account"]["id"]
        viewer = f"Bearer {_login(client, 'viewer@acme.example')}"

        listing = client.get(reverse("businesses:business-list"), HTTP_AUTHORIZATION=viewer)
        assert listing.status_code == 200 and listing.data["results"][0]["can_edit"] is False
        assert client.get(reverse("history:list", args=[business_id]), HTTP_AUTHORIZATION=viewer).status_code == 200

        # every kind of write is refused: ordinary actions, creating a business, editing one
        assert client.post(reverse("search_console:sync", args=[business_id]), HTTP_AUTHORIZATION=viewer).status_code == 403
        assert client.post(reverse("site_health:crawl", args=[business_id]), HTTP_AUTHORIZATION=viewer).status_code == 403
        assert client.post(reverse("businesses:business-list"), {"account": account_id, "name": "Sneaky"}, format="json", HTTP_AUTHORIZATION=viewer).status_code == 403
        assert client.patch(reverse("businesses:business-detail", args=[business_id]), {"name": "Renamed"}, format="json", HTTP_AUTHORIZATION=viewer).status_code == 403
        assert Business.objects.get(id=business_id).name == "Acme"

    def test_viewer_can_still_start_the_google_sign_in(self, client, settings):
        settings.GOOGLE_OAUTH_CLIENT_ID = "id"
        settings.GOOGLE_OAUTH_CLIENT_SECRET = "secret"
        settings.SEARCH_CONSOLE_OAUTH_STATE_SECRET = "state-secret"
        _make_admin()
        created = client.post(
            reverse("businesses:agency-customers"),
            {"customer_name": "Acme", "email": "grant@acme.example", "password": "SuperSecret123!"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {_login(client, 'boss@agency.com')}",
        ).data
        response = client.get(reverse("search_console:authorize-url", args=[created["business"]["id"]]), HTTP_AUTHORIZATION=f"Bearer {_login(client, 'grant@acme.example')}")
        assert response.status_code == 200  # customers grant Google access themselves

    def test_viewer_cannot_see_another_customers_business(self, client):
        _make_admin()
        admin_access = f"Bearer {_login(client, 'boss@agency.com')}"
        first = client.post(reverse("businesses:agency-customers"), {"customer_name": "One", "email": "one@x.example", "password": "SuperSecret123!"}, format="json", HTTP_AUTHORIZATION=admin_access).data
        second = client.post(reverse("businesses:agency-customers"), {"customer_name": "Two", "email": "two@x.example", "password": "SuperSecret123!"}, format="json", HTTP_AUTHORIZATION=admin_access).data
        response = client.get(reverse("history:list", args=[second["business"]["id"]]), HTTP_AUTHORIZATION=f"Bearer {_login(client, 'one@x.example')}")
        assert response.status_code == 404

    def test_a_stranger_writing_to_someone_elses_business_still_gets_404_not_403(self, client):
        _a, account_a = _register_and_login(client, "owner-x@example.com")
        business = Business.objects.create(account_id=account_a, name="Private")
        access_b, _ = _register_and_login(client, "stranger-x@example.com")
        response = client.post(reverse("search_console:sync", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access_b}")
        assert response.status_code == 404  # same answer as before roles existed: no hint the id is real


class TestClientsWithMultipleWebsites:
    def _admin(self, client):
        _make_admin()
        return f"Bearer {_login(client, 'boss@agency.com')}"

    def _create_client(self, client, admin, name="Acme Group", **extra):
        return client.post(reverse("businesses:agency-customers"), {"customer_name": name, **extra}, format="json", HTTP_AUTHORIZATION=admin).data

    def test_admin_adds_several_websites_under_one_client(self, client):
        admin = self._admin(client)
        acme = self._create_client(client, admin, website="https://acme-plumbing.com")

        for name, site in (("Acme HVAC", "acme-hvac.com"), ("Acme Roofing", "https://acme-roofing.com")):
            response = client.post(reverse("businesses:business-list"), {"account": acme["account"]["id"], "name": name, "website": site}, format="json", HTTP_AUTHORIZATION=admin)
            assert response.status_code == 201
            assert response.data["account_name"] == "Acme Group"

        listed = client.get(reverse("businesses:agency-customers"), HTTP_AUTHORIZATION=admin).data
        websites = [b["website"] for b in next(c for c in listed if c["id"] == acme["account"]["id"])["businesses"]]
        assert websites == ["https://acme-plumbing.com", "https://acme-hvac.com", "https://acme-roofing.com"]

    def test_website_typed_without_a_scheme_is_stored_as_https(self, client):
        admin = self._admin(client)
        created = self._create_client(client, admin, name="Bare Domain", website="bare-domain.com")
        assert created["business"]["website"] == "https://bare-domain.com"

    def test_client_login_sees_all_of_their_websites_and_none_of_anothers(self, client):
        admin = self._admin(client)
        acme = self._create_client(client, admin, email="owner@acme.example", password="SuperSecret123!")
        other = self._create_client(client, admin, name="Other Co", email="owner@other.example", password="SuperSecret123!")
        client.post(reverse("businesses:business-list"), {"account": acme["account"]["id"], "name": "Acme Second Site"}, format="json", HTTP_AUTHORIZATION=admin)

        mine = client.get(reverse("businesses:business-list"), HTTP_AUTHORIZATION=f"Bearer {_login(client, 'owner@acme.example')}").data["results"]

        assert {b["name"] for b in mine} == {"Acme Group", "Acme Second Site"}
        assert other["business"]["id"] not in {b["id"] for b in mine}

    def test_each_website_is_its_own_workspace(self, client):
        admin = self._admin(client)
        acme = self._create_client(client, admin)
        second = client.post(reverse("businesses:business-list"), {"account": acme["account"]["id"], "name": "Second"}, format="json", HTTP_AUTHORIZATION=admin).data

        assert second["id"] != acme["business"]["id"]
        history = client.get(reverse("history:list", args=[second["id"]]), HTTP_AUTHORIZATION=admin)
        assert history.status_code == 200


class TestPauseAndDeleteAccountsAndBusinesses:
    def _admin(self, client):
        _make_admin()
        return f"Bearer {_login(client, 'boss@agency.com')}"

    def test_admin_can_pause_and_resume_customer(self, client):
        admin = self._admin(client)
        created = client.post(
            reverse("businesses:agency-customers"),
            {"customer_name": "Pause Test Co"},
            format="json",
            HTTP_AUTHORIZATION=admin,
        ).data
        account_id = created["account"]["id"]

        # Pause customer
        response = client.patch(
            reverse("businesses:agency-customer-detail", args=[account_id]),
            {"is_paused": True},
            format="json",
            HTTP_AUTHORIZATION=admin,
        )
        assert response.status_code == 200
        assert response.data["is_paused"] is True
        assert response.data["businesses"][0]["effective_paused"] is True

        # Verify in list
        listed = client.get(reverse("businesses:agency-customers"), HTTP_AUTHORIZATION=admin).data
        customer = next(c for c in listed if c["id"] == account_id)
        assert customer["is_paused"] is True

        # Resume customer
        response = client.patch(
            reverse("businesses:agency-customer-detail", args=[account_id]),
            {"is_paused": False},
            format="json",
            HTTP_AUTHORIZATION=admin,
        )
        assert response.status_code == 200
        assert response.data["is_paused"] is False

    def test_admin_can_delete_customer_and_cleans_orphan_viewer(self, client):
        admin = self._admin(client)
        created = client.post(
            reverse("businesses:agency-customers"),
            {"customer_name": "Delete Test Co", "email": "viewer-delete@example.com", "password": "SuperSecret123!"},
            format="json",
            HTTP_AUTHORIZATION=admin,
        ).data
        account_id = created["account"]["id"]
        business_id = created["business"]["id"]

        User = get_user_model()
        assert User.objects.filter(email="viewer-delete@example.com").exists()

        # Delete customer account
        response = client.delete(
            reverse("businesses:agency-customer-detail", args=[account_id]),
            HTTP_AUTHORIZATION=admin,
        )
        assert response.status_code == 204

        assert not Account.objects.filter(id=account_id).exists()
        assert not Business.objects.filter(id=business_id).exists()
        assert not User.objects.filter(email="viewer-delete@example.com").exists()
        # Admin is still alive
        assert User.objects.filter(email="boss@agency.com").exists()

    def test_non_admin_cannot_access_agency_customer_endpoints(self, client):
        access, account_id = _register_and_login(client, "regular@example.com")
        auth = {"HTTP_AUTHORIZATION": f"Bearer {access}"}

        assert client.get(reverse("businesses:agency-customer-detail", args=[account_id]), **auth).status_code == 403
        assert client.patch(reverse("businesses:agency-customer-detail", args=[account_id]), {"is_paused": True}, format="json", **auth).status_code == 403
        assert client.delete(reverse("businesses:agency-customer-detail", args=[account_id]), **auth).status_code == 403

    def test_pause_and_resume_business(self, client):
        access, account_id = _register_and_login(client, "bizowner@example.com")
        auth = {"HTTP_AUTHORIZATION": f"Bearer {access}"}
        business = Business.objects.create(account_id=account_id, name="Biz Test")

        # Pause business
        response = client.patch(
            reverse("businesses:business-detail", args=[business.id]),
            {"is_paused": True},
            format="json",
            **auth,
        )
        assert response.status_code == 200
        assert response.data["is_paused"] is True
        assert response.data["effective_paused"] is True

        business.refresh_from_db()
        assert business.is_paused is True

        # Resume business
        response = client.patch(
            reverse("businesses:business-detail", args=[business.id]),
            {"is_paused": False},
            format="json",
            **auth,
        )
        assert response.status_code == 200
        assert response.data["is_paused"] is False
        assert response.data["effective_paused"] is False

    def test_viewer_cannot_pause_or_delete_business(self, client):
        admin = self._admin(client)
        created = client.post(
            reverse("businesses:agency-customers"),
            {"customer_name": "Viewer Biz Test", "email": "viewerbiz@example.com", "password": "SuperSecret123!"},
            format="json",
            HTTP_AUTHORIZATION=admin,
        ).data
        business_id = created["business"]["id"]

        viewer_access = f"Bearer {_login(client, 'viewerbiz@example.com')}"
        v_auth = {"HTTP_AUTHORIZATION": viewer_access}

        # Viewer cannot pause
        assert client.patch(reverse("businesses:business-detail", args=[business_id]), {"is_paused": True}, format="json", **v_auth).status_code == 403
        # Viewer cannot delete
        assert client.delete(reverse("businesses:business-detail", args=[business_id]), **v_auth).status_code == 403

    def test_owner_or_admin_can_delete_business(self, client):
        access, account_id = _register_and_login(client, "deleter@example.com")
        auth = {"HTTP_AUTHORIZATION": f"Bearer {access}"}
        business = Business.objects.create(account_id=account_id, name="To Delete")

        response = client.delete(reverse("businesses:business-detail", args=[business.id]), **auth)
        assert response.status_code == 204
        assert not Business.objects.filter(id=business.id).exists()

