import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.businesses.models import Account, Membership

pytestmark = pytest.mark.django_db


@pytest.fixture
def client():
    return APIClient()


def _register(client, email="new@example.com", password="SuperSecret123!", full_name="New User"):
    return client.post(
        reverse("accounts:register"),
        {"email": email, "password": password, "full_name": full_name},
        format="json",
    )


class TestRegister:
    def test_creates_user_account_and_owner_membership(self, client):
        response = _register(client)

        assert response.status_code == 201
        assert "access" in response.data
        assert "refresh_token" not in response.data  # never in the body — only the httpOnly cookie

        account = Account.objects.get()
        membership = Membership.objects.get()
        assert membership.account == account
        assert membership.role == Membership.Role.OWNER

    def test_sets_httponly_refresh_cookie(self, client):
        response = _register(client)

        cookie = response.cookies["refresh_token"]
        assert cookie["httponly"] is True
        assert cookie["samesite"] == "Lax"

    def test_rejects_duplicate_email(self, client):
        _register(client, email="dupe@example.com")
        response = _register(client, email="dupe@example.com")

        assert response.status_code == 400
        assert response.data["error"]["code"] == "validation_error"

    def test_rejects_weak_password(self, client):
        response = _register(client, password="123")
        assert response.status_code == 400


class TestLogin:
    def test_succeeds_with_correct_credentials(self, client):
        _register(client, email="user@example.com", password="SuperSecret123!")
        response = client.post(reverse("accounts:login"), {"email": "user@example.com", "password": "SuperSecret123!"}, format="json")

        assert response.status_code == 200
        assert "access" in response.data

    def test_fails_with_wrong_password(self, client):
        _register(client, email="user2@example.com", password="SuperSecret123!")
        response = client.post(reverse("accounts:login"), {"email": "user2@example.com", "password": "WrongPassword!"}, format="json")

        assert response.status_code == 401

    def test_does_not_leak_whether_email_exists(self, client):
        response = client.post(reverse("accounts:login"), {"email": "nobody@example.com", "password": "whatever"}, format="json")
        assert response.status_code == 401
        assert "exist" not in response.data["detail"].lower()


class TestMe:
    def test_requires_authentication(self, client):
        response = client.get(reverse("accounts:me"))
        assert response.status_code == 401

    def test_returns_user_and_their_accounts_with_role(self, client):
        register_response = _register(client, email="me@example.com")
        access = register_response.data["access"]

        response = client.get(reverse("accounts:me"), HTTP_AUTHORIZATION=f"Bearer {access}")

        assert response.status_code == 200
        assert response.data["user"]["email"] == "me@example.com"
        assert response.data["accounts"][0]["role"] == "owner"


class TestLogout:
    def test_blacklists_refresh_token(self, client):
        register_response = _register(client, email="logout@example.com")
        access = register_response.data["access"]

        logout_response = client.post(reverse("accounts:logout"), HTTP_AUTHORIZATION=f"Bearer {access}")
        assert logout_response.status_code == 204

        # The refresh cookie set at registration should now be rejected.
        refresh_response = client.post(reverse("accounts:refresh"))
        assert refresh_response.status_code == 401


class TestSsoLogin:
    def _link(self, location_id="loc123", **user_kwargs):
        from django.contrib.auth import get_user_model

        from .models import SsoLink

        user = get_user_model().objects.create_user(email="sso@agency.com", password="SuperSecret123!", is_agency_admin=True, **user_kwargs)
        return SsoLink.issue(name="ghl", user=user, location_id=location_id)

    def _sso(self, client, location_id, token):
        return client.post(reverse("accounts:sso"), {"location_id": location_id, "token": token}, format="json")

    def test_valid_link_signs_in(self, client):
        _link, secret = self._link()
        response = self._sso(client, "loc123", secret)

        assert response.status_code == 200
        assert response.data["user"]["email"] == "sso@agency.com"
        assert response.cookies["refresh_token"]["httponly"] is True
        _link.refresh_from_db()
        assert _link.last_used_at is not None

    def test_location_id_alone_is_not_enough(self, client):
        self._link()
        assert self._sso(client, "loc123", "").status_code == 401
        assert self._sso(client, "loc123", "guess").status_code == 401

    def test_wrong_location_is_rejected(self, client):
        _link, secret = self._link()
        assert self._sso(client, "another-location", secret).status_code == 401

    def test_revoked_link_is_rejected(self, client):
        from django.utils import timezone

        link, secret = self._link()
        link.revoked_at = timezone.now()
        link.save()
        assert self._sso(client, "loc123", secret).status_code == 401

    def test_inactive_user_is_rejected(self, client):
        link, secret = self._link()
        link.user.is_active = False
        link.user.save()
        assert self._sso(client, "loc123", secret).status_code == 401

    def test_the_secret_is_never_stored(self):
        from .models import SsoLink

        _link, secret = self._link()
        assert secret not in SsoLink.objects.get().secret_hash
