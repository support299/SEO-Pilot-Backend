"""
Agent endpoints for the package, checklist and scope — added next to the existing agent API and using the same
`Authorization: Agent <key>` key, so Claude and Codex both use them with no new credentials.

What an agent can do here: read what the package includes, read and update checklist items (with findings and proof),
and ask for work outside the package. What it can never do: grant scope, decide a request, record customer consent, or
change a subscription — those routes aren't exposed to agent keys at all.
"""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from apps.history.models import Event
from apps.subscriptions import checklist as checklist_service
from apps.subscriptions.errors import RequestRejected
from apps.subscriptions.models import Subscription
from apps.subscriptions.serializers import ScopeRequestCreateSerializer, scope_request_dict, staff_plan
from apps.subscriptions.services import request_scope_change
from apps.subscriptions.views_extras import ChecklistUpdateSerializer, benefits_payload
from common.permissions import get_business_or_404

from .views import AgentView


def _rejected(error: RequestRejected) -> Response:
    return Response({"detail": error.message, "code": error.code}, status=error.status)


class AgentPlanView(AgentView):
    """GET — the website's package and what each capability allows (included / conditional / optional / not included)."""

    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        plan = staff_plan(Subscription.objects.filter(business=business).first())
        plan["staff"] = None  # payment references and internal notes are for people, not agents
        return Response(plan)


class AgentBenefitsView(AgentView):
    """GET — whether hosting and reputation are on. An agent can read this but never change it."""

    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        rows = benefits_payload(business, staff=False)
        return Response(rows)


class AgentChecklistView(AgentView):
    """GET — all 100 items with eligibility for this package, status, findings, prerequisites and policy notes."""

    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        return Response(checklist_service.checklist_rows(business, staff=True))


class AgentChecklistItemView(AgentView):
    """
    PUT — record a finding or move one item. The same rules as for people apply (a finding for "checked", proof for
    "done", blocker + owner + next action for "blocked", nothing but "outside the package" for excluded items), plus a
    daily cap per website so a looping agent can't flood the history. Unchanged repeats are ignored, so retries are safe.
    """

    def put(self, request, business_id, feature_id):
        business = get_business_or_404(request.user, business_id)
        if business.is_effective_paused:
            return Response({"detail": "This business is paused."}, status=status.HTTP_400_BAD_REQUEST)
        subscription = Subscription.objects.filter(business=business).first()
        if subscription is not None and subscription.status != Subscription.Status.ACTIVE:
            return Response({"detail": "This website's subscription is not active."}, status=status.HTTP_403_FORBIDDEN)

        serializer = ChecklistUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        cap = settings.AGENT_MAX_CHECKLIST_UPDATES_PER_BUSINESS_PER_DAY
        recent = Event.objects.filter(business=business, kind="checklist.updated", metadata__has_key="agent", occurred_at__gte=timezone.now() - timedelta(days=1)).count()
        if recent >= cap:
            return Response({"detail": f"Daily limit of {cap} checklist updates for this website reached."}, status=status.HTTP_429_TOO_MANY_REQUESTS)

        try:
            checklist_service.update_item(business, feature_id.upper(), dict(serializer.validated_data), user=None, agent_name=request.auth.name)
        except RequestRejected as error:
            return _rejected(error)
        rows = checklist_service.checklist_rows(business, staff=True)["rows"]
        return Response(next(row for row in rows if row["feature_id"] == feature_id.upper()))


class AgentScopeRequestsView(AgentView):
    """GET open and past requests (check before asking). POST — ask for work the package doesn't cover; a person decides."""

    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        return Response([scope_request_dict(r, staff=True) for r in business.scope_requests.select_related("requested_by")[:100]])

    def post(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        serializer = ScopeRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            created = request_scope_change(business, serializer.validated_data["capability"], user=None, note=serializer.validated_data.get("note", ""), agent_name=request.auth.name)
        except RequestRejected as error:
            return _rejected(error)
        return Response(scope_request_dict(created, staff=True), status=status.HTTP_201_CREATED)
