"""
The per-website SEO checklist: the SEO-001..SEO-100 registry plus what we know about each item on each site.
Definition, package eligibility, applicability, execution status and outcome measurement are kept separate.
"""

import csv
import json
from collections import Counter
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from apps.businesses.models import Business
from apps.history.services import record_event

from .errors import RequestRejected
from .models import ChecklistItem, ChecklistStatusChange, FeatureDefinition, Subscription

CODES = ("I", "C", "A", "N", "X")
CODE_WORDS = {"I": "Included", "C": "Conditional", "A": "Add-on", "N": "Not included", "X": "Not applicable"}

# Tier-code totals of the accepted registry (docs/checklist/*.csv plus REGISTRY_OVERRIDES). A guard against a file
# that was edited or truncated, not a promise of tasks.
#
# The handoff PDF (p.9, "Import QA") quotes different totals: Starter I35 C33 A6 N21 X5, Growth I53 C28 A6 N9 X4,
# Authority I62 C27 A6 N1 X4. The client's item-level CSV does not match them (for example Starter has 43 included items,
# not 35), and the PDF calls its own totals "internal validation only". The CSV is the only item-by-item source, so it
# is used as the base and these totals describe it.
EXPECTED_TOTALS = {
    "starter": {"I": 42, "C": 26, "A": 6, "N": 21, "X": 5},
    "growth": {"I": 52, "C": 27, "A": 6, "N": 10, "X": 5},
    "authority": {"I": 60, "C": 28, "A": 6, "N": 1, "X": 5},
}

# Where the CSV and the handoff PDF disagree about a customer promise, the more cautious reading wins. Each entry only
# applies if the file still carries the original value, so a corrected file is never silently overridden.
#   SEO-006 Competitor research: CSV says included for Starter; the PDF package table (p.2) says conditional.
REGISTRY_OVERRIDES = {"SEO-006": {"starter": ("I", "C")}}

CATEGORY_RANGES = (
    (1, 10, "Goals and setup"),
    (11, 25, "Technical checks"),
    (26, 40, "On-page and content"),
    (41, 55, "Google Business Profile and listings"),
    (56, 70, "Backlinks and authority"),
    (71, 85, "Conversions and usability"),
    (86, 100, "AI, tracking and reporting"),
)


def category_of(feature_id: str) -> str:
    number = int(feature_id.split("-")[1])
    return next(label for low, high, label in CATEGORY_RANGES if low <= number <= high)


class ImportInvalid(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


HEADER_ALIASES = {
    "id": "id",
    "title": "title",
    "starter": "starter",
    "growth": "growth",
    "authority": "authority",
    "delivery mode": "delivery_mode",
    "delivery_mode": "delivery_mode",
    "prerequisites / limits": "prerequisites",
    "prerequisites": "prerequisites",
    "policy notes": "policy_notes",
    "policy_notes": "policy_notes",
    "category": "category",
}


def normalise_rows(rows: list[dict]) -> list[dict]:
    """Accept the client's export headers ("ID", "Delivery mode", "Prerequisites / limits") as well as our own."""
    return [{HEADER_ALIASES.get((key or "").strip().lower(), (key or "").strip().lower()): value for key, value in row.items()} for row in rows]


def read_rows(path: Path) -> list[dict]:
    if path.suffix.lower() == ".json":
        return normalise_rows(json.loads(path.read_text(encoding="utf-8")))
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return normalise_rows(list(csv.DictReader(handle)))


def validate_rows(rows: list[dict], expected_totals: dict | None = EXPECTED_TOTALS) -> list[str]:
    problems: list[str] = []
    ids = [(row.get("id") or "").strip().upper() for row in rows]
    expected_ids = [f"SEO-{n:03d}" for n in range(1, 101)]
    if len(rows) != 100:
        problems.append(f"Expected exactly 100 rows, found {len(rows)}.")
    duplicates = sorted(i for i, n in Counter(ids).items() if n > 1)
    if duplicates:
        problems.append(f"Duplicate IDs: {', '.join(duplicates)}.")
    missing = [i for i in expected_ids if i not in ids]
    unexpected = sorted(set(ids) - set(expected_ids))
    if missing:
        problems.append(f"Missing IDs: {', '.join(missing[:10])}{' …' if len(missing) > 10 else ''}.")
    if unexpected:
        problems.append(f"Unexpected IDs (HOST-001 and MSG-001 belong outside this list): {', '.join(unexpected)}.")
    for row in rows:
        row_id = (row.get("id") or "?").strip()
        if not (row.get("title") or "").strip():
            problems.append(f"{row_id}: title is empty.")
        for tier in ("starter", "growth", "authority"):
            if (row.get(tier) or "").strip().upper() not in CODES:
                problems.append(f"{row_id}: {tier} code must be one of {', '.join(CODES)}.")
    if expected_totals and not problems:
        for tier, expected in expected_totals.items():
            counts = Counter((row[tier]).strip().upper() for row in rows)
            actual = {code: counts.get(code, 0) for code in CODES}
            if actual != expected:
                problems.append(f"{tier.title()} totals are {actual}, expected {expected}.")
    return problems


def apply_overrides(rows: list[dict], overrides: dict = REGISTRY_OVERRIDES) -> list[dict]:
    result = []
    for row in rows:
        row = dict(row)
        for tier, (original, replacement) in overrides.get((row.get("id") or "").strip().upper(), {}).items():
            if (row.get(tier) or "").strip().upper() == original:
                row[tier] = replacement
        result.append(row)
    return result


@transaction.atomic
def import_registry(rows: list[dict], version: str, expected_totals: dict | None = EXPECTED_TOTALS) -> int:
    rows = apply_overrides(rows)
    problems = validate_rows(rows, expected_totals)
    if problems:
        raise ImportInvalid(problems)
    for row in rows:
        feature_id = row["id"].strip().upper()
        FeatureDefinition.objects.update_or_create(
            feature_id=feature_id,
            defaults={
                "checklist_version": version,
                "category": (row.get("category") or "").strip() or category_of(feature_id),
                "title": row["title"].strip(),
                "delivery_mode": (row.get("delivery_mode") or "").strip(),
                "starter_code": row["starter"].strip().upper(),
                "growth_code": row["growth"].strip().upper(),
                "authority_code": row["authority"].strip().upper(),
                "prerequisites": (row.get("prerequisites") or "").strip(),
                "policy_notes": (row.get("policy_notes") or "").strip(),
            },
        )
    return len(rows)


# --- reading ----------------------------------------------------------------------------------------------------

def _subscription(business: Business) -> Subscription | None:
    return Subscription.objects.filter(business=business).first()


def checklist_rows(business: Business, *, staff: bool) -> dict:
    subscription = _subscription(business)
    package = subscription.package if subscription else None
    items = {item.feature_id: item for item in ChecklistItem.objects.filter(business=business)}
    rows = []
    for feature in FeatureDefinition.objects.all():
        item = items.get(feature.id)
        code = feature.code_for(package) if package else None
        row = {
            "feature_id": feature.feature_id,
            "version": feature.checklist_version,
            "category": feature.category,
            "title": feature.title,
            "delivery_mode": feature.delivery_mode,
            "eligibility": code,
            "eligibility_label": CODE_WORDS[code] if code else "No package",
            "status": item.status if item else ChecklistItem.Status.NOT_CHECKED,
            "finding": item.finding if item else "",
            "owner": item.owner if item else "",
            "next_action": item.next_action if item else "",
            "blocker": item.blocker if item else "",
            "last_checked_at": item.last_checked_at.isoformat() if item and item.last_checked_at else None,
            "source": item.source if item else "",
        }
        if staff:
            row["evidence"] = item.evidence if item else ""
            row["prerequisites"] = feature.prerequisites
            row["policy_notes"] = feature.policy_notes
        rows.append(row)
    counts = Counter(row["status"] for row in rows)
    return {
        "loaded": bool(rows),
        "package": package,
        "summary": {status.value: counts.get(status.value, 0) for status in ChecklistItem.Status},
        "total": len(rows),
        "rows": rows,
    }


# --- writing ----------------------------------------------------------------------------------------------------

S = ChecklistItem.Status
NEEDS_FINDING = {S.VERIFIED, S.NEEDS_WORK, S.DONE}


def update_item(business: Business, feature_id: str, data: dict, *, user, agent_name: str = "") -> ChecklistItem:
    """`user` is the person acting; for an AI key pass user=None and its name as `agent_name` so the trail says who did it."""
    feature = FeatureDefinition.objects.filter(feature_id=feature_id).first()
    if feature is None:
        raise RequestRejected("unknown_feature", "That checklist item doesn't exist.", status=404)
    subscription = _subscription(business)
    if subscription is None:
        raise RequestRejected("no_subscription", "This website has no package, so its checklist can't be worked yet.")
    status = data["status"]
    code = feature.code_for(subscription.package)

    # Package eligibility: a finding can be recorded outside the package, but work on it needs an approved scope change.
    if code in ("N", "A") and status not in (S.NOT_CHECKED, S.OUT_OF_PACKAGE):
        raise RequestRejected("outside_package", "This item isn't in the package. Record it as outside the package and request a scope change.")
    if code == "X" and status not in (S.NOT_CHECKED, S.N_A):
        raise RequestRejected("not_applicable_to_package", "This item doesn't apply to the package.")

    finding = (data.get("finding") if "finding" in data else None)
    existing = ChecklistItem.objects.filter(business=business, feature=feature).first()
    merged = {
        "finding": finding if finding is not None else (existing.finding if existing else ""),
        "evidence": data.get("evidence", existing.evidence if existing else ""),
        "owner": data.get("owner", existing.owner if existing else ""),
        "next_action": data.get("next_action", existing.next_action if existing else ""),
        "blocker": data.get("blocker", existing.blocker if existing else ""),
    }
    reason = (data.get("reason") or "").strip()

    if status in NEEDS_FINDING and not merged["finding"].strip():
        raise RequestRejected("finding_required", "Describe what was found. A status on its own isn't a result.")
    if status == S.DONE and not merged["evidence"].strip():
        raise RequestRejected("evidence_required", "Link the proof the work was done.")
    if status == S.BLOCKED and not all(merged[k].strip() for k in ("blocker", "owner", "next_action")):
        raise RequestRejected("blocker_details_required", "A blocked item needs the blocker, who owns it, and the next action.")
    if status == S.N_A and not reason:
        raise RequestRejected("rationale_required", "Say why this doesn't apply.")
    if status != S.BLOCKED:
        merged["blocker"] = ""

    if existing and existing.status == status and not existing.source and not reason and all(getattr(existing, key) == value for key, value in merged.items()):
        return existing  # nothing changed: a retried request must not add history or count against a daily cap

    with transaction.atomic():
        item, _ = ChecklistItem.objects.select_for_update().get_or_create(business=business, feature=feature)
        previous = item.status
        for key, value in merged.items():
            setattr(item, key, value)
        item.status = status
        item.source = ""  # a person or agent owns this item now; later crawls won't overwrite it
        if status != S.NOT_CHECKED:
            item.last_checked_at = timezone.now()
        item.save()
        if previous != status or reason:
            ChecklistStatusChange.objects.create(item=item, actor=user, agent_name=agent_name, from_status=previous, to_status=status, reason=reason, evidence=merged["evidence"])
    by = f" (by {agent_name})" if agent_name else ""
    metadata = {"feature_id": feature.feature_id, "status": status, **({"agent": agent_name} if agent_name else {})}
    record_event(business, "checklist.updated", f"{feature.feature_id} {feature.title}: {previous} → {status}{by}.", actor=user, metadata=metadata)
    return item


def item_history(business: Business, feature_id: str) -> list[dict]:
    changes = ChecklistStatusChange.objects.filter(item__business=business, item__feature__feature_id=feature_id).select_related("actor")
    return [
        {
            "from_status": c.from_status,
            "to_status": c.to_status,
            "reason": c.reason,
            "evidence": c.evidence,
            "actor": c.actor.email if c.actor else (f"agent: {c.agent_name}" if c.agent_name else ("site crawl" if c.source == "crawl" else None)),
            "occurred_at": c.occurred_at.isoformat(),
        }
        for c in changes
    ]


# --- automatic results from a site crawl --------------------------------------------------------------------------

# Checklist item -> the crawl finding titles that mean "this needs work". Only what the crawler actually measures.
CRAWL_COVERAGE = {
    "SEO-026": ("Title tags", ("Missing title", "Title too long", "Title too short", "Duplicate title")),
    "SEO-027": ("meta descriptions", ("Missing meta description", "Meta description too long", "Meta description too short", "Duplicate meta description")),
    "SEO-028": ("H1 headings", ("Missing H1 heading", "Multiple H1 headings")),
    "SEO-016": ("canonical tags", ("Missing canonical tag",)),
    "SEO-018": ("broken pages and links", ("Page did not load", "Broken internal link")),
    "SEO-034": ("image alt text", ("Images missing alt text",)),
}
MAX_URLS_IN_FINDING = 10


def apply_crawl_results(crawl) -> int:
    """
    After a completed crawl, fill in the checklist items the crawl can measure: "needs work" with the affected pages,
    or "checked" when nothing was found. Never "done" (that needs proof of a fix). Never touches an item a person or
    an agent has recorded. Returns how many items changed.
    """
    business = crawl.business
    subscription = Subscription.objects.filter(business=business).first()
    if subscription is None or subscription.status != Subscription.Status.ACTIVE:
        return 0
    if crawl.pages_crawled == 0:
        return 0
    findings = list(crawl.findings.values("url", "title"))
    when = (crawl.finished_at or timezone.now()).date().isoformat()
    changed = 0

    for feature in FeatureDefinition.objects.filter(feature_id__in=CRAWL_COVERAGE):
        if feature.code_for(subscription.package) in ("N", "A", "X"):
            continue
        label, titles = CRAWL_COVERAGE[feature.feature_id]
        hits = [f for f in findings if f["title"] in titles]
        if hits:
            urls = sorted({f["url"] for f in hits if f["url"]})
            counts = Counter(f["title"] for f in hits)
            breakdown = ", ".join(f"{title.lower()} ({n})" for title, n in counts.most_common())
            shown = "; ".join(urls[:MAX_URLS_IN_FINDING]) + (f"; and {len(urls) - MAX_URLS_IN_FINDING} more" if len(urls) > MAX_URLS_IN_FINDING else "")
            status = S.NEEDS_WORK
            finding = f"Site crawl on {when}: {len(urls)} of {crawl.pages_crawled} crawled pages have problems with {label}: {breakdown}. Pages: {shown}"
            next_action = "Fix the listed pages, then re-crawl to confirm."
        else:
            status = S.VERIFIED
            finding = f"Site crawl on {when}: no problems with {label} found on {crawl.pages_crawled} crawled pages."
            next_action = ""

        with transaction.atomic():
            item = ChecklistItem.objects.select_for_update().filter(business=business, feature=feature).first()
            if item is not None and (item.source != "crawl" and item.status != S.NOT_CHECKED):
                continue  # recorded by a person or an agent: theirs to judge
            if item is not None and item.status == status and item.finding == finding:
                continue
            item = item or ChecklistItem(business=business, feature=feature)
            previous = item.status if item.pk else S.NOT_CHECKED
            item.status, item.finding, item.next_action, item.blocker = status, finding, next_action, ""
            item.evidence = f"Site crawl #{crawl.id}"
            item.owner = ""
            item.source = "crawl"
            item.last_checked_at = crawl.finished_at or timezone.now()
            item.save()
            if previous != status:
                ChecklistStatusChange.objects.create(item=item, from_status=previous, to_status=status, source="crawl", reason=f"Automatic result of site crawl #{crawl.id}", evidence=item.evidence)
            changed += 1

    if changed:
        record_event(business, "checklist.prefilled", f"Site crawl updated {changed} checklist item(s) automatically.", metadata={"crawl": crawl.id, "items": changed})
    return changed
