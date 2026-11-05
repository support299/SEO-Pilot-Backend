"""
Customer-safe vs staff projections of a subscription.

The customer projection is an explicit allow-list built by hand (not a ModelSerializer with `exclude`), so a new
model field can never reach a customer by accident.
"""

from rest_framework import serializers

from .entitlements import capability_rows
from .models import PlanChangeRequest, ScopeChangeRequest, Subscription
from .packages import CAPABILITY_BY_KEY, PACKAGE_LABELS, Package


def customer_plan(subscription: Subscription | None) -> dict:
    if subscription is None:
        return {"pending_change": None, "subscribed": False, "package": None, "package_label": None, "status": "none", "monthly_price_usd": None, "started_on": None, "capabilities": []}
    return {
        "pending_change": customer_plan_change(open_plan_change(subscription)),
        "subscribed": subscription.status == Subscription.Status.ACTIVE,
        "package": subscription.package,
        "package_label": subscription.package_label,
        "status": subscription.status,
        "monthly_price_usd": subscription.monthly_price_usd,
        "started_on": subscription.started_on.isoformat() if subscription.started_on else None,
        "capabilities": capability_rows(subscription),
    }


def staff_plan(subscription: Subscription | None) -> dict:
    plan = customer_plan(subscription)
    plan["staff"] = (
        None
        if subscription is None
        else {
            "external_reference": subscription.external_reference,
            "internal_notes": subscription.internal_notes,
            "contracted_price_usd": subscription.contracted_price_usd,
            "ended_on": subscription.ended_on.isoformat() if subscription.ended_on else None,
        }
    )
    return plan


def open_plan_change(subscription: Subscription) -> PlanChangeRequest | None:
    return subscription.business.plan_changes.filter(status__in=[PlanChangeRequest.Status.PENDING, PlanChangeRequest.Status.CONFIRMED]).first()


def customer_plan_change(change: PlanChangeRequest | None) -> dict | None:
    if change is None:
        return None
    confirmed = change.status == PlanChangeRequest.Status.CONFIRMED
    return {
        "id": change.id,
        "requested_package": change.requested_package,
        "requested_package_label": PACKAGE_LABELS[Package(change.requested_package)],
        "effective_on": change.confirmed_effective_on.isoformat() if confirmed and change.confirmed_effective_on else None,
        "effective_date_label": change.confirmed_effective_on.isoformat() if confirmed and change.confirmed_effective_on else "Effective date awaiting confirmation",
    }


def staff_plan_change(change: PlanChangeRequest) -> dict:
    return {
        "id": change.id,
        "from_package": change.from_package,
        "requested_package": change.requested_package,
        "requested_package_label": PACKAGE_LABELS[Package(change.requested_package)],
        "status": change.status,
        "notice_received_at": change.notice_received_at.isoformat(),
        "requested_effective_on": change.requested_effective_on.isoformat() if change.requested_effective_on else None,
        "confirmed_effective_on": change.confirmed_effective_on.isoformat() if change.confirmed_effective_on else None,
        "note": change.note,
        "created_at": change.created_at.isoformat(),
    }


def scope_request_dict(scope_request: ScopeChangeRequest, *, staff: bool) -> dict:
    data = {
        "id": scope_request.id,
        "capability": scope_request.capability,
        "capability_label": CAPABILITY_BY_KEY[scope_request.capability].label,
        "status": scope_request.status,
        "note": scope_request.note,
        "decision_note": scope_request.decision_note,
        "created_at": scope_request.created_at.isoformat(),
        "decided_at": scope_request.decided_at.isoformat() if scope_request.decided_at else None,
    }
    if staff:
        data["requested_by"] = scope_request.requested_by.email if scope_request.requested_by else (f"agent: {scope_request.requested_by_agent}" if scope_request.requested_by_agent else None)
    return data


class ScopeRequestCreateSerializer(serializers.Serializer):
    capability = serializers.CharField(max_length=60)
    note = serializers.CharField(max_length=2000, required=False, allow_blank=True)


class ScopeDecisionSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=["approve", "decline"])
    grant = serializers.BooleanField(required=False, default=False)
    decision_note = serializers.CharField(max_length=2000, required=False, allow_blank=True)


class PlanChangeCreateSerializer(serializers.Serializer):
    requested_package = serializers.ChoiceField(choices=[p.value for p in Package])
    notice_received_at = serializers.DateTimeField(required=False)
    requested_effective_on = serializers.DateField(required=False, allow_null=True)
    note = serializers.CharField(max_length=2000, required=False, allow_blank=True)


class PlanChangeActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=["confirm", "cancel"])
    effective_on = serializers.DateField(required=False)

    def validate(self, attrs):
        if attrs["action"] == "confirm" and "effective_on" not in attrs:
            raise serializers.ValidationError({"effective_on": "Give the confirmed effective date."})
        return attrs


class SubscriptionWriteSerializer(serializers.Serializer):
    package = serializers.ChoiceField(choices=[(p.value, PACKAGE_LABELS[p]) for p in Package])
    status = serializers.ChoiceField(choices=Subscription.Status.choices, required=False)
    started_on = serializers.DateField(required=False, allow_null=True)
    ended_on = serializers.DateField(required=False, allow_null=True)
    contracted_price_usd = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    external_reference = serializers.CharField(max_length=200, required=False, allow_blank=True)
    internal_notes = serializers.CharField(required=False, allow_blank=True)
