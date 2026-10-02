from datetime import date, datetime, timedelta, timezone as dt_timezone

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.businesses.models import Account, Business, Membership
from integrations.google import crypto as google_crypto
from apps.accounts.models import User
from integrations.google.analytics import DailyOrganic, GoogleApiError, Property, TokenSet, pick_best_property

from .metrics import summarize_period
from .models import AnalyticsConnection, AnalyticsDailyMetric
from .services import complete_connection, sync_analytics

pytestmark = pytest.mark.django_db


class TestPickBestProperty:
    def test_matches_stream_host(self):
        properties = [
            Property("111", "Other", "1", "https://other.com"),
            Property("222", "Example", "1", "https://www.example.com"),
        ]
        assert pick_best_property(properties, "https://example.com").property_id == "222"

    def test_matches_property_name_without_the_full_domain(self):
        properties = [Property("222", "Saasyway", "1", None), Property("111", "Other", "1", "https://other.com")]
        assert pick_best_property(properties, "https://saasyway.com").property_id == "222"

    def test_uses_the_only_property_when_it_has_no_website(self):
        properties = [Property("222", "Web stream", "1", None)]
        assert pick_best_property(properties, "https://saasyway.com").property_id == "222"

    def test_does_not_use_the_only_property_when_it_is_a_different_site(self):
        properties = [Property("111", "Other", "1", "https://other.com")]
        assert pick_best_property(properties, "https://saasyway.com") is None

    def test_returns_none_when_several_properties_do_not_match(self):
        properties = [
            Property("111", "Other", "1", "https://other.com"),
            Property("333", "Another", "1", "https://another.com"),
        ]
        assert pick_best_property(properties, "https://example.com") is None

    def test_single_property_without_a_website_is_used(self):
        properties = [Property("111", "Only", "1", None)]
        assert pick_best_property(properties, None).property_id == "111"

    def test_several_properties_without_a_website_are_not_guessed(self):
        properties = [Property("111", "A", "1", None), Property("222", "B", "1", None)]
        assert pick_best_property(properties, None) is None

    def test_empty_list(self):
        assert pick_best_property([], "https://example.com") is None


class TestSummarize:
    def test_conversions_stay_unset_when_no_key_events_exist(self):
        summary = summarize_period([{"sessions": 4, "active_users": 3, "conversions": 9}], conversions_measurable=False)
        assert summary.sessions == 4
        assert summary.conversions is None

    def test_conversions_sum_when_key_events_exist(self):
        summary = summarize_period(
            [
                {"sessions": 4, "active_users": 3, "conversions": 1},
                {"sessions": 2, "active_users": 2, "conversions": 0},
            ],
            conversions_measurable=True,
        )
        assert summary.sessions == 6
        assert summary.conversions == 1


@pytest.fixture
def client():
    return APIClient()


def _register_and_login(client, email):
    response = client.post(reverse("accounts:register"), {"email": email, "password": "SuperSecret123!"}, format="json")
    access = response.data["access"]
    account_id = Membership.objects.get(user__email=email).account_id
    return access, account_id


def _connection(business, events=None):
    secret = "test-secret-key"
    return AnalyticsConnection.objects.create(
        business=business,
        property_id="123",
        property_name="Example",
        website_url="https://example.com",
        access_token_enc=google_crypto.encrypt_secret({"access_token": "token"}, secret),
        refresh_token_enc=google_crypto.encrypt_secret({"refresh_token": "refresh"}, secret),
        token_expires_at=datetime(2030, 1, 1, tzinfo=dt_timezone.utc),
        scope="https://www.googleapis.com/auth/analytics.readonly",
        conversion_events=events or [],
    )


class TestAnalyticsEndpoints:
    def test_status_requires_authentication(self, client):
        account = Account.objects.create(name="Unowned Account")
        business = Business.objects.create(account=account, name="Unowned")
        response = client.get(reverse("analytics:status", args=[business.id]))
        assert response.status_code == 401

    def test_status_returns_not_connected(self, client):
        access, account_id = _register_and_login(client, "ga-status@example.com")
        business = Business.objects.create(account_id=account_id, name="My Business")
        response = client.get(reverse("analytics:status", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access}")
        assert response.status_code == 200
        assert response.data == {"connected": False}

    def test_status_for_foreign_business_is_not_found(self, client):
        _access_a, account_a = _register_and_login(client, "ga-a@example.com")
        business = Business.objects.create(account_id=account_a, name="A's Business")
        access_b, _ = _register_and_login(client, "ga-b@example.com")
        response = client.get(reverse("analytics:status", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access_b}")
        assert response.status_code == 404

    def test_sync_without_connection_returns_400(self, client):
        access, account_id = _register_and_login(client, "ga-sync@example.com")
        business = Business.objects.create(account_id=account_id, name="My Business")
        response = client.post(reverse("analytics:sync", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access}")
        assert response.status_code == 400

    def test_disconnect_removes_connection_and_metrics(self, client):
        access, account_id = _register_and_login(client, "ga-disconnect@example.com")
        business = Business.objects.create(account_id=account_id, name="My Business", website="https://example.com")
        _connection(business, events=["generate_lead"])
        AnalyticsDailyMetric.objects.create(business=business, date=date(2026, 8, 1), sessions=4, active_users=3, conversions=1)

        response = client.post(reverse("analytics:disconnect", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access}")

        assert response.status_code == 200
        assert response.data == {"connected": False}
        assert AnalyticsConnection.objects.filter(business=business).count() == 0
        assert AnalyticsDailyMetric.objects.filter(business=business).count() == 0

    def test_overview_hides_conversions_when_no_key_events(self, client):
        access, account_id = _register_and_login(client, "ga-overview@example.com")
        business = Business.objects.create(account_id=account_id, name="My Business", website="https://example.com")
        _connection(business, events=[])
        AnalyticsDailyMetric.objects.create(business=business, date=date.today() - timedelta(days=3), sessions=8, active_users=5, conversions=None)

        response = client.get(reverse("analytics:overview", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access}")

        assert response.status_code == 200
        assert response.data["conversions_measurable"] is False
        assert response.data["summary"]["sessions"] == 8
        assert response.data["summary"]["conversions"] is None

    def test_overview_reports_conversions_when_key_events_exist(self, client):
        access, account_id = _register_and_login(client, "ga-leads@example.com")
        business = Business.objects.create(account_id=account_id, name="My Business", website="https://example.com")
        _connection(business, events=["generate_lead"])
        AnalyticsDailyMetric.objects.create(business=business, date=date.today() - timedelta(days=3), sessions=8, active_users=5, conversions=2)

        response = client.get(reverse("analytics:overview", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access}")

        assert response.data["conversions_measurable"] is True
        assert response.data["summary"]["conversions"] == 2

    def test_authorize_url_uses_analytics_scope_and_callback(self, client):
        access, account_id = _register_and_login(client, "ga-redirect@example.com")
        business = Business.objects.create(account_id=account_id, name="My Business", website="https://example.com")
        response = client.get(reverse("analytics:authorize-url", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access}", HTTP_HOST="127.0.0.1:8000")
        assert response.status_code == 200
        assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8000%2Fapi%2Fv1%2Fanalytics%2Fcallback%2F" in response.data["url"]
        assert "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fanalytics.readonly" in response.data["url"]

    def test_callback_shows_the_google_error(self, client, settings, monkeypatch):
        access, account_id = _register_and_login(client, "ga-callback@example.com")
        business = Business.objects.create(account_id=account_id, name="My Business", website="https://example.com")
        user = User.objects.get(email="ga-callback@example.com")
        state = google_crypto.sign_oauth_state(str(business.id), str(user.id), settings.SEARCH_CONSOLE_OAUTH_STATE_SECRET, redirect_uri="http://127.0.0.1:8000/api/v1/analytics/callback/")

        def fail(**kwargs):
            raise GoogleApiError("Google Analytics Admin API has not been used in this project.")

        monkeypatch.setattr("apps.analytics.views.services.complete_connection", fail)
        response = client.get(reverse("analytics:callback"), {"code": "abc", "state": state})

        assert response.status_code == 302
        location = response["Location"]
        assert "gaError=connection_failed" in location
        assert "Admin+API" in location or "Admin%20API" in location or "Admin API" in location
        assert access  # login succeeded; the callback itself is unauthenticated

    def test_authorize_url_fails_cleanly_when_not_configured(self, client, settings):
        settings.GOOGLE_OAUTH_CLIENT_ID = ""
        access, account_id = _register_and_login(client, "ga-authurl@example.com")
        business = Business.objects.create(account_id=account_id, name="My Business")
        response = client.get(reverse("analytics:authorize-url", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access}")
        assert response.status_code == 503


class TestCompleteConnection:
    def test_missing_key_events_does_not_block_the_connection(self, settings, monkeypatch):
        settings.GOOGLE_OAUTH_CLIENT_ID = "client"
        settings.GOOGLE_OAUTH_CLIENT_SECRET = "secret"
        settings.SEARCH_CONSOLE_TOKEN_ENCRYPTION_KEY = "test-secret-key"
        account = Account.objects.create(name="Account 3")
        business = Business.objects.create(account=account, name="Example", website="https://example.com")

        monkeypatch.setattr(
            "apps.analytics.services.ga_api.exchange_code_for_tokens",
            lambda **kwargs: TokenSet("access", "refresh", 3600, "https://www.googleapis.com/auth/analytics.readonly"),
        )
        monkeypatch.setattr(
            "apps.analytics.services.ga_api.list_properties",
            lambda access_token: [Property("123", "Example", "1", "https://example.com")],
        )

        def fail(access_token, property_id):
            raise GoogleApiError("key events unavailable")

        monkeypatch.setattr("apps.analytics.services.ga_api.list_key_events", fail)

        connection = complete_connection(business=business, code="abc", redirect_uri="http://127.0.0.1:8000/api/v1/analytics/callback/")

        assert connection.property_id == "123"
        assert connection.conversion_events == []


class TestSync:
    def test_sync_drops_conversion_counts_when_no_key_events(self, settings, monkeypatch):
        settings.SEARCH_CONSOLE_TOKEN_ENCRYPTION_KEY = "test-secret-key"
        account = Account.objects.create(name="Account")
        business = Business.objects.create(account=account, name="Example", website="https://example.com")
        connection = _connection(business)

        monkeypatch.setattr("apps.analytics.services.ga_api.list_key_events", lambda access_token, property_id: [])
        monkeypatch.setattr(
            "apps.analytics.services.ga_api.fetch_organic_daily",
            lambda **kwargs: [DailyOrganic(day=date(2026, 8, 1), sessions=5, active_users=4, conversions=9)],
        )

        sync_analytics(connection)
        stored = AnalyticsDailyMetric.objects.get(business=business)
        connection.refresh_from_db()

        assert stored.sessions == 5
        assert stored.conversions is None
        assert connection.conversion_events == []
        assert connection.last_synced_at is not None

    def test_sync_keeps_conversion_counts_when_key_events_exist(self, settings, monkeypatch):
        settings.SEARCH_CONSOLE_TOKEN_ENCRYPTION_KEY = "test-secret-key"
        account = Account.objects.create(name="Account 2")
        business = Business.objects.create(account=account, name="Example", website="https://example.com")
        connection = _connection(business)

        monkeypatch.setattr("apps.analytics.services.ga_api.list_key_events", lambda access_token, property_id: ["generate_lead"])
        monkeypatch.setattr(
            "apps.analytics.services.ga_api.fetch_organic_daily",
            lambda **kwargs: [DailyOrganic(day=date(2026, 8, 1), sessions=5, active_users=4, conversions=2)],
        )

        sync_analytics(connection)
        stored = AnalyticsDailyMetric.objects.get(business=business)
        connection.refresh_from_db()

        assert stored.conversions == 2
        assert connection.conversion_events == ["generate_lead"]
