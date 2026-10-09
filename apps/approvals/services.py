from django.utils import timezone

from apps.businesses.models import Business
from apps.history.services import record_event
from apps.site_health.models import CrawlRun

from .models import Approval

SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2}


def sync_approvals(business: Business) -> None:
    """
    Turn the latest completed crawl into one approval per issue type. A decision
    already recorded is never overwritten; a pending item whose issue is gone
    from the latest crawl is marked resolved, not deleted.
    """
    crawl = CrawlRun.objects.filter(business=business, status=CrawlRun.Status.COMPLETED).order_by("-finished_at", "-id").first()
    if crawl is None:
        return

    groups: dict[str, dict] = {}
    for finding in crawl.findings.all():
        group = groups.setdefault(
            finding.title,
            {"category": finding.category, "severity": finding.severity, "detail": finding.detail, "urls": []},
        )
        if finding.url and finding.url not in group["urls"]:
            group["urls"].append(finding.url)
        if SEVERITY_RANK.get(finding.severity, 9) < SEVERITY_RANK.get(group["severity"], 9):
            group["severity"] = finding.severity

    existing = {approval.source_key: approval for approval in Approval.objects.filter(business=business)}

    for key, group in groups.items():
        approval = existing.get(key)
        if approval is None:
            Approval.objects.create(
                business=business,
                source_key=key,
                title=key,
                category=group["category"],
                severity=group["severity"],
                detail=group["detail"],
                affected_urls=group["urls"],
            )
            continue
        fields = {"severity": group["severity"], "detail": group["detail"], "affected_urls": group["urls"]}
        if approval.status == Approval.Status.RESOLVED:
            fields["status"] = Approval.Status.PENDING
        for name, value in fields.items():
            setattr(approval, name, value)
        approval.save(update_fields=[*fields, "updated_at"])

    for key, approval in existing.items():
        if key not in groups and approval.status == Approval.Status.PENDING:
            approval.status = Approval.Status.RESOLVED
            approval.save(update_fields=["status", "updated_at"])


def serialize_approval(approval: Approval) -> dict:
    return {
        "id": approval.id,
        "title": approval.title,
        "category": approval.category,
        "severity": approval.severity,
        "detail": approval.detail,
        "affected_urls": approval.affected_urls,
        "status": approval.status,
        "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
        "created_at": approval.created_at.isoformat(),
    }


def list_approvals(business: Business) -> list[dict]:
    sync_approvals(business)
    approvals = sorted(
        Approval.objects.filter(business=business),
        key=lambda approval: (SEVERITY_RANK.get(approval.severity, 9), approval.title),
    )
    return [serialize_approval(approval) for approval in approvals]


def record_decision(approval: Approval, decision: str, user) -> Approval:
    approval.status = decision
    if decision == Approval.Status.PENDING:
        approval.decided_by = None
        approval.decided_at = None
    else:
        approval.decided_by = user
        approval.decided_at = timezone.now()
    approval.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])
    verb = {Approval.Status.APPROVED: "Approved", Approval.Status.REJECTED: "Rejected", Approval.Status.PENDING: "Reopened"}[decision]
    kind = "approval.reopened" if decision == Approval.Status.PENDING else f"approval.{decision}"
    record_event(approval.business, kind, f"{verb} \"{approval.title}\".", actor=user, metadata={"approval_id": approval.id})
    return approval
