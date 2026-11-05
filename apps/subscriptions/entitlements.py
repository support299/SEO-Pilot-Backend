"""
One place that answers "may this website have this capability?". Every feature that does package-gated work
should call `entitlement()` / `can_execute()` instead of checking the package name itself.
"""

from dataclasses import dataclass

from apps.businesses.models import Business

from .consent import is_opted_in
from .models import Subscription
from .packages import CAPABILITIES, CAPABILITY_BY_KEY, CAPABILITY_BY_SEO_ID, Capability, Level, Package


OPTIONAL_BENEFIT_OF_CAPABILITY = {"hosting": "hosting", "reputation_management": "reputation"}


@dataclass(frozen=True)
class Entitlement:
    capability: str
    level: Level | None  # None: the business has no usable subscription
    reason: str  # short machine-readable reason: "ok", "no_subscription", "subscription_pending", ...

    @property
    def can_execute(self) -> bool:
        """Work may start only for an included capability. Conditional/optional need extra approval or consent first."""
        return self.level == Level.INCLUDED


def _subscription_of(business: Business) -> Subscription | None:
    try:
        return business.subscription
    except Subscription.DoesNotExist:
        return None


def resolve_capability(feature: str) -> Capability:
    """Accepts a capability key ("local_pages") or an SEO id ("SEO-031"). Unknown names raise, so typos fail loudly."""
    capability = CAPABILITY_BY_KEY.get(feature) or CAPABILITY_BY_SEO_ID.get(feature)
    if capability is None:
        raise ValueError(f"Unknown capability: {feature}")
    return capability


def effective_level(subscription: Subscription, capability: Capability) -> Level:
    """The package's level, lifted to included by an approved scope grant. Opt-in benefits stay opt-in: they need consent, not a grant."""
    level = capability.levels[Package(subscription.package)]
    if level == Level.OPTIONAL:
        benefit = OPTIONAL_BENEFIT_OF_CAPABILITY.get(capability.key)
        return Level.INCLUDED if benefit and is_opted_in(subscription.business, benefit) else Level.OPTIONAL
    if level != Level.OPTIONAL and capability.key in (subscription.granted_capabilities or []):
        return Level.INCLUDED
    return level


def entitlement(business: Business, feature: str) -> Entitlement:
    capability = resolve_capability(feature)
    subscription = _subscription_of(business)
    if subscription is None:
        return Entitlement(capability.key, None, "no_subscription")
    if subscription.status != Subscription.Status.ACTIVE:
        return Entitlement(capability.key, None, f"subscription_{subscription.status}")
    return Entitlement(capability.key, effective_level(subscription, capability), "ok")


def capability_rows(subscription: Subscription) -> list[dict]:
    package = Package(subscription.package)
    return [
        {
            "key": c.key,
            "label": c.label,
            "seo_ids": list(c.seo_ids),
            "level": (Level.OPTIONAL if c.levels[package] == Level.OPTIONAL else effective_level(subscription, c)).value,
            "opted_in": c.levels[package] == Level.OPTIONAL and effective_level(subscription, c) == Level.INCLUDED,
            "note": "Added by agreement." if c.key in (subscription.granted_capabilities or []) and effective_level(subscription, c) == Level.INCLUDED else c.notes.get(package, ""),
        }
        for c in CAPABILITIES
    ]
