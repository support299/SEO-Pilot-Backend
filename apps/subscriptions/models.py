from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.businesses.models import Business

from .packages import LIST_PRICE_USD, PACKAGE_LABELS, Package


class Subscription(models.Model):
    """
    The paid SEO package for ONE website (Business). One paid subscription covers one business: a second site
    under the same client needs its own subscription, it never inherits the first one's.

    Payment happens on the ServicePilot site; staff record the result here when they create the client.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PENDING = "pending", "Pending"  # created, not yet started or confirmed
        CANCELLED = "cancelled", "Cancelled"

    business = models.OneToOneField(Business, on_delete=models.CASCADE, related_name="subscription")
    package = models.CharField(max_length=20, choices=[(p.value, PACKAGE_LABELS[p]) for p in Package])
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    started_on = models.DateField(null=True, blank=True)
    ended_on = models.DateField(null=True, blank=True)
    # Set only for a signed, grandfathered price that differs from the list price.
    contracted_price_usd = models.PositiveIntegerField(null=True, blank=True)
    # Capability keys added beyond the package by an approved scope change or a signed contract. Opt-in benefits can never be granted this way.
    granted_capabilities = models.JSONField(default=list, blank=True)
    # Staff-only. Never serialized for customers.
    external_reference = models.CharField(max_length=200, blank=True)
    internal_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "subscriptions_subscription"

    def __str__(self) -> str:
        return f"{self.business_id}: {self.package} ({self.status})"

    @property
    def package_label(self) -> str:
        return PACKAGE_LABELS[Package(self.package)]

    @property
    def monthly_price_usd(self) -> int:
        return self.contracted_price_usd if self.contracted_price_usd is not None else LIST_PRICE_USD[Package(self.package)]


class ScopeChangeRequest(models.Model):
    """
    Someone wants work the package does not include (or only conditionally). This records the ask for the owner to
    decide. It never starts work or creates a charge by itself.
    """

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        APPROVED = "approved", "Approved"
        DECLINED = "declined", "Declined"

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="scope_requests")
    capability = models.CharField(max_length=60)
    note = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    requested_by_agent = models.CharField(max_length=100, blank=True)  # name of the AI key that asked, when no person did
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "subscriptions_scope_request"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["business", "capability"], condition=models.Q(status="open"), name="one_open_scope_request_per_capability"),
        ]


class PlanChangeRequest(models.Model):
    """
    An upgrade or downgrade asked for in writing. The effective date is NEVER computed: late notice, proration and
    cycle anchors are open owner decisions (OD-03, OD-14). Until staff confirm a date it reads "awaiting confirmation".
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Awaiting confirmation"
        CONFIRMED = "confirmed", "Confirmed"
        APPLIED = "applied", "Applied"
        CANCELLED = "cancelled", "Cancelled"

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="plan_changes")
    from_package = models.CharField(max_length=20)
    requested_package = models.CharField(max_length=20)
    notice_received_at = models.DateTimeField(default=timezone.now)
    requested_effective_on = models.DateField(null=True, blank=True)
    confirmed_effective_on = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    note = models.TextField(blank=True)  # staff-only
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    applied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "subscriptions_plan_change"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["business"], condition=models.Q(status__in=["pending", "confirmed"]), name="one_open_plan_change_per_business"),
        ]


class ConsentRecord(models.Model):
    """
    One recorded decision about an optional benefit. Rows are never edited or deleted: the newest row for a
    (website, benefit) is the current state, older rows are the evidence trail. No row means "off". Nothing is ever
    pre-selected, and declining never blocks SEO work.
    """

    class Benefit(models.TextChoices):
        HOSTING = "hosting", "Hosting"
        REPUTATION = "reputation", "Reputation management"
        HOSTING_AFTER_CANCELLATION = "hosting_after_cancellation", "Hosting only (after cancellation)"

    class Decision(models.TextChoices):
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"
        REVOKED = "revoked", "Revoked"

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="consent_records")
    benefit = models.CharField(max_length=40, choices=Benefit.choices)
    decision = models.CharField(max_length=20, choices=Decision.choices)
    consented_by_name = models.CharField(max_length=200, blank=True)  # the customer-side person who agreed
    evidence = models.TextField(blank=True)  # where/how the consent was given (email, date, message)
    terms_accepted = models.BooleanField(default=False)
    terms_reference = models.CharField(max_length=300, blank=True)
    workflow_authority_confirmed = models.BooleanField(default=False)
    recurring_charge_acknowledged = models.BooleanField(default=False)
    note = models.TextField(blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "subscriptions_consent_record"
        ordering = ["-recorded_at", "-id"]


class FeatureDefinition(models.Model):
    """One row of the SEO-001..SEO-100 registry. Loaded by `import_checklist`; never edited by hand."""

    feature_id = models.CharField(max_length=10, unique=True)
    checklist_version = models.CharField(max_length=40)
    category = models.CharField(max_length=60)
    title = models.CharField(max_length=300)
    starter_code = models.CharField(max_length=1)
    growth_code = models.CharField(max_length=1)
    authority_code = models.CharField(max_length=1)
    delivery_mode = models.CharField(max_length=20, blank=True)  # Setup / Recurring / Conditional / N/A
    prerequisites = models.TextField(blank=True)
    policy_notes = models.TextField(blank=True)

    class Meta:
        db_table = "subscriptions_feature_definition"
        ordering = ["feature_id"]

    def code_for(self, package: str) -> str:
        return {"starter": self.starter_code, "growth": self.growth_code, "authority": self.authority_code}[package]


class ChecklistItem(models.Model):
    """What we know about one feature on one website. A row exists only once someone has recorded something."""

    class Status(models.TextChoices):
        NOT_CHECKED = "not_checked", "Not checked"
        VERIFIED = "verified", "Checked"
        NEEDS_WORK = "needs_work", "Needs work"
        IN_PROGRESS = "in_progress", "In progress"
        BLOCKED = "blocked", "Blocked"
        DONE = "done", "Done"
        N_A = "n_a", "Not applicable"
        OUT_OF_PACKAGE = "out_of_package", "Outside the package"

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="checklist_items")
    feature = models.ForeignKey(FeatureDefinition, on_delete=models.PROTECT, related_name="items")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NOT_CHECKED)
    finding = models.TextField(blank=True)  # the observation, in plain language (customer-visible)
    evidence = models.TextField(blank=True)  # where the proof is (staff-only)
    owner = models.CharField(max_length=200, blank=True)
    next_action = models.TextField(blank=True)
    blocker = models.TextField(blank=True)
    # "crawl" when the latest write came from the automatic site crawl. Once a person or an agent records the item,
    # this is cleared and later crawls leave the item alone.
    source = models.CharField(max_length=20, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "subscriptions_checklist_item"
        constraints = [models.UniqueConstraint(fields=["business", "feature"], name="one_checklist_item_per_feature_per_site")]


class ChecklistStatusChange(models.Model):
    """Append-only: every status change with who, when, from, to, why and proof. A reopened item keeps its earlier 'done'."""

    item = models.ForeignKey(ChecklistItem, on_delete=models.CASCADE, related_name="changes")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    agent_name = models.CharField(max_length=100, blank=True)  # set when an AI key made the change
    source = models.CharField(max_length=20, blank=True)  # "crawl" for automatic updates from a site crawl
    from_status = models.CharField(max_length=20)
    to_status = models.CharField(max_length=20)
    reason = models.TextField(blank=True)
    evidence = models.TextField(blank=True)
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "subscriptions_checklist_change"
        ordering = ["-occurred_at", "-id"]
