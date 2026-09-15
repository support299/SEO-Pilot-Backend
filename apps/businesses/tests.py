import pytest
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
