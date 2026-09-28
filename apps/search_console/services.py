from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.businesses.models import Business
from integrations.google import crypto as google_crypto
from integrations.google import search_console as gsc_api

from .metrics import compute_range_window, summarize_period
from .models import SearchConsoleConnection, SearchConsoleDailyMetric, SearchConsoleTopPage, SearchConsoleTopQuery

logger = logging.getLogger("django")

SYNC_WINDOW_DAYS = 186  # up to a 90-day range + a full previous 90-day comparison window, plus buffer
TOP_CONTENT_WINDOW_DAYS = 28


class SearchConsoleError(Exception):
    pass


def _client_id() -> str:
    value = settings.GOOGLE_OAUTH_CLIENT_ID
    if not value:
        raise SearchConsoleError("Google Search Console is not configured on this server yet.")
    return value


def _client_secret() -> str:
    value = settings.GOOGLE_OAUTH_CLIENT_SECRET
    if not value:
        raise SearchConsoleError("Google Search Console is not configured on this server yet.")
    return value


def _state_secret() -> str:
    return settings.SEARCH_CONSOLE_OAUTH_STATE_SECRET


def _token_encryption_secret() -> str:
    return settings.SEARCH_CONSOLE_TOKEN_ENCRYPTION_KEY


def build_authorization_url(*, business_id: str, user_id: str, redirect_uri: str) -> str:
    state = google_crypto.sign_oauth_state(business_id, user_id, _state_secret())
    return gsc_api.build_authorization_url(client_id=_client_id(), redirect_uri=redirect_uri, state=state)


def read_oauth_state(state: str) -> google_crypto.OAuthStatePayload:
    return google_crypto.verify_oauth_state(state, _state_secret())


@transaction.atomic
def complete_connection(*, business: Business, code: str, redirect_uri: str) -> SearchConsoleConnection:
    tokens = gsc_api.exchange_code_for_tokens(client_id=_client_id(), client_secret=_client_secret(), code=code, redirect_uri=redirect_uri)
    sites = gsc_api.list_sites(tokens.access_token)
    site = gsc_api.pick_best_site(sites, business.website)
    if site is None:
        raise SearchConsoleError("No Search Console properties are available for this Google account.")

    secret = _token_encryption_secret()
    connection, _created = SearchConsoleConnection.objects.update_or_create(
        business=business,
        defaults={
            "site_url": site.site_url,
            "access_token_enc": google_crypto.encrypt_secret({"access_token": tokens.access_token}, secret),
            "refresh_token_enc": google_crypto.encrypt_secret({"refresh_token": tokens.refresh_token}, secret),
            "token_expires_at": timezone.now() + timedelta(seconds=tokens.expires_in),
            "scope": tokens.scope,
            "last_sync_error": None,
        },
    )
    return connection


def get_fresh_access_token(connection: SearchConsoleConnection) -> str:
    secret = _token_encryption_secret()

    if connection.token_expires_at - timezone.now() > timedelta(minutes=5):
        return google_crypto.decrypt_secret(connection.access_token_enc, secret)["access_token"]

    refresh_token = google_crypto.decrypt_secret(connection.refresh_token_enc, secret)["refresh_token"]
    refreshed = gsc_api.refresh_access_token(client_id=_client_id(), client_secret=_client_secret(), refresh_token=refresh_token)

    connection.access_token_enc = google_crypto.encrypt_secret({"access_token": refreshed.access_token}, secret)
    connection.token_expires_at = timezone.now() + timedelta(seconds=refreshed.expires_in)
    connection.save(update_fields=["access_token_enc", "token_expires_at", "updated_at"])

    return refreshed.access_token


def sync_search_console(connection: SearchConsoleConnection) -> None:
    """
    Pulls the last ~186 days of daily performance plus a fresh top-queries/
    top-pages snapshot from Google, and stores it as this app's own history.
    Re-pulling the full daily window each time (an upsert, not an append)
    keeps late-arriving/corrected data accurate without a separate backfill
    mechanism — same approach as the old Next.js app used.
    """

    business = connection.business
    today = timezone.localdate()

    try:
        access_token = get_fresh_access_token(connection)

        end = today - timedelta(days=2)  # Search Console data has ~2 days' lag before it's "final"
        start = end - timedelta(days=SYNC_WINDOW_DAYS)
        daily_rows = gsc_api.fetch_daily_analytics(access_token=access_token, site_url=connection.site_url, start_date=start.isoformat(), end_date=end.isoformat())

        content_start = end - timedelta(days=TOP_CONTENT_WINDOW_DAYS)
        top_queries = gsc_api.fetch_top_queries(access_token=access_token, site_url=connection.site_url, start_date=content_start.isoformat(), end_date=end.isoformat())
        top_pages = gsc_api.fetch_top_pages(access_token=access_token, site_url=connection.site_url, start_date=content_start.isoformat(), end_date=end.isoformat())

        with transaction.atomic():
            for row in daily_rows:
                SearchConsoleDailyMetric.objects.update_or_create(
                    business=business,
                    date=row.key,
                    defaults={"clicks": row.clicks, "impressions": row.impressions, "ctr": row.ctr, "position": row.position},
                )

            SearchConsoleTopQuery.objects.filter(business=business).delete()
            SearchConsoleTopQuery.objects.bulk_create(
                [SearchConsoleTopQuery(business=business, query=row.key, clicks=row.clicks, impressions=row.impressions, ctr=row.ctr, position=row.position) for row in top_queries]
            )

            SearchConsoleTopPage.objects.filter(business=business).delete()
            SearchConsoleTopPage.objects.bulk_create(
                [SearchConsoleTopPage(business=business, page=row.key, clicks=row.clicks, impressions=row.impressions, ctr=row.ctr, position=row.position) for row in top_pages]
            )

            connection.last_synced_at = timezone.now()
            connection.last_sync_error = None
            connection.save(update_fields=["last_synced_at", "last_sync_error", "updated_at"])

    except (gsc_api.GoogleApiError, SearchConsoleError) as exc:
        logger.warning("Search Console sync failed for business %s: %s", business.id, exc)
        connection.last_sync_error = str(exc)
        connection.save(update_fields=["last_sync_error", "updated_at"])


def disconnect_search_console(business: Business) -> bool:
    """Removes the saved Google connection and the metrics that came from it."""
    connection = SearchConsoleConnection.objects.filter(business=business).first()
    if connection is None:
        return False

    with transaction.atomic():
        connection.delete()
        SearchConsoleDailyMetric.objects.filter(business=business).delete()
        SearchConsoleTopQuery.objects.filter(business=business).delete()
        SearchConsoleTopPage.objects.filter(business=business).delete()

    return True


def get_overview(business: Business, days: int) -> dict:
    window = compute_range_window(days)

    current_rows = list(
        SearchConsoleDailyMetric.objects.filter(business=business, date__gte=window.current.start, date__lte=window.current.end).values("date", "clicks", "impressions", "ctr", "position")
    )
    previous_rows = list(
        SearchConsoleDailyMetric.objects.filter(business=business, date__gte=window.previous.start, date__lte=window.previous.end).values("clicks", "impressions", "position")
    )

    current_summary = summarize_period(current_rows)
    previous_summary = summarize_period(previous_rows)

    return {
        "current_rows": current_rows,
        "current_summary": current_summary,
        "previous_summary": previous_summary,
    }
