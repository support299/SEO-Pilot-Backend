from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from apps.businesses.models import Business
from apps.history.services import record_event
from apps.subscriptions.consent import is_opted_in
from apps.subscriptions.entitlements import OPTIONAL_BENEFIT_OF_CAPABILITY
from apps.subscriptions.errors import RequestRejected
from apps.subscriptions.models import FeatureDefinition, Subscription
from apps.subscriptions.packages import CAPABILITY_BY_SEO_ID, Level, Package

from .models import PlanTask, SitePlan

MAX_TASKS_PER_PLAN = 50
OPEN_STATUSES = (SitePlan.Status.DRAFT, SitePlan.Status.SUBMITTED)
FINISHED_TASK_STATUSES = (PlanTask.Status.DONE, PlanTask.Status.CANCELLED)
TASK_TEXT_FIELDS = ("title", "description", "target", "completion_criteria", "scope_justification", "dependencies")
TASK_EDITABLE_IN_DRAFT = (*TASK_TEXT_FIELDS, "priority", "risk", "planned_start", "planned_end", "estimate_minutes", "position")


def _who(agent_name: str) -> str:
    return f" (by {agent_name})" if agent_name else ""


def _event(business: Business, kind: str, summary: str, *, user, agent_name: str, **metadata) -> None:
    record_event(business, kind, summary + _who(agent_name), actor=user, metadata={**metadata, **({"agent": agent_name} if agent_name else {})})


def active_plan(business: Business) -> SitePlan | None:
    return SitePlan.objects.filter(business=business, status=SitePlan.Status.ACTIVE).first()


def open_plan(business: Business) -> SitePlan | None:
    return SitePlan.objects.filter(business=business, status__in=OPEN_STATUSES).first()


def _active_subscription(business: Business) -> Subscription:
    subscription = Subscription.objects.filter(business=business).first()
    if subscription is None or subscription.status != Subscription.Status.ACTIVE:
        raise RequestRejected("no_active_subscription", "This website has no active package.")
    return subscription


def task_eligibility(business: Business, feature: FeatureDefinition) -> dict:
    """
    Whether a checklist item may be worked on under the current package. Checked when a task is added, when a plan is
    submitted and approved, and before a task starts (so a downgrade stops ineligible work).
    """
    subscription = Subscription.objects.filter(business=business).first()
    if subscription is None or subscription.status != Subscription.Status.ACTIVE:
        return {"ok": False, "code": "no_active_subscription", "conditional": False}
    code = feature.code_for(subscription.package)
    capability = CAPABILITY_BY_SEO_ID.get(feature.feature_id)
    if capability is not None:
        if capability.levels[Package(subscription.package)] == Level.OPTIONAL:
            benefit = OPTIONAL_BENEFIT_OF_CAPABILITY.get(capability.key)
            if not (benefit and is_opted_in(business, benefit)):
                return {"ok": False, "code": "needs_consent", "conditional": False}
        if capability.key in (subscription.granted_capabilities or []):
            return {"ok": True, "code": "granted", "conditional": False}
    if code in ("N", "A"):
        return {"ok": False, "code": "outside_package", "conditional": False}
    if code == "X":
        return {"ok": False, "code": "not_applicable", "conditional": False}
    return {"ok": True, "code": "included" if code == "I" else "conditional", "conditional": code == "C"}


ELIGIBILITY_MESSAGES = {
    "no_active_subscription": "This website has no active package.",
    "needs_consent": "This is an optional benefit the customer hasn't turned on.",
    "outside_package": "This item isn't in the package. Send a scope request instead.",
    "not_applicable": "This item doesn't apply to the package.",
}


def _check_task_allowed(business: Business, feature: FeatureDefinition, scope_justification: str) -> None:
    eligibility = task_eligibility(business, feature)
    if not eligibility["ok"]:
        raise RequestRejected(eligibility["code"], f"{feature.feature_id}: {ELIGIBILITY_MESSAGES[eligibility['code']]}")
    if eligibility["conditional"] and not scope_justification.strip():
        raise RequestRejected("justification_required", f"{feature.feature_id} is conditional for this package. Explain why this site qualifies (scope_justification).")


def _require_status(plan: SitePlan, *statuses: str) -> None:
    if plan.status not in statuses:
        raise RequestRejected("not_editable", f"This plan is {plan.get_status_display().lower()} and can't be changed this way.", status=409)


def _validate_dates(start, end) -> None:
    if start and end and end < start:
        raise RequestRejected("bad_dates", "planned_end can't be before planned_start.")


# --- plans ------------------------------------------------------------------------------------------------------

def create_draft(business: Business, *, user=None, agent_name: str = "", summary: str = "", reassess_on=None) -> SitePlan:
    """Start the next version. Unfinished tasks from the active plan are carried over so nothing silently disappears."""
    _active_subscription(business)
    existing = open_plan(business)
    if existing is not None:
        raise RequestRejected("plan_already_open", f"Version {existing.version} is already {existing.get_status_display().lower()}. Edit or withdraw it first.", status=409)
    current = active_plan(business)
    try:
        with transaction.atomic():
            version = (SitePlan.objects.filter(business=business).aggregate(v=Max("version"))["v"] or 0) + 1
            plan = SitePlan.objects.create(
                business=business,
                version=version,
                supersedes=current,
                summary=summary.strip() or (current.summary if current else ""),
                reassess_on=reassess_on,
                created_by=user,
                created_by_agent=agent_name,
            )
            if current is not None:
                for task in current.tasks.exclude(status__in=FINISHED_TASK_STATUSES):
                    PlanTask.objects.create(
                        plan=plan,
                        feature=task.feature,
                        carried_from=task,
                        status=task.status,
                        status_note=task.status_note,
                        **{field: getattr(task, field) for field in TASK_EDITABLE_IN_DRAFT},
                    )
    except IntegrityError:
        raise RequestRejected("plan_already_open", "Another draft was just started for this website.", status=409)
    _event(business, "siteplan.drafted", f"Site plan version {plan.version} started", user=user, agent_name=agent_name, plan=plan.id)
    return plan


def update_plan(plan: SitePlan, data: dict, *, user=None, agent_name: str = "") -> SitePlan:
    _require_status(plan, SitePlan.Status.DRAFT)
    if "summary" in data:
        plan.summary = data["summary"].strip()
    if "reassess_on" in data:
        plan.reassess_on = data["reassess_on"]
    plan.save()
    return plan


def submit(plan: SitePlan, *, user=None, agent_name: str = "") -> SitePlan:
    _require_status(plan, SitePlan.Status.DRAFT)
    tasks = list(plan.tasks.select_related("feature"))
    if not tasks:
        raise RequestRejected("no_tasks", "Add at least one task before submitting.")
    if not plan.summary.strip():
        raise RequestRejected("summary_required", "Add a short summary of the plan's goals before submitting.")
    for task in tasks:
        if task.status not in FINISHED_TASK_STATUSES:
            _check_task_allowed(plan.business, task.feature, task.scope_justification)
    plan.status = SitePlan.Status.SUBMITTED
    plan.submitted_at = timezone.now()
    plan.decision_note = ""
    plan.save()
    _event(plan.business, "siteplan.submitted", f"Site plan version {plan.version} submitted for approval", user=user, agent_name=agent_name, plan=plan.id)
    return plan


def withdraw(plan: SitePlan, *, user=None, agent_name: str = "") -> SitePlan:
    _require_status(plan, *OPEN_STATUSES)
    plan.status = SitePlan.Status.WITHDRAWN
    plan.save()
    _event(plan.business, "siteplan.withdrawn", f"Site plan version {plan.version} withdrawn", user=user, agent_name=agent_name, plan=plan.id)
    return plan


def decide(plan: SitePlan, decision: str, *, user, note: str = "") -> SitePlan:
    """People only. approve: becomes the active plan. request_changes: back to draft with a note. reject: closed."""
    _require_status(plan, SitePlan.Status.SUBMITTED)
    note = note.strip()
    if decision in ("reject", "request_changes") and not note:
        raise RequestRejected("note_required", "Say what needs to change, so the plan can be fixed.")
    with transaction.atomic():
        if decision == "approve":
            _active_subscription(plan.business)
            for task in plan.tasks.select_related("feature"):
                if task.status not in FINISHED_TASK_STATUSES:
                    _check_task_allowed(plan.business, task.feature, task.scope_justification)
            SitePlan.objects.filter(business=plan.business, status=SitePlan.Status.ACTIVE).update(status=SitePlan.Status.SUPERSEDED)
            plan.status = SitePlan.Status.ACTIVE
            plan.effective_from = timezone.localdate()
        elif decision == "request_changes":
            plan.status = SitePlan.Status.DRAFT
        elif decision == "reject":
            plan.status = SitePlan.Status.REJECTED
        else:
            raise RequestRejected("bad_decision", "Decision must be approve, request_changes or reject.")
        plan.decided_by, plan.decided_at, plan.decision_note = user, timezone.now(), note
        plan.save()
    words = {"approve": "approved", "request_changes": "sent back for changes", "reject": "rejected"}
    record_event(plan.business, f"siteplan.{decision}", f"Site plan version {plan.version} {words[decision]}.", actor=user, metadata={"plan": plan.id})
    return plan


# --- tasks ------------------------------------------------------------------------------------------------------

def add_task(plan: SitePlan, data: dict, *, user=None, agent_name: str = "") -> PlanTask:
    _require_status(plan, SitePlan.Status.DRAFT)
    if plan.tasks.count() >= MAX_TASKS_PER_PLAN:
        raise RequestRejected("too_many_tasks", f"A plan can hold at most {MAX_TASKS_PER_PLAN} tasks. Prioritise.")
    feature = FeatureDefinition.objects.filter(feature_id=(data.get("feature_id") or "").strip().upper()).first()
    if feature is None:
        raise RequestRejected("unknown_feature", "feature_id must be a checklist item such as SEO-026.")
    _check_task_allowed(plan.business, feature, data.get("scope_justification") or "")
    _validate_dates(data.get("planned_start"), data.get("planned_end"))
    fields = {key: data[key] for key in TASK_EDITABLE_IN_DRAFT if key in data and data[key] is not None}
    if "position" not in fields:
        fields["position"] = (plan.tasks.aggregate(p=Max("position"))["p"] or 0) + 1
    task = PlanTask.objects.create(plan=plan, feature=feature, updated_by_agent=agent_name, **fields)
    return task


def update_task(task: PlanTask, data: dict, *, user=None, agent_name: str = "") -> PlanTask:
    """
    Draft plan: the task's details can be edited. Active plan: only progress can change (status, status_note,
    completion_evidence). Any other plan state: nothing.
    """
    plan = task.plan
    if plan.status == SitePlan.Status.DRAFT:
        if "status" in data or "completion_evidence" in data:
            raise RequestRejected("not_editable", "Progress can only be recorded once the plan is approved.", status=409)
        changes = {key: data[key] for key in TASK_EDITABLE_IN_DRAFT if key in data}
        merged_justification = changes.get("scope_justification", task.scope_justification) or ""
        _check_task_allowed(plan.business, task.feature, merged_justification)
        _validate_dates(changes.get("planned_start", task.planned_start), changes.get("planned_end", task.planned_end))
        for key, value in changes.items():
            if value is None and key not in ("planned_start", "planned_end", "estimate_minutes"):
                continue  # null for a text field means "leave as is"
            setattr(task, key, value)
        if not task.title.strip() or not task.completion_criteria.strip():
            raise RequestRejected("missing_fields", "A task needs a title and completion_criteria.")
        task.updated_by_agent = agent_name
        task.save()
        return task

    _require_status(plan, SitePlan.Status.ACTIVE)
    detail_fields = set(data) & set(TASK_EDITABLE_IN_DRAFT)
    if detail_fields:
        raise RequestRejected("plan_is_approved", "An approved plan can't be edited. Start a new draft version to change tasks.", status=409)
    new_status = data.get("status", task.status)
    note = (data.get("status_note") if "status_note" in data else task.status_note) or ""
    evidence = (data.get("completion_evidence") if "completion_evidence" in data else task.completion_evidence) or ""
    if new_status in (PlanTask.Status.IN_PROGRESS, PlanTask.Status.DONE) and task.status != new_status:
        eligibility = task_eligibility(plan.business, task.feature)
        if not eligibility["ok"]:
            raise RequestRejected(eligibility["code"], f"{task.feature.feature_id}: {ELIGIBILITY_MESSAGES[eligibility['code']]}")
    if new_status == PlanTask.Status.DONE and not evidence.strip():
        raise RequestRejected("evidence_required", "Link the proof the task was completed (completion_evidence).")
    if new_status in (PlanTask.Status.WAITING, PlanTask.Status.DEFERRED, PlanTask.Status.CANCELLED) and not note.strip():
        raise RequestRejected("note_required", "Say what it is waiting on, or why it was deferred or cancelled (status_note).")
    if new_status == task.status and note == task.status_note and evidence == task.completion_evidence:
        return task  # unchanged: a retried request adds nothing
    previous = task.status
    task.status, task.status_note, task.completion_evidence, task.updated_by_agent = new_status, note.strip(), evidence.strip(), agent_name
    task.save()
    if previous != new_status:
        _event(plan.business, "siteplan.task_updated", f"Task \"{task.title}\": {previous} → {new_status}", user=user, agent_name=agent_name, plan=plan.id, task=task.id, status=new_status)
    return task


def delete_task(task: PlanTask, *, user=None, agent_name: str = "") -> None:
    _require_status(task.plan, SitePlan.Status.DRAFT)
    task.delete()


# --- reading ----------------------------------------------------------------------------------------------------

def _date(value):
    return value.isoformat() if value else None


def task_dict(task: PlanTask, *, internal: bool) -> dict:
    data = {
        "id": task.id,
        "feature_id": task.feature.feature_id,
        "feature_title": task.feature.title,
        "title": task.title,
        "description": task.description,
        "target": task.target,
        "priority": task.priority,
        "completion_criteria": task.completion_criteria,
        "planned_start": _date(task.planned_start),
        "planned_end": _date(task.planned_end),
        "status": task.status,
        "status_note": task.status_note,
    }
    if internal:
        data.update(
            {
                "risk": task.risk,
                "scope_justification": task.scope_justification,
                "dependencies": task.dependencies,
                "estimate_minutes": task.estimate_minutes,
                "completion_evidence": task.completion_evidence,
                "carried_from": task.carried_from_id,
                "updated_by_agent": task.updated_by_agent,
                "position": task.position,
                "eligibility": task_eligibility(task.plan.business, task.feature),
            }
        )
    return data


def plan_dict(plan: SitePlan | None, *, internal: bool) -> dict | None:
    if plan is None:
        return None
    data = {
        "id": plan.id,
        "version": plan.version,
        "status": plan.status,
        "summary": plan.summary,
        "effective_from": _date(plan.effective_from),
        "reassess_on": _date(plan.reassess_on),
        "tasks": [task_dict(t, internal=internal) for t in plan.tasks.select_related("feature")],
    }
    if internal:
        data.update(
            {
                "created_by": plan.created_by.email if plan.created_by else (f"agent: {plan.created_by_agent}" if plan.created_by_agent else None),
                "submitted_at": plan.submitted_at.isoformat() if plan.submitted_at else None,
                "decided_by": plan.decided_by.email if plan.decided_by else None,
                "decided_at": plan.decided_at.isoformat() if plan.decided_at else None,
                "decision_note": plan.decision_note,
                "supersedes": plan.supersedes_id,
                "estimate_minutes_total": sum(t.estimate_minutes or 0 for t in plan.tasks.all()),
            }
        )
    return data


def workspace(business: Business, *, internal: bool) -> dict:
    if not internal:
        return {"active": plan_dict(active_plan(business), internal=False)}
    history = SitePlan.objects.filter(business=business).exclude(status__in=[*OPEN_STATUSES, SitePlan.Status.ACTIVE])
    return {
        "active": plan_dict(active_plan(business), internal=True),
        "open": plan_dict(open_plan(business), internal=True),
        "history": [{"id": p.id, "version": p.version, "status": p.status, "decided_at": p.decided_at.isoformat() if p.decided_at else None, "decision_note": p.decision_note} for p in history[:20]],
    }
