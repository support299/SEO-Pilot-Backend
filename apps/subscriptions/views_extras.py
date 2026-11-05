"""Optional-benefit consent and per-website checklist endpoints."""

from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from common.permissions import IsAgencyAdmin, get_business_or_404, is_agency_admin

from . import checklist as checklist_service
from .consent import BENEFIT_COPY, consent_state, latest_consent, record_consent
from .errors import RequestRejected
from .models import ChecklistItem, ConsentRecord, Subscription


def _rejected(error: RequestRejected) -> Response:
    return Response({"detail": error.message, "code": error.code}, status=error.status)


# --- optional benefits ---------------------------------------------------------------------------------------------

def benefits_payload(business, *, staff: bool) -> list[dict]:
    subscription = Subscription.objects.filter(business=business).first()
    rows = []
    for benefit in ConsentRecord.Benefit:
        if benefit == ConsentRecord.Benefit.HOSTING_AFTER_CANCELLATION and not (subscription and subscription.status == Subscription.Status.CANCELLED):
            continue  # only offered once SEO has been cancelled
        record = latest_consent(business, benefit.value)
        row = {
            "key": benefit.value,
            "label": benefit.label,
            "description": BENEFIT_COPY[benefit],
            "state": consent_state(business, benefit.value),
            "decided_on": record.recorded_at.date().isoformat() if record else None,
        }
        if staff and record:
            row["consented_by_name"] = record.consented_by_name
            row["evidence"] = record.evidence
            row["recorded_by"] = record.recorded_by.email if record.recorded_by else None
        rows.append(row)
    return rows


class BenefitsView(APIView):
    """GET /businesses/<id>/benefits/ — current state of each optional benefit. Nothing is ever pre-selected."""

    permission_classes = [IsAuthenticated]

    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        return Response(benefits_payload(business, staff=is_agency_admin(request.user)))


class ConsentWriteSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=ConsentRecord.Decision.values)
    consented_by_name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    evidence = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    terms_accepted = serializers.BooleanField(required=False, default=False)
    terms_reference = serializers.CharField(max_length=300, required=False, allow_blank=True)
    workflow_authority_confirmed = serializers.BooleanField(required=False, default=False)
    recurring_charge_acknowledged = serializers.BooleanField(required=False, default=False)
    note = serializers.CharField(max_length=2000, required=False, allow_blank=True)


class BenefitDecisionView(APIView):
    """POST /businesses/<id>/benefits/<benefit>/ — staff record what the customer decided, with the evidence."""

    permission_classes = [IsAuthenticated, IsAgencyAdmin]

    def post(self, request, business_id, benefit):
        business = get_business_or_404(request.user, business_id)
        serializer = ConsentWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        try:
            record_consent(business, benefit, data.pop("decision"), user=request.user, data=data)
        except RequestRejected as error:
            return _rejected(error)
        return Response(benefits_payload(business, staff=True), status=status.HTTP_201_CREATED)


# --- checklist -----------------------------------------------------------------------------------------------------

class ChecklistView(APIView):
    """GET /businesses/<id>/checklist/ — every item with its status. Items nobody has checked read "not_checked"."""

    permission_classes = [IsAuthenticated]

    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        return Response(checklist_service.checklist_rows(business, staff=is_agency_admin(request.user)))


class ChecklistUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=ChecklistItem.Status.values)
    finding = serializers.CharField(max_length=4000, required=False, allow_blank=True)
    evidence = serializers.CharField(max_length=4000, required=False, allow_blank=True)
    owner = serializers.CharField(max_length=200, required=False, allow_blank=True)
    next_action = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    blocker = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    reason = serializers.CharField(max_length=2000, required=False, allow_blank=True)


class ChecklistItemView(APIView):
    """PUT /businesses/<id>/checklist/<feature_id>/ — staff record a finding or move an item. History is kept."""

    permission_classes = [IsAuthenticated, IsAgencyAdmin]

    def put(self, request, business_id, feature_id):
        business = get_business_or_404(request.user, business_id)
        serializer = ChecklistUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            checklist_service.update_item(business, feature_id.upper(), dict(serializer.validated_data), user=request.user)
        except RequestRejected as error:
            return _rejected(error)
        rows = checklist_service.checklist_rows(business, staff=True)["rows"]
        return Response(next(row for row in rows if row["feature_id"] == feature_id.upper()))

    def get(self, request, business_id, feature_id):
        business = get_business_or_404(request.user, business_id)
        return Response(checklist_service.item_history(business, feature_id.upper()))
