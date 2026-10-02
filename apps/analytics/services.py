from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.businesses.models import Business
from integrations.google import analytics as ga_api
from integrations.google import crypto as google_crypto

from .metrics import compute_range_window, percent_change, summarize_period
from .models import AnalyticsConnection, AnalyticsDailyMetric

logger = logging.getLogger("django")

SYNC_WINDOW_DAYS = 186


class AnalyticsError(Exception):
    def __init__(self, message: str, code: str = "connection_failed") -> None:
        super().__init__(message)
        self.code = code


def _client_id() -> str:
    value = settings.GOOGLE_OAUTH_CLIENT_ID
    if not value:
        raise AnalyticsError("Google Analytics is not configured on this server yet.")
    return value


def _client_secret() -> str:
    value = settings.GOOGLE_OAUTH_CLIENT_SECRET
    if not value:
        raise AnalyticsError("Google Analytics is not configured on this server yet.")
    return value


def _state_secret() -> str:
    return settings.SEARCH_CONSOLE_OAUTH_STATE_SECRET


def _token_encryption_secret() -> str:
    return settings.SEARCH_CONSOLE_TOKEN_ENCRYPTION_KEY


def build_authorization_url(*, business_id: str, user_id: str, redirect_uri: str) -> str:
    state = google_crypto.sign_oauth_state(business_id, user_id, _state_secret(), redirect_uri=redirect_uri)
    return ga_api.build_authorization_url(client_id=_client_id(), redirect_uri=redirect_uri, state=state)


def read_oauth_state(state: str) -> google_crypto.OAuthStatePayload:
    return google_crypto.verify_oauth_state(state, _state_secret())


@transaction.atomic
def complete_connection(*, business: Business, code: str, redirect_uri: str) -> AnalyticsConnection:
    tokens = ga_api.exchange_code_for_tokens(client_id=_client_id(), client_secret=_client_secret(), code=code, redirect_uri=redirect_uri)
    properties = ga_api.list_properties(tokens.access_token)
    chosen = ga_api.pick_best_property(properties, business.website)
    if chosen is None:
        found = ", ".join(f"{prop.property_name} ({prop.website_url or 'no website'})" for prop in properties[:6]) or "no properties"
        if len(properties) > 6:
            found = f"{found}, and {len(properties) - 6} more"
        if not properties:
            message = "This Google account is not a user on any GA4 property. Open analytics.google.com with the same account. If the property is not there, add that Google user under Admin, then Property access management, and connect again."
        elif business.website:
            message = f"No Analytics property matches {business.website}. Google returned: {found}."
        else:
            message = f"This Google account has more than one Analytics property, and the business has no website to match. Google returned: {found}."
        logger.warning("Analytics property match failed for business %s: %s", business.id, message)
        raise AnalyticsError(message, code="no_property")

    try:
        events = ga_api.list_key_events(tokens.access_token, chosen.property_id)
    except ga_api.GoogleApiError as exc:
        logger.warning("Could not list GA4 key events for property %s: %s", chosen.property_id, exc)
        events = []
    secret = _token_encryption_secret()
    connection, _created = AnalyticsConnection.objects.update_or_create(
        business=business,
        defaults={
            "property_id": chosen.property_id,
            "property_name": chosen.property_name,
            "account_id": chosen.account_id,
            "website_url": chosen.website_url or "",
            "access_token_enc": google_crypto.encrypt_secret({"access_token": tokens.access_token}, secret),
            "refresh_token_enc": google_crypto.encrypt_secret({"refresh_token": tokens.refresh_token}, secret),
            "token_expires_at": timezone.now() + timedelta(seconds=tokens.expires_in),
            "scope": tokens.scope,
            "conversion_events": events,
            "last_sync_error": None,
        },
    )
    return connection


def get_fresh_access_token(connection: AnalyticsConnection) -> str:
    secret = _token_encryption_secret()

    if connection.token_expires_at - timezone.now() > timedelta(minutes=5):
        return google_crypto.decrypt_secret(connection.access_token_enc, secret)["access_token"]

    refresh_token = google_crypto.decrypt_secret(connection.refresh_token_enc, secret)["refresh_token"]
    refreshed = ga_api.refresh_access_token(client_id=_client_id(), client_secret=_client_secret(), refresh_token=refresh_token)

    connection.access_token_enc = google_crypto.encrypt_secret({"access_token": refreshed.access_token}, secret)
    connection.token_expires_at = timezone.now() + timedelta(seconds=refreshed.expires_in)
    connection.save(update_fields=["access_token_enc", "token_expires_at", "updated_at"])

    return refreshed.access_token


def sync_analytics(connection: AnalyticsConnection) -> None:
    """Re-pulls the organic daily window. Conversions are stored only when the property currently has key events."""
    business = connection.business
    today = timezone.localdate()

    try:
        access_token = get_fresh_access_token(connection)
        events = ga_api.list_key_events(access_token, connection.property_id)
        end = today - timedelta(days=2)
        start = end - timedelta(days=SYNC_WINDOW_DAYS)
        daily_rows = ga_api.fetch_organic_daily(
            access_token=access_token,
            property_id=connection.property_id,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
        )
        measurable = bool(events)

        with transaction.atomic():
            for row in daily_rows:
                AnalyticsDailyMetric.objects.update_or_create(
                    business=business,
                    date=row.day,
                    defaults={
                        "sessions": row.sessions,
                        "active_users": row.active_users,
                        "conversions": row.conversions if measurable else None,
                    },
                )

            connection.conversion_events = events
            connection.last_synced_at = timezone.now()
            connection.last_sync_error = None
            connection.save(update_fields=["conversion_events", "last_synced_at", "last_sync_error", "updated_at"])

    except (ga_api.GoogleApiError, AnalyticsError) as exc:
        logger.warning("Analytics sync failed for business %s: %s", business.id, exc)
        connection.last_sync_error = str(exc)
        connection.save(update_fields=["last_sync_error", "updated_at"])


def disconnect_analytics(business: Business) -> bool:
    connection = AnalyticsConnection.objects.filter(business=business).first()
    if connection is None:
        return False

    with transaction.atomic():
        connection.delete()
        AnalyticsDailyMetric.objects.filter(business=business).delete()

    return True


def get_overview(business: Business, days: int) -> dict:
    connection = AnalyticsConnection.objects.filter(business=business).first()
    measurable = bool(connection and connection.conversion_events)
    window = compute_range_window(days)

    current_rows = list(
        AnalyticsDailyMetric.objects.filter(business=business, date__gte=window.current.start, date__lte=window.current.end).values("date", "sessions", "active_users", "conversions")
    )
    previous_rows = list(
        AnalyticsDailyMetric.objects.filter(business=business, date__gte=window.previous.start, date__lte=window.previous.end).values("sessions", "active_users", "conversions")
    )

    current = summarize_period(current_rows, conversions_measurable=measurable)
    previous = summarize_period(previous_rows, conversions_measurable=measurable)
    comparison = None
    if previous.sessions > 0:
        comparison = {
            "sessions_delta_pct": percent_change(previous.sessions, current.sessions),
            "active_users_delta_pct": percent_change(previous.active_users, current.active_users),
            "conversions_delta_pct": percent_change(previous.conversions, current.conversions) if measurable and previous.conversions and current.conversions is not None else None,
        }

    return {
        "conversions_measurable": measurable,
        "current_rows": current_rows,
        "current": current,
        "comparison": comparison,
    }
