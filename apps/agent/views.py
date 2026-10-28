from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.approvals.services import list_approvals
from apps.businesses.models import Business
from apps.local_visibility import services as lv_services
from apps.local_visibility.models import TrackedKeyword
from apps.search_console.models import SearchConsoleTopPage, SearchConsoleTopQuery
from apps.search_console.serializers import TopPageSerializer, TopQuerySerializer
from apps.site_health.services import latest_report
from apps.wordpress import services as wp_services
from apps.wordpress.models import ChangePacket, WordPressConnection
from common.permissions import get_business_or_404

from .authentication import AgentKeyAuthentication, IsAgentKey

MAX_TITLE_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 320
MAX_REASON_LENGTH = 2000
OPEN_STATUSES = [ChangePacket.Status.AWAITING_APPROVAL, ChangePacket.Status.APPROVED, ChangePacket.Status.STALE]


class AgentView(APIView):
    """Base for every agent endpoint: key-only auth, so approve/apply/rollback are unreachable by design."""

    authentication_classes = [AgentKeyAuthentication]
    permission_classes = [IsAuthenticated, IsAgentKey]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "agent"


class BusinessListView(AgentView):
    def get(self, request):
        businesses = Business.objects.select_related("account").order_by("id")
        connected = set(WordPressConnection.objects.values_list("business_id", flat=True))
        return Response(
            [
                {"id": b.id, "name": b.name, "website": b.website, "customer": b.account.name, "wordpress_connected": b.id in connected}
                for b in businesses
            ]
        )


class BusinessContextView(AgentView):
    """Everything an agent needs to decide what to propose for one business, in a single read."""

    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        connection = wp_services.get_connection(business)
        report = {key: value for key, value in latest_report(business).items() if key != "pages"}  # the per-page crawl dump is noise here
        return Response(
            {
                "business": {"id": business.id, "name": business.name, "website": business.website},
                "wordpress_connected": connection is not None and connection.status == "connected",
                "site_health": report,
                "issues": list_approvals(business),
                "top_queries": TopQuerySerializer(SearchConsoleTopQuery.objects.filter(business=business)[:20], many=True).data,
                "top_pages": TopPageSerializer(SearchConsoleTopPage.objects.filter(business=business)[:20], many=True).data,
                "open_proposals": [wp_services.serialize_packet(p) for p in ChangePacket.objects.filter(business=business, status__in=OPEN_STATUSES)],
            }
        )


class WordPressInventoryView(AgentView):
    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        object_type = request.query_params.get("type", "page")
        if object_type not in ("page", "post"):
            return Response({"detail": "type must be 'page' or 'post'."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            return Response(wp_services.list_inventory(business, object_type))
        except wp_services.WordPressIntegrationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class LocalVisibilityView(AgentView):
    """Google Maps rank per tracked keyword; add `?keyword=<id>` for the per-point grid of one keyword."""

    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        keyword_id = request.query_params.get("keyword")
        if keyword_id is None:
            return Response(lv_services.overview(business))
        try:
            keyword = TrackedKeyword.objects.get(pk=int(keyword_id), business=business)
        except (TypeError, ValueError, TrackedKeyword.DoesNotExist):
            return Response({"detail": "Unknown keyword for this business."}, status=status.HTTP_404_NOT_FOUND)
        return Response(lv_services.keyword_detail(keyword))


class ProposalsView(AgentView):
    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        packets = ChangePacket.objects.filter(business=business)[:100]
        return Response([wp_services.serialize_packet(p) for p in packets])

    def post(self, request, business_id):
        """Queue a change as *awaiting approval*. Nothing reaches the live site until a person approves and applies it."""
        business = get_business_or_404(request.user, business_id)

        object_type = request.data.get("target_object_type")
        if object_type not in (ChangePacket.ObjectType.PAGE, ChangePacket.ObjectType.POST):
            return Response({"detail": "target_object_type must be 'page' or 'post'."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            object_id = int(request.data.get("target_object_id"))
        except (TypeError, ValueError):
            return Response({"detail": "target_object_id must be a number."}, status=status.HTTP_400_BAD_REQUEST)

        raw_changes = request.data.get("proposed_changes")
        if not isinstance(raw_changes, dict):
            return Response({"detail": "proposed_changes must be an object with a title and/or meta_description."}, status=status.HTTP_400_BAD_REQUEST)
        changes: dict[str, str] = {}
        for field, limit in (("title", MAX_TITLE_LENGTH), ("meta_description", MAX_DESCRIPTION_LENGTH)):
            value = raw_changes.get(field)
            if value in (None, ""):
                continue
            if not isinstance(value, str) or not value.strip():
                return Response({"detail": f"{field} must be text."}, status=status.HTTP_400_BAD_REQUEST)
            if len(value.strip()) > limit:
                return Response({"detail": f"{field} is too long (max {limit} characters)."}, status=status.HTTP_400_BAD_REQUEST)
            changes[field] = value.strip()
        if not changes:
            return Response({"detail": "Propose at least a title or a meta_description."}, status=status.HTTP_400_BAD_REQUEST)

        reason = request.data.get("reason") or ""
        if not isinstance(reason, str) or len(reason) > MAX_REASON_LENGTH:
            return Response({"detail": f"reason must be text of at most {MAX_REASON_LENGTH} characters."}, status=status.HTTP_400_BAD_REQUEST)

        if ChangePacket.objects.filter(business=business, target_object_type=object_type, target_object_id=object_id, status__in=OPEN_STATUSES).exists():
            return Response({"detail": "This page already has an open proposal. Wait for it to be decided."}, status=status.HTTP_409_CONFLICT)

        cap = settings.AGENT_MAX_PROPOSALS_PER_BUSINESS_PER_DAY
        recent = ChangePacket.objects.filter(business=business, source="agent", created_at__gte=timezone.now() - timedelta(days=1)).count()
        if recent >= cap:
            return Response({"detail": f"Daily proposal limit reached for this business ({cap})."}, status=status.HTTP_429_TOO_MANY_REQUESTS)

        try:
            packet = wp_services.prepare_change_packet(
                business,
                user=request.user,
                target_object_type=object_type,
                target_object_id=object_id,
                proposed_changes=changes,
                reason=reason,
                source="agent",
                agent_name=request.auth.name,
            )
        except wp_services.WordPressIntegrationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(wp_services.serialize_packet(packet), status=status.HTTP_201_CREATED)
