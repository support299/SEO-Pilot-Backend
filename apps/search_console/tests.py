from datetime import date, datetime, timezone as dt_timezone

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.businesses.models import Account, Business, Membership
from integrations.google import crypto as google_crypto
from integrations.google.search_console import Site, pick_best_site

from .metrics import compare_periods, compute_range_window, has_comparable_data, parse_range_param, summarize_period
from .models import SearchConsoleConnection, SearchConsoleDailyMetric, SearchConsoleTopPage, SearchConsoleTopQuery

pytestmark = pytest.mark.django_db


# --- Pure metrics math — mirrors the exact scenarios already verified once in the old TS version. ---


class TestMetrics:
    def test_summarize_period_computes_totals_and_weighted_position(self):
        rows = [
            {"clicks": 10, "impressions": 100, "position": 5},
            {"clicks": 20, "impressions": 300, "position": 7},
        ]
        summary = summarize_period(rows)

        assert summary.clicks == 30
        assert summary.impressions == 400
        assert summary.ctr == pytest.approx(0.075)
        assert summary.average_position == pytest.approx(6.5)  # (5*100 + 7*300) / 400

    def test_summarize_period_handles_zero_impressions(self):
        summary = summarize_period([])
        assert summary.ctr is None
        assert summary.average_position is None

    def test_compare_periods(self):
        current = summarize_period([{"clicks": 30, "impressions": 400, "position": 6.5}])
        previous = summarize_period([{"clicks": 20, "impressions": 200, "position": 8}])

        comparison = compare_periods(current, previous)

        assert comparison.clicks_delta_pct == pytest.approx(50)
        assert comparison.impressions_delta_pct == pytest.approx(100)
        assert comparison.position_delta == pytest.approx(-1.5)  # improved (moved up)

    def test_compare_periods_returns_none_when_previous_has_no_data(self):
        current = summarize_period([{"clicks": 10, "impressions": 100, "position": 5}])
        previous = summarize_period([])

        comparison = compare_periods(current, previous)

        assert comparison.clicks_delta_pct is None
        assert comparison.impressions_delta_pct is None
        assert comparison.ctr_delta_pct is None
        assert comparison.position_delta is None
        assert has_comparable_data(previous) is False

    def test_parse_range_param_rejects_invalid_values(self):
        assert parse_range_param("7") == 7
        assert parse_range_param("28") == 28
        assert parse_range_param("90") == 90
        assert parse_range_param("9999") == 28  # falls back to default
        assert parse_range_param(None) == 28
        assert parse_range_param("not-a-number") == 28

    def test_compute_range_window_current_and_previous_are_contiguous_non_overlapping(self):
        window = compute_range_window(28, today=date(2026, 6, 1))

        assert (window.current.end - window.current.start).days == 27
        assert (window.previous.end - window.previous.start).days == 27
        assert window.previous.end < window.current.start  # no overlap


class TestPickBestSite:
    def test_matches_domain_property(self):
        sites = [Site("https://other.com/", "siteOwner"), Site("sc-domain:example.com", "siteOwner")]
        assert pick_best_site(sites, "https://example.com").site_url == "sc-domain:example.com"

    def test_falls_back_to_first_when_no_match(self):
        sites = [Site("https://other.com/", "siteOwner")]
        assert pick_best_site(sites, "https://unrelated.com").site_url == "https://other.com/"

    def test_returns_none_for_empty_list(self):
        assert pick_best_site([], "https://example.com") is None


# --- Crypto round-trip + tamper detection ---


class TestCrypto:
    def test_encrypt_decrypt_round_trip(self):
        envelope = google_crypto.encrypt_secret({"access_token": "secret-value"}, "test-secret-key")
        assert google_crypto.decrypt_secret(envelope, "test-secret-key")["access_token"] == "secret-value"

    def test_state_sign_and_verify_round_trip(self):
        state = google_crypto.sign_oauth_state("biz-1", "user-1", "test-state-secret")
        payload = google_crypto.verify_oauth_state(state, "test-state-secret")
        assert payload.business_id == "biz-1"
        assert payload.user_id == "user-1"

    def test_tampered_state_is_rejected(self):
        state = google_crypto.sign_oauth_state("biz-1", "user-1", "test-state-secret")
        with pytest.raises(google_crypto.InvalidOAuthState):
            google_crypto.verify_oauth_state(state + "tampered", "test-state-secret")

    def test_wrong_secret_is_rejected(self):
        state = google_crypto.sign_oauth_state("biz-1", "user-1", "secret-a")
        with pytest.raises(google_crypto.InvalidOAuthState):
            google_crypto.verify_oauth_state(state, "secret-b")


# --- API endpoints: auth + tenant isolation (mirrors the businesses app's tests) ---


@pytest.fixture
def client():
    return APIClient()


def _register_and_login(client, email):
    response = client.post(reverse("accounts:register"), {"email": email, "password": "SuperSecret123!"}, format="json")
    access = response.data["access"]
    account_id = Membership.objects.get(user__email=email).account_id
    return access, account_id


class TestSearchConsoleEndpoints:
    def test_status_requires_authentication(self, client):
        account = Account.objects.create(name="Unowned Account")
        business = Business.objects.create(account=account, name="Unowned")
        response = client.get(reverse("search_console:status", args=[business.id]))
        assert response.status_code == 401

    def test_status_returns_not_connected_when_no_connection(self, client):
        access, account_id = _register_and_login(client, "sc-status@example.com")
        business = Business.objects.create(account_id=account_id, name="My Business")

        response = client.get(reverse("search_console:status", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access}")

        assert response.status_code == 200
        assert response.data == {"connected": False}

    def test_status_for_foreign_business_is_not_found(self, client):
        _access_a, account_a = _register_and_login(client, "sc-a@example.com")
        business = Business.objects.create(account_id=account_a, name="A's Business")

        access_b, _ = _register_and_login(client, "sc-b@example.com")
        response = client.get(reverse("search_console:status", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access_b}")

        assert response.status_code == 404

    def test_sync_without_connection_returns_400(self, client):
        access, account_id = _register_and_login(client, "sc-sync@example.com")
        business = Business.objects.create(account_id=account_id, name="My Business")

        response = client.post(reverse("search_console:sync", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access}")

        assert response.status_code == 400

    def test_sync_with_connection_enqueues_task_and_returns_202(self, client, settings, monkeypatch):
        settings.CELERY_TASK_ALWAYS_EAGER = True
        access, account_id = _register_and_login(client, "sc-sync2@example.com")
        business = Business.objects.create(account_id=account_id, name="My Business")
        SearchConsoleConnection.objects.create(
            business=business,
            site_url="sc-domain:example.com",
            access_token_enc="x",
            refresh_token_enc="x",
            token_expires_at=datetime(2030, 1, 1, tzinfo=dt_timezone.utc),
            scope="scope",
        )

        called = {}

        def fake_sync(connection):
            called["business_id"] = connection.business_id

        monkeypatch.setattr("apps.search_console.services.sync_search_console", fake_sync)

        response = client.post(reverse("search_console:sync", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access}")

        assert response.status_code == 202
        assert called["business_id"] == business.id

    def test_overview_with_no_data_returns_empty_summary(self, client):
        access, account_id = _register_and_login(client, "sc-overview@example.com")
        business = Business.objects.create(account_id=account_id, name="My Business")

        response = client.get(reverse("search_console:overview", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access}")

        assert response.status_code == 200
        assert response.data["summary"]["clicks"] == 0
        assert response.data["comparison"] is None

    def test_disconnect_removes_connection_and_stored_metrics(self, client):
        access, account_id = _register_and_login(client, "sc-disconnect@example.com")
        business = Business.objects.create(account_id=account_id, name="My Business")
        SearchConsoleConnection.objects.create(
            business=business,
            site_url="sc-domain:example.com",
            access_token_enc="x",
            refresh_token_enc="x",
            token_expires_at=datetime(2030, 1, 1, tzinfo=dt_timezone.utc),
            scope="scope",
        )
        SearchConsoleDailyMetric.objects.create(business=business, date=date(2026, 8, 1), clicks=4, impressions=40, ctr=0.1, position=8)
        SearchConsoleTopQuery.objects.create(business=business, query="example query", clicks=4, impressions=40, ctr=0.1, position=8)
        SearchConsoleTopPage.objects.create(business=business, page="https://example.com/", clicks=4, impressions=40, ctr=0.1, position=8)

        response = client.post(reverse("search_console:disconnect", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access}")

        assert response.status_code == 200
        assert response.data == {"connected": False}
        assert SearchConsoleConnection.objects.filter(business=business).count() == 0
        assert SearchConsoleDailyMetric.objects.filter(business=business).count() == 0
        assert SearchConsoleTopQuery.objects.filter(business=business).count() == 0
        assert SearchConsoleTopPage.objects.filter(business=business).count() == 0

    def test_disconnect_without_connection_returns_400(self, client):
        access, account_id = _register_and_login(client, "sc-disconnect-missing@example.com")
        business = Business.objects.create(account_id=account_id, name="My Business")

        response = client.post(reverse("search_console:disconnect", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access}")

        assert response.status_code == 400

    def test_authorize_url_fails_cleanly_when_not_configured(self, client, settings):
        settings.GOOGLE_OAUTH_CLIENT_ID = ""
        access, account_id = _register_and_login(client, "sc-authurl@example.com")
        business = Business.objects.create(account_id=account_id, name="My Business")

        response = client.get(reverse("search_console:authorize-url", args=[business.id]), HTTP_AUTHORIZATION=f"Bearer {access}")

        assert response.status_code == 503
