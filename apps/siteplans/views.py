"""Site plan endpoints for people (JWT): everyone with access reads; agency admins decide and record progress."""

from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.subscriptions.errors import RequestRejected
from common.permissions import IsAgencyAdmin, get_business_or_404, is_agency_admin

from . import services
from .models import PlanTask, SitePlan
from .serializers import DecisionSerializer, TaskUpdateSerializer


def rejected(error: RequestRejected) -> Response:
    return Response({"detail": error.message, "code": error.code}, status=error.status)


class SitePlansView(APIView):
    """GET /businesses/<id>/site-plans/ — customers see the active plan only, with no estimates or evidence."""

    permission_classes = [IsAuthenticated]

    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        return Response(services.workspace(business, internal=is_agency_admin(request.user)))


class SitePlanDecisionView(APIView):
    """POST /businesses/<id>/site-plans/<pid>/decision/ {decision: approve|request_changes|reject, note}."""

    permission_classes = [IsAuthenticated, IsAgencyAdmin]

    def post(self, request, business_id, plan_id):
        business = get_business_or_404(request.user, business_id)
        plan = get_object_or_404(SitePlan, pk=plan_id, business=business)
        serializer = DecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.decide(plan, serializer.validated_data["decision"], user=request.user, note=serializer.validated_data.get("note", ""))
        except RequestRejected as error:
            return rejected(error)
        return Response(services.workspace(business, internal=True))


class SitePlanTaskProgressView(APIView):
    """PATCH /businesses/<id>/site-plans/tasks/<tid>/ — staff record progress on an approved plan's task."""

    permission_classes = [IsAuthenticated, IsAgencyAdmin]

    def patch(self, request, business_id, task_id):
        business = get_business_or_404(request.user, business_id)
        task = get_object_or_404(PlanTask.objects.select_related("plan", "feature"), pk=task_id, plan__business=business)
        serializer = TaskUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        try:
            services.update_task(task, dict(serializer.validated_data), user=request.user)
        except RequestRejected as error:
            return rejected(error)
        return Response(services.task_dict(task, internal=True))
