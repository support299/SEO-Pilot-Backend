from django.db import transaction

from apps.businesses.models import Business
from apps.history.services import record_event

from .models import ConsentRecord, Subscription
from .errors import RequestRejected

BENEFIT_COPY = {
    ConsentRecord.Benefit.HOSTING: "Free managed hosting while your paid SEO is active. You can keep your current host instead.",
    ConsentRecord.Benefit.REPUTATION: "Help asking your happy customers for reviews. Off until you agree to the terms.",
    ConsentRecord.Benefit.HOSTING_AFTER_CANCELLATION: "Hosting only, $12 per month, if you stop SEO. Never starts without your separate yes.",
}


def latest_consent(business: Business, benefit: str) -> ConsentRecord | None:
    return ConsentRecord.objects.filter(business=business, benefit=benefit).first()


def consent_state(business: Business, benefit: str) -> str:
    """off (never decided) | on | declined | revoked."""
    record = latest_consent(business, benefit)
    if record is None:
        return "off"
    return {"accepted": "on", "declined": "declined", "revoked": "revoked"}[record.decision]


def is_opted_in(business: Business, benefit: str) -> bool:
    return consent_state(business, benefit) == "on"


def record_consent(business: Business, benefit: str, decision: str, *, user, data: dict) -> ConsentRecord:
    if benefit not in ConsentRecord.Benefit.values:
        raise RequestRejected("unknown_benefit", "That isn't an optional benefit.")
    state = consent_state(business, benefit)
    subscription = Subscription.objects.filter(business=business).first()

    if decision == ConsentRecord.Decision.ACCEPTED:
        if state == "on":
            raise RequestRejected("already_on", "That benefit is already on.", status=409)
        if subscription is None:
            raise RequestRejected("no_subscription", "This website has no package.")
        if benefit == ConsentRecord.Benefit.HOSTING_AFTER_CANCELLATION:
            if subscription.status != Subscription.Status.CANCELLED:
                raise RequestRejected("not_cancelled", "This offer only applies after SEO is cancelled.")
            if not data.get("recurring_charge_acknowledged"):
                raise RequestRejected("charge_not_acknowledged", "The customer must explicitly acknowledge the $12 monthly charge.")
        elif subscription.status != Subscription.Status.ACTIVE:
            raise RequestRejected("subscription_not_active", "This benefit needs an active package.")
        if not (data.get("consented_by_name") or "").strip():
            raise RequestRejected("missing_consenting_person", "Record who agreed.")
        if not (data.get("evidence") or "").strip():
            raise RequestRejected("missing_evidence", "Record where and when they agreed (for example the email and its date).")
        if benefit == ConsentRecord.Benefit.REPUTATION:
            if not data.get("terms_accepted") or not (data.get("terms_reference") or "").strip():
                raise RequestRejected("terms_not_accepted", "The customer must accept the usage terms. Record which terms.")
            if not data.get("workflow_authority_confirmed"):
                raise RequestRejected("authority_not_confirmed", "Confirm that this person has authority to approve the review workflow.")
    elif decision == ConsentRecord.Decision.REVOKED:
        if state != "on":
            raise RequestRejected("not_on", "Only a benefit that is on can be revoked.", status=409)
    elif decision != ConsentRecord.Decision.DECLINED:
        raise RequestRejected("bad_decision", "Decision must be accepted, declined or revoked.")

    with transaction.atomic():
        record = ConsentRecord.objects.create(
            business=business,
            benefit=benefit,
            decision=decision,
            consented_by_name=(data.get("consented_by_name") or "").strip(),
            evidence=(data.get("evidence") or "").strip(),
            terms_accepted=bool(data.get("terms_accepted")),
            terms_reference=(data.get("terms_reference") or "").strip(),
            workflow_authority_confirmed=bool(data.get("workflow_authority_confirmed")),
            recurring_charge_acknowledged=bool(data.get("recurring_charge_acknowledged")),
            note=(data.get("note") or "").strip(),
            recorded_by=user,
        )
    label = ConsentRecord.Benefit(benefit).label
    record_event(business, f"benefit.{decision}", f"{label}: {decision}.", actor=user, metadata={"benefit": benefit})
    return record
