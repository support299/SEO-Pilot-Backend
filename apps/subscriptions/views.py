from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.history.services import record_event
from common.permissions import IsAgencyAdmin, get_business_or_404, is_agency_admin

from .models import PlanChangeRequest, ScopeChangeRequest, Subscription
from .serializers import (
    PlanChangeActionSerializer,
    PlanChangeCreateSerializer,
    ScopeDecisionSerializer,
    ScopeRequestCreateSerializer,
    SubscriptionWriteSerializer,
    customer_plan,
    scope_request_dict,
    staff_plan,
    staff_plan_change,
)
from .errors import RequestRejected
from .services import cancel_plan_change, confirm_plan_change, decide_scope_change, request_plan_change, request_scope_change


def plan_for(user, business) -> dict:
    subscription = Subscription.objects.filter(business=business).first()
    return staff_plan(subscription) if is_agency_admin(user) else customer_plan(subscription)


class PlanView(APIView):
    """GET /businesses/<id>/plan/ — the package and what it includes. Customers get the safe projection."""

    permission_classes = [IsAuthenticated]

    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        return Response(plan_for(request.user, business))


class SubscriptionView(APIView):
    """PUT /businesses/<id>/subscription/ — agency admins record or change the package after the customer has paid."""

    permission_classes = [IsAuthenticated, IsAgencyAdmin]

    def put(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        serializer = SubscriptionWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        existing = Subscription.objects.filter(business=business).first()
        previous = existing.package if existing else None
        subscription, created = Subscription.objects.update_or_create(business=business, defaults=data)

        if created:
            summary = f"Package set to {subscription.package_label}."
        elif previous != subscription.package:
            summary = f"Package changed to {subscription.package_label}."
        else:
            summary = f"Subscription updated ({subscription.package_label}, {subscription.status})."
        record_event(business, "subscription.updated", summary, actor=request.user, metadata={"package": subscription.package, "status": subscription.status})
        return Response(staff_plan(subscription), status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


def _rejected(error: RequestRejected) -> Response:
    return Response({"detail": error.message, "code": error.code}, status=error.status)


class ScopeRequestsView(APIView):
    """
    GET/POST /businesses/<id>/scope-requests/. Anyone with access to the website can ask for work outside the package,
    including read-only customers: it is only a request, it starts nothing.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        staff = is_agency_admin(request.user)
        return Response([scope_request_dict(r, staff=staff) for r in business.scope_requests.select_related("requested_by")[:100]])

    def post(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        serializer = ScopeRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            created = request_scope_change(business, serializer.validated_data["capability"], user=request.user, note=serializer.validated_data.get("note", ""))
        except RequestRejected as error:
            return _rejected(error)
        return Response(scope_request_dict(created, staff=is_agency_admin(request.user)), status=status.HTTP_201_CREATED)


class ScopeRequestDecisionView(APIView):
    """POST /businesses/<id>/scope-requests/<rid>/decision/ — agency admins only. `grant` adds the capability to the package."""

    permission_classes = [IsAuthenticated, IsAgencyAdmin]

    def post(self, request, business_id, request_id):
        business = get_business_or_404(request.user, business_id)
        scope_request = get_object_or_404(ScopeChangeRequest, pk=request_id, business=business)
        serializer = ScopeDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            decided = decide_scope_change(scope_request, approve=data["decision"] == "approve", grant=data["grant"], decision_note=data.get("decision_note", ""), user=request.user)
        except RequestRejected as error:
            return _rejected(error)
        return Response(scope_request_dict(decided, staff=True))


class PlanChangesView(APIView):
    """GET (staff) lists plan changes; POST (staff) records written notice of an upgrade or downgrade."""

    permission_classes = [IsAuthenticated, IsAgencyAdmin]

    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        return Response([staff_plan_change(c) for c in business.plan_changes.all()[:50]])

    def post(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        serializer = PlanChangeCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            change = request_plan_change(
                business,
                data["requested_package"],
                user=request.user,
                notice_received_at=data.get("notice_received_at"),
                requested_effective_on=data.get("requested_effective_on"),
                note=data.get("note", ""),
            )
        except RequestRejected as error:
            return _rejected(error)
        return Response(staff_plan_change(change), status=status.HTTP_201_CREATED)


class PlanChangeActionView(APIView):
    """POST /businesses/<id>/plan-changes/<pid>/ {action: confirm|cancel} — staff confirm the effective date or cancel."""

    permission_classes = [IsAuthenticated, IsAgencyAdmin]

    def post(self, request, business_id, change_id):
        business = get_business_or_404(request.user, business_id)
        change = get_object_or_404(PlanChangeRequest, pk=change_id, business=business)
        serializer = PlanChangeActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            if data["action"] == "confirm":
                change = confirm_plan_change(change, data["effective_on"], user=request.user)
            else:
                change = cancel_plan_change(change, user=request.user)
        except RequestRejected as error:
            return _rejected(error)
        return Response(staff_plan_change(change))
