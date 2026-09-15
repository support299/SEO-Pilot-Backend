from rest_framework.permissions import BasePermission

from apps.businesses.models import Membership


class IsAccountMember(BasePermission):
    """
    Object-level permission: the requesting user must have a Membership in the
    Account that owns the object. Works for any model with either an `account`
    field directly, or a `business.account` relation (one hop).

    This is the single place tenant isolation is enforced for object-level
    checks — individual views should not hand-roll `.filter(owner=...)` logic;
    combine this with a queryset scoped via `accounts_for_user()` below so a
    user can never even list another account's rows, not just be blocked from
    fetching them by id.
    """

    message = "You don't have access to this account's data."

    def has_object_permission(self, request, view, obj):
        account = getattr(obj, "account", None) or getattr(getattr(obj, "business", None), "account", None)
        if account is None:
            return False
        return Membership.objects.filter(account=account, user=request.user).exists()


def accounts_for_user(user):
    """Queryset of Account ids the user belongs to — use to scope list querysets."""
    return Membership.objects.filter(user=user).values_list("account_id", flat=True)


def get_business_or_404(user, business_id):
    """
    Fetch a Business the user actually has access to, or raise Http404 — used
    by any view (e.g. search_console's) that isn't a ModelViewSet over
    Business itself but still needs to scope a request to one. 404 (not 403)
    for the same reason as IsAccountMember: don't confirm a foreign business
    id exists.
    """
    from django.shortcuts import get_object_or_404

    from apps.businesses.models import Business

    return get_object_or_404(Business, id=business_id, account_id__in=accounts_for_user(user))
