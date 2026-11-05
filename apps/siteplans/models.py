from django.conf import settings
from django.db import models

from apps.businesses.models import Business
from apps.subscriptions.models import FeatureDefinition


class SitePlan(models.Model):
    """
    The agreed work for one website: which checklist items, in what order, with what completion criteria.

    Usually drafted by an AI agent through the agent API and approved by a person. An active plan is never edited in
    place: changing it means a new draft version (which carries over unfinished tasks) that needs its own approval.
    Earlier versions stay on record.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Awaiting approval"
        ACTIVE = "active", "Active"
        SUPERSEDED = "superseded", "Replaced by a newer version"
        REJECTED = "rejected", "Rejected"
        WITHDRAWN = "withdrawn", "Withdrawn"

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="site_plans")
    version = models.PositiveIntegerField()
    supersedes = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    summary = models.TextField(blank=True)  # goals and approach, in plain language (customer-visible once active)
    reassess_on = models.DateField(null=True, blank=True)
    effective_from = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_by_agent = models.CharField(max_length=100, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)  # why it was rejected or sent back (staff and agents see it)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "siteplans_plan"
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(fields=["business", "version"], name="unique_plan_version_per_site"),
            models.UniqueConstraint(fields=["business"], condition=models.Q(status="active"), name="one_active_plan_per_site"),
            models.UniqueConstraint(fields=["business"], condition=models.Q(status__in=["draft", "submitted"]), name="one_open_plan_per_site"),
        ]


class PlanTask(models.Model):
    class Status(models.TextChoices):
        PLANNED = "planned", "Planned"
        IN_PROGRESS = "in_progress", "In progress"
        WAITING = "waiting", "Waiting"
        DONE = "done", "Done"
        DEFERRED = "deferred", "Deferred"
        CANCELLED = "cancelled", "Cancelled"

    class Level(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"

    plan = models.ForeignKey(SitePlan, on_delete=models.CASCADE, related_name="tasks")
    feature = models.ForeignKey(FeatureDefinition, on_delete=models.PROTECT, related_name="plan_tasks")
    carried_from = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    target = models.CharField(max_length=500, blank=True)  # URL, page or profile the task changes
    priority = models.CharField(max_length=10, choices=Level.choices, default=Level.MEDIUM)
    risk = models.CharField(max_length=10, choices=Level.choices, default=Level.LOW)
    completion_criteria = models.TextField()
    scope_justification = models.TextField(blank=True)  # required for conditional items: why this site qualifies
    dependencies = models.TextField(blank=True)
    planned_start = models.DateField(null=True, blank=True)
    planned_end = models.DateField(null=True, blank=True)
    estimate_minutes = models.PositiveIntegerField(null=True, blank=True)  # internal only: never shown to customers
    position = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PLANNED)
    status_note = models.TextField(blank=True)  # what it is waiting on, or why it was deferred / cancelled
    completion_evidence = models.TextField(blank=True)  # internal: proof for "done"
    updated_by_agent = models.CharField(max_length=100, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "siteplans_task"
        ordering = ["position", "id"]
