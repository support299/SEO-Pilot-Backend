"""
The three sold packages and what each includes. Source: SEO Pilot developer handoff (2 Nov 2026), page 2.

Only customer-visible facts live here (name, list price, capabilities). Internal work budgets, overhead and
planning rates are deliberately NOT stored in this module so they can never leak into a customer response.
"""

from dataclasses import dataclass
from enum import Enum


class Level(str, Enum):
    INCLUDED = "included"
    CONDITIONAL = "conditional"  # only with explicit scope / evidence / plan inclusion
    OPTIONAL = "optional"  # an opt-in benefit: off until the customer consents
    NOT_INCLUDED = "not_included"  # a finding may be recorded; work needs an approved scope change


class Package(str, Enum):
    STARTER = "starter"
    GROWTH = "growth"
    AUTHORITY = "authority"


PACKAGE_LABELS = {
    Package.STARTER: "Starter Local SEO",
    Package.GROWTH: "Growth SEO + Content",
    Package.AUTHORITY: "Authority SEO Partner",
}

LIST_PRICE_USD = {Package.STARTER: 497, Package.GROWTH: 957, Package.AUTHORITY: 1800}


@dataclass(frozen=True)
class Capability:
    key: str
    label: str
    seo_ids: tuple[str, ...]
    levels: dict[Package, Level]
    notes: dict[Package, str]


S, G, A = Package.STARTER, Package.GROWTH, Package.AUTHORITY
INC, COND, OPT, NO = Level.INCLUDED, Level.CONDITIONAL, Level.OPTIONAL, Level.NOT_INCLUDED

CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        "dashboard_baselines", "Daily dashboard and rank and backlink baselines", ("SEO-090", "SEO-091"),
        {S: INC, G: INC, A: INC}, {},
    ),
    Capability(
        "on_page_optimization", "Existing page optimization", ("SEO-026", "SEO-027", "SEO-028", "SEO-029"),
        {S: INC, G: INC, A: INC},
        {S: "Core optimization; service page work is conditional.", G: "Broader on-site content.", A: "Advanced on-site content."},
    ),
    Capability(
        "new_service_pages", "New service pages", ("SEO-030",),
        {S: COND, G: INC, A: INC},
        {S: "Only if explicitly scoped."},
    ),
    Capability(
        "local_pages", "Useful local pages", ("SEO-031",),
        {S: NO, G: COND, A: COND},
        {S: "City pages are excluded.", G: "Needs legitimate local evidence and inclusion in the site plan.", A: "Needs legitimate local evidence and inclusion in the site plan."},
    ),
    Capability(
        "competitor_review", "Competitor review", ("SEO-006",),
        {S: COND, G: INC, A: INC}, {},
    ),
    Capability(
        "offsite_articles", "Off-site articles and publisher outreach", ("SEO-060",),
        {S: NO, G: NO, A: INC},
        {A: "Quantity is set in the approved site plan."},
    ),
    Capability(
        "unlinked_mention_identification", "Unlinked mention identification", ("SEO-062",),
        {S: NO, G: COND, A: INC}, {},
    ),
    Capability(
        "unlinked_mention_outreach", "Unlinked mention outreach", ("SEO-062",),
        {S: NO, G: NO, A: INC}, {},
    ),
    Capability(
        "reputation_management", "Reputation management", ("SEO-047", "SEO-048", "SEO-089", "SEO-096"),
        {S: OPT, G: OPT, A: OPT},
        {S: "Optional included benefit. Off until you opt in.", G: "Optional included benefit. Off until you opt in.", A: "Optional included benefit. Off until you opt in."},
    ),
    Capability(
        "hosting", "Hosting", ("HOST-001",),
        {S: OPT, G: OPT, A: OPT},
        {S: "Optional included benefit. You can keep your current host.", G: "Optional included benefit. You can keep your current host.", A: "Optional included benefit. You can keep your current host."},
    ),
)

CAPABILITY_BY_KEY = {c.key: c for c in CAPABILITIES}

# SEO-030 appears under both "existing page optimization" and "new service pages" in the handoff. It maps to the
# stricter one (new service pages) so a lookup by id can never grant more than the package allows.
CAPABILITY_BY_SEO_ID: dict[str, Capability] = {}
for _capability in CAPABILITIES:
    for _seo_id in _capability.seo_ids:
        existing = CAPABILITY_BY_SEO_ID.get(_seo_id)
        if existing is None or _capability.key in ("new_service_pages", "unlinked_mention_outreach"):
            CAPABILITY_BY_SEO_ID[_seo_id] = _capability
