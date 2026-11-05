from datetime import date

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.businesses.models import Business
from apps.history.services import record_event

from .errors import RequestRejected
from .entitlements import entitlement, resolve_capability
from .models import PlanChangeRequest, ScopeChangeRequest, Subscription
from .packages import PACKAGE_LABELS, Level, Package


def request_scope_change(business: Business, capability_key: str, *, user, note: str = "", agent_name: str = "") -> ScopeChangeRequest:
    """Record a request for work the package doesn't cover. Never starts work and never creates a charge."""
    try:
        capability = resolve_capability(capability_key)
    except ValueError:
        raise RequestRejected("unknown_capability", "That item isn't part of the service list.")
    result = entitlement(business, capability.key)
    if result.level is None:
        raise RequestRejected("no_active_subscription", "This website has no active package.")
    if result.level == Level.INCLUDED:
        raise RequestRejected("already_included", "That is already included in this package.")
    if result.level == Level.OPTIONAL:
        raise RequestRejected("optional_benefit", "That is an optional benefit you turn on with consent, not a scope change.")
    try:
        with transaction.atomic():
            scope_request = ScopeChangeRequest.objects.create(business=business, capability=capability.key, note=note.strip(), requested_by=user, requested_by_agent=agent_name)
    except IntegrityError:
        raise RequestRejected("already_requested", "There is already an open request for that.", status=409)
    by = f" (by {agent_name})" if agent_name else ""
    record_event(business, "scope.requested", f"Scope change requested: {capability.label}{by}.", actor=user, metadata={"capability": capability.key, **({"agent": agent_name} if agent_name else {})})
    return scope_request


def decide_scope_change(scope_request: ScopeChangeRequest, *, approve: bool, grant: bool, decision_note: str, user) -> ScopeChangeRequest:
    if scope_request.status != ScopeChangeRequest.Status.OPEN:
        raise RequestRejected("already_decided", "This request was already decided.", status=409)
    with transaction.atomic():
        scope_request.status = ScopeChangeRequest.Status.APPROVED if approve else ScopeChangeRequest.Status.DECLINED
        scope_request.decided_by, scope_request.decided_at, scope_request.decision_note = user, timezone.now(), decision_note.strip()
        scope_request.save()
        if approve and grant:
            subscription = Subscription.objects.select_for_update().get(business=scope_request.business)
            if scope_request.capability not in subscription.granted_capabilities:
                subscription.granted_capabilities = [*subscription.granted_capabilities, scope_request.capability]
                subscription.save(update_fields=["granted_capabilities", "updated_at"])
    verb = "approved" if approve else "declined"
    record_event(scope_request.business, "scope.decided", f"Scope change {verb}: {resolve_capability(scope_request.capability).label}.", actor=user, metadata={"capability": scope_request.capability, "granted": bool(approve and grant)})
    return scope_request


def request_plan_change(business: Business, package: str, *, user, notice_received_at=None, requested_effective_on: date | None = None, note: str = "") -> PlanChangeRequest:
    """Record written notice of an upgrade/downgrade. Does not pick an effective date or calculate proration."""
    subscription = Subscription.objects.filter(business=business).first()
    if subscription is None or subscription.status != Subscription.Status.ACTIVE:
        raise RequestRejected("no_active_subscription", "This website has no active package to change.")
    if package == subscription.package:
        raise RequestRejected("same_package", "The website is already on that package.")
    try:
        with transaction.atomic():
            change = PlanChangeRequest.objects.create(
                business=business,
                from_package=subscription.package,
                requested_package=package,
                notice_received_at=notice_received_at or timezone.now(),
                requested_effective_on=requested_effective_on,
                note=note,
                created_by=user,
            )
    except IntegrityError:
        raise RequestRejected("change_already_open", "A plan change is already open for this website.", status=409)
    record_event(business, "plan.change_requested", f"Plan change to {PACKAGE_LABELS[Package(package)]} recorded; effective date awaiting confirmation.", actor=user, metadata={"from": subscription.package, "to": package})
    return change


def confirm_plan_change(change: PlanChangeRequest, effective_on: date, *, user) -> PlanChangeRequest:
    """Staff set the date after written confirmation. Back-dating is refused; a date of today applies immediately."""
    if change.status != PlanChangeRequest.Status.PENDING:
        raise RequestRejected("not_pending", "Only a change awaiting confirmation can be confirmed.", status=409)
    if effective_on < timezone.localdate():
        raise RequestRejected("date_in_past", "The effective date can't be in the past.")
    change.confirmed_effective_on = effective_on
    change.status = PlanChangeRequest.Status.CONFIRMED
    change.save(update_fields=["confirmed_effective_on", "status"])
    record_event(change.business, "plan.change_confirmed", f"Plan change confirmed for {effective_on.isoformat()}.", actor=user, metadata={"effective_on": effective_on.isoformat()})
    if effective_on <= timezone.localdate():
        apply_plan_change(change.pk)
        change.refresh_from_db()
    return change


def cancel_plan_change(change: PlanChangeRequest, *, user) -> PlanChangeRequest:
    if change.status not in (PlanChangeRequest.Status.PENDING, PlanChangeRequest.Status.CONFIRMED):
        raise RequestRejected("not_open", "That change is already closed.", status=409)
    change.status = PlanChangeRequest.Status.CANCELLED
    change.save(update_fields=["status"])
    record_event(change.business, "plan.change_cancelled", "Plan change cancelled.", actor=user)
    return change


def apply_plan_change(change_id: int) -> bool:
    """Switch the package once the confirmed date has arrived. Safe to call twice: a change applies only once."""
    with transaction.atomic():
        change = PlanChangeRequest.objects.select_for_update().select_related("business").get(pk=change_id)
        if change.status != PlanChangeRequest.Status.CONFIRMED or change.confirmed_effective_on > timezone.localdate():
            return False
        subscription = Subscription.objects.select_for_update().get(business=change.business)
        subscription.package = change.requested_package
        subscription.save(update_fields=["package", "updated_at"])
        change.status, change.applied_at = PlanChangeRequest.Status.APPLIED, timezone.now()
        change.save(update_fields=["status", "applied_at"])
    record_event(change.business, "plan.change_applied", f"Package is now {PACKAGE_LABELS[Package(change.requested_package)]}.", metadata={"from": change.from_package, "to": change.requested_package})
    return True


def apply_due_plan_changes() -> int:
    due = PlanChangeRequest.objects.filter(status=PlanChangeRequest.Status.CONFIRMED, confirmed_effective_on__lte=timezone.localdate()).values_list("pk", flat=True)
    return sum(apply_plan_change(pk) for pk in due)
