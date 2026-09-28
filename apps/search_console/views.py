from django.conf import settings
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.http import urlencode
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from common.permissions import get_business_or_404
from integrations.google import crypto as google_crypto
from integrations.google.search_console import GoogleApiError

from . import services
from .metrics import compare_periods, has_comparable_data, parse_range_param
from .models import SearchConsoleConnection, SearchConsoleTopPage, SearchConsoleTopQuery
from .serializers import ConnectionStatusSerializer, DailyMetricSerializer, TopPageSerializer, TopQuerySerializer
from .tasks import sync_business_search_console


class ConnectionStatusView(APIView):
    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        connection = SearchConsoleConnection.objects.filter(business=business).first()

        if not connection:
            return Response({"connected": False})

        return Response(ConnectionStatusSerializer(connection).data)


class AuthorizeUrlView(APIView):
    """
    Returns the Google authorization URL as JSON rather than redirecting
    directly — the frontend must call this via an authenticated request
    (Bearer token) to prove who's connecting, then navigate the browser to
    the returned URL itself. A raw browser navigation to this endpoint
    wouldn't carry the Authorization header, which is why this can't just be
    a redirect view the way it was in the old same-origin Next.js app.
    """

    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        redirect_uri = request.build_absolute_uri(reverse("search_console:callback"))

        try:
            url = services.build_authorization_url(business_id=str(business.id), user_id=str(request.user.id), redirect_uri=redirect_uri)
        except services.SearchConsoleError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response({"url": url})


class CallbackView(APIView):
    """
    Google redirects the browser here directly (no Authorization header
    possible) — identity is established entirely from the signed `state`
    parameter (HMAC-verified, short-lived), then double-checked against real
    membership before anything is saved. Always ends by redirecting the
    browser to the frontend, not returning JSON — this is a page navigation,
    not an API call.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        denied = request.query_params.get("error")

        if denied:
            return self._redirect_to_frontend(business_id=None, params={"scError": "denied"})

        try:
            if not code or not state:
                raise services.SearchConsoleError("Google did not return the expected authorization response.")

            payload = services.read_oauth_state(state)
            business = get_business_or_404(_UserIdShim(payload.user_id), payload.business_id)

            redirect_uri = request.build_absolute_uri(reverse("search_console:callback"))
            services.complete_connection(business=business, code=code, redirect_uri=redirect_uri)

            sync_business_search_console.delay(business.id)

            return self._redirect_to_frontend(business_id=business.id, params={"scConnected": "1"})
        except (services.SearchConsoleError, GoogleApiError, google_crypto.InvalidOAuthState):
            return self._redirect_to_frontend(business_id=None, params={"scError": "connection_failed"})

    def _redirect_to_frontend(self, *, business_id, params: dict) -> HttpResponseRedirect:
        path = f"/businesses/{business_id}/performance" if business_id else "/"
        return HttpResponseRedirect(f"{settings.FRONTEND_URL}{path}?{urlencode(params)}")


class _UserIdShim:
    """get_business_or_404 only needs `.id` off whatever's passed as `user` — this avoids an extra DB query for a User we don't otherwise need in the callback."""

    def __init__(self, user_id: str):
        self.id = user_id


class DisconnectView(APIView):
    def post(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        removed = services.disconnect_search_console(business)
        if not removed:
            return Response({"detail": "Google Search Console is not connected for this business."}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"connected": False})


class SyncView(APIView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "sync"

    def post(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        connection = SearchConsoleConnection.objects.filter(business=business).first()

        if not connection:
            return Response({"detail": "Google Search Console is not connected for this business."}, status=status.HTTP_400_BAD_REQUEST)

        sync_business_search_console.delay(business.id)
        return Response({"detail": "Sync started."}, status=status.HTTP_202_ACCEPTED)


class OverviewView(APIView):
    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        days = parse_range_param(request.query_params.get("range"))

        result = services.get_overview(business, days)
        current_summary, previous_summary = result["current_summary"], result["previous_summary"]
        comparison = compare_periods(current_summary, previous_summary) if has_comparable_data(previous_summary) else None

        return Response(
            {
                "range_days": days,
                "rows": DailyMetricSerializer(result["current_rows"], many=True).data,
                "summary": {
                    "clicks": current_summary.clicks,
                    "impressions": current_summary.impressions,
                    "ctr": current_summary.ctr,
                    "average_position": current_summary.average_position,
                },
                "comparison": None
                if comparison is None
                else {
                    "clicks_delta_pct": comparison.clicks_delta_pct,
                    "impressions_delta_pct": comparison.impressions_delta_pct,
                    "ctr_delta_pct": comparison.ctr_delta_pct,
                    "position_delta": comparison.position_delta,
                },
            }
        )


class TopQueriesView(APIView):
    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        queries = SearchConsoleTopQuery.objects.filter(business=business)[:10]
        return Response(TopQuerySerializer(queries, many=True).data)


class TopPagesView(APIView):
    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        pages = SearchConsoleTopPage.objects.filter(business=business)[:10]
        return Response(TopPageSerializer(pages, many=True).data)
