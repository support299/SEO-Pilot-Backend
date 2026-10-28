import logging

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from common.permissions import get_business_or_404, is_agency_admin

from . import services
from .models import GridScan, TrackedKeyword
from .tasks import run_grid_scan

logger = logging.getLogger("django")


class ProfileSerializer(serializers.Serializer):
    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)
    grid_size = serializers.IntegerField(default=5)
    radius_km = serializers.FloatField(default=5)
    business_domain = serializers.CharField(required=False, allow_blank=True, default="")
    place_id = serializers.CharField(required=False, allow_blank=True, default="")


class KeywordSerializer(serializers.Serializer):
    keyword = serializers.CharField(max_length=200)


def _error(exc: services.LocalVisibilityError) -> Response:
    return Response({"detail": str(exc)}, status=exc.status_code)


class OverviewView(APIView):
    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        return Response(services.overview(business, include_cost=is_agency_admin(request.user)))


class ProfileView(APIView):
    def put(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        serializer = ProfileSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            profile = services.save_profile(business, **serializer.validated_data)
        except services.LocalVisibilityError as exc:
            return _error(exc)
        return Response(services.serialize_profile(profile, business))


class KeywordsView(APIView):
    def post(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        serializer = KeywordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            keyword = services.add_keyword(business, serializer.validated_data["keyword"])
        except services.LocalVisibilityError as exc:
            return _error(exc)
        return Response({"id": keyword.id, "keyword": keyword.keyword}, status=status.HTTP_201_CREATED)


class KeywordDetailView(APIView):
    def get(self, request, business_id, keyword_id):
        business = get_business_or_404(request.user, business_id)
        keyword = get_object_or_404(TrackedKeyword, id=keyword_id, business=business)
        return Response(services.keyword_detail(keyword, include_cost=is_agency_admin(request.user)))

    def delete(self, request, business_id, keyword_id):
        business = get_business_or_404(request.user, business_id)
        keyword = get_object_or_404(TrackedKeyword, id=keyword_id, business=business)
        keyword.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class KeywordScanView(APIView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "sync"

    def post(self, request, business_id, keyword_id):
        business = get_business_or_404(request.user, business_id)
        keyword = get_object_or_404(TrackedKeyword, id=keyword_id, business=business)
        try:
            scan = services.start_scan(keyword)
        except services.LocalVisibilityError as exc:
            return _error(exc)

        try:
            run_grid_scan.delay(scan.id)
        except Exception:
            logger.exception("Could not enqueue grid scan for keyword %s", keyword.id)
            scan.status = GridScan.Status.FAILED
            scan.error = "The scan queue is unavailable. Start Redis and the Celery worker, then try again."
            scan.finished_at = timezone.now()
            scan.save(update_fields=["status", "error", "finished_at"])
            return Response({"detail": scan.error}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response({"detail": "Scan started.", "id": scan.id}, status=status.HTTP_202_ACCEPTED)
