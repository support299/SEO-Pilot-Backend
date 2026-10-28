from rest_framework.permissions import SAFE_METHODS, BasePermission

from apps.businesses.models import Account, Business, Membership


def is_agency_admin(user) -> bool:
    return bool(user and user.is_authenticated and getattr(user, "is_agency_admin", False))


def is_viewer_of(user, account_id) -> bool:
    """True only for a customer login (viewer role) on this account. Non-members are not viewers: they fall through to the views' 404/400."""
    return Membership.objects.filter(user=user, account_id=account_id, role=Membership.Role.VIEWER).exists()


class IsAgencyAdmin(BasePermission):
    message = "Agency admin access required."

    def has_permission(self, request, view):
        return is_agency_admin(request.user)


class DenyViewerWrites(BasePermission):
    """
    Customers get a read-only "viewer" membership. This is the single place that
    turns that into "no writes": any non-safe request is refused when the target
    business's account only gives the user a viewer role. Reads, and the Google
    sign-in grant (a GET authorize-url + callback), stay open to viewers.

    Requests that name no business at all are refused for users who only hold
    viewer memberships.
    """

    message = "You have view-only access to this account."

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        user = request.user
        if not (user and user.is_authenticated) or is_agency_admin(user):
            return True  # authentication itself is enforced by the other permission classes

        business_id = view.kwargs.get("business_id")
        if business_id is None and getattr(view, "pk_is_business", False):
            business_id = view.kwargs.get("pk")
        if business_id is not None:
            account_id = Business.objects.filter(id=business_id).values_list("account_id", flat=True).first()
            return account_id is None or not is_viewer_of(user, account_id)  # unknown/foreign id: the view answers 404

        if getattr(view, "pk_is_business", False):  # creating a business: the target account is in the body
            try:
                return not is_viewer_of(user, int(request.data.get("account")))
            except (TypeError, ValueError):
                return True  # the serializer rejects a missing/invalid account

        return Membership.objects.filter(user=user).exclude(role=Membership.Role.VIEWER).exists()


class IsAccountMember(BasePermission):
    """
    Object-level permission: the requesting user must have a Membership in the
    Account that owns the object (agency admins pass for every account). Works
    for any model with either an `account` field directly, or a
    `business.account` relation (one hop).

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
        if is_agency_admin(request.user):
            return True
        return Membership.objects.filter(account=account, user=request.user).exists()


def accounts_for_user(user):
    """Queryset of Account ids the user can reach — use to scope list querysets. Agency admins reach all."""
    if is_agency_admin(user):
        return Account.objects.values_list("id", flat=True)
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

    return get_object_or_404(Business, id=business_id, account_id__in=accounts_for_user(user))
