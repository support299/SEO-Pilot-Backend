import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.http import urlencode
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.history.services import record_event
from common.permissions import get_business_or_404
from integrations.google import crypto as google_crypto
from integrations.google.analytics import GoogleApiError

from . import services
from .metrics import parse_range_param
from .models import AnalyticsConnection
from .serializers import ConnectionStatusSerializer
from .tasks import sync_business_analytics

logger = logging.getLogger("django")


class ConnectionStatusView(APIView):
    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        connection = AnalyticsConnection.objects.filter(business=business).first()
        if not connection:
            return Response({"connected": False})
        return Response(ConnectionStatusSerializer(connection).data)


class AuthorizeUrlView(APIView):
    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        redirect_uri = request.build_absolute_uri(reverse("analytics:callback"))
        try:
            url = services.build_authorization_url(business_id=str(business.id), user_id=str(request.user.id), redirect_uri=redirect_uri)
        except services.AnalyticsError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({"url": url})


class CallbackView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        denied = request.query_params.get("error")
        business_id = None

        if denied:
            return self._redirect_to_frontend(business_id=None, params={"gaError": "denied"})

        try:
            if not code or not state:
                raise services.AnalyticsError("Google did not return the expected authorization response.")

            payload = services.read_oauth_state(state)
            business_id = payload.business_id
            try:
                user = get_user_model().objects.get(pk=payload.user_id)
            except get_user_model().DoesNotExist as exc:
                raise services.AnalyticsError("The signed-in user no longer exists.") from exc
            business = get_business_or_404(user, payload.business_id)

            redirect_uri = payload.redirect_uri or request.build_absolute_uri(reverse("analytics:callback"))
            connection = services.complete_connection(business=business, code=code, redirect_uri=redirect_uri)
            record_event(business, "analytics.connected", f"Connected Google Analytics ({connection.property_name}).", actor=user)
            _enqueue_sync(business.id)
            return self._redirect_to_frontend(business_id=business.id, params={"gaConnected": "1"})
        except services.AnalyticsError as exc:
            logger.warning("Analytics connection failed: %s", exc)
            return self._redirect_to_frontend(business_id=business_id, params=_error_params(exc.code, str(exc)))
        except (GoogleApiError, google_crypto.InvalidOAuthState) as exc:
            logger.warning("Analytics connection failed: %s", exc)
            return self._redirect_to_frontend(business_id=business_id, params=_error_params("connection_failed", str(exc)))

    def _redirect_to_frontend(self, *, business_id, params: dict) -> HttpResponseRedirect:
        path = f"/businesses/{business_id}/connections" if business_id else "/"
        return HttpResponseRedirect(f"{settings.FRONTEND_URL}{path}?{urlencode(params)}")


def _error_params(code: str, message: str) -> dict:
    params = {"gaError": code}
    text = " ".join(message.split())
    if text:
        params["gaDetail"] = text[:400]
    return params


def _enqueue_sync(business_id: int) -> bool:
    try:
        sync_business_analytics.delay(business_id)
    except Exception:
        logger.exception("Could not enqueue Analytics sync for business %s", business_id)
        return False
    return True


class DisconnectView(APIView):
    def post(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        removed = services.disconnect_analytics(business)
        if not removed:
            return Response({"detail": "Google Analytics is not connected for this business."}, status=status.HTTP_400_BAD_REQUEST)
        record_event(business, "analytics.disconnected", "Disconnected Google Analytics and removed its stored data.", actor=request.user)
        return Response({"connected": False})


class SyncView(APIView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "sync"

    def post(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        connection = AnalyticsConnection.objects.filter(business=business).first()
        if not connection:
            return Response({"detail": "Google Analytics is not connected for this business."}, status=status.HTTP_400_BAD_REQUEST)
        if not _enqueue_sync(business.id):
            return Response(
                {"detail": "Google Analytics is connected, but the sync queue is unavailable. Start Redis and the Celery worker, then try again."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({"detail": "Sync started."}, status=status.HTTP_202_ACCEPTED)


class OverviewView(APIView):
    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        days = parse_range_param(request.query_params.get("range"))
        result = services.get_overview(business, days)
        current = result["current"]
        return Response(
            {
                "range_days": days,
                "conversions_measurable": result["conversions_measurable"],
                "rows": [
                    {
                        "date": row["date"].isoformat(),
                        "sessions": row["sessions"],
                        "active_users": row["active_users"],
                        "conversions": row["conversions"],
                    }
                    for row in result["current_rows"]
                ],
                "summary": {
                    "sessions": current.sessions,
                    "active_users": current.active_users,
                    "conversions": current.conversions,
                },
                "comparison": result["comparison"],
            }
        )
