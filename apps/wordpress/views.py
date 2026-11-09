from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from rest_framework.permissions import IsAuthenticated

from common.permissions import IsAgencyAdmin, get_business_or_404

from . import services
from .models import ChangePacket
from .tasks import apply_change_packet_task, rollback_change_packet_task


class ConnectionStatusView(APIView):
    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        connection = services.get_connection(business)
        if connection is None:
            return Response({"connected": False})
        if connection.seo_plugin_checked_at is None:  # connected before the plugin check existed
            connection = services.refresh_plugin_check(connection)
        return Response(services.serialize_connection(connection))


class PluginCheckView(APIView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "sync"

    def post(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        try:
            connection = services.require_connection(business)
        except services.WordPressIntegrationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(services.serialize_connection(services.refresh_plugin_check(connection)))


class PublishingView(APIView):
    """Staff switch: off when the connected WordPress is only a content store behind a non-WordPress live site."""

    permission_classes = [IsAuthenticated, IsAgencyAdmin]

    def post(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        enabled = request.data.get("enabled")
        if not isinstance(enabled, bool):
            return Response({"detail": "enabled must be true or false."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            connection = services.set_publishing(business, enabled, user=request.user)
        except services.WordPressIntegrationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(services.serialize_connection(connection))


class ConnectView(APIView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "sync"

    def post(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        website_url = request.data.get("website_url", "")
        username = request.data.get("username", "")
        application_password = request.data.get("application_password", "")
        try:
            services.connect(business, website_url=website_url, username=username, application_password=application_password)
        except services.WordPressIntegrationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(status=status.HTTP_204_NO_CONTENT)


class DisconnectView(APIView):
    def post(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        services.disconnect(business)
        return Response(status=status.HTTP_204_NO_CONTENT)


class InventoryView(APIView):
    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        object_type = request.query_params.get("type", "page")
        if object_type not in ("page", "post"):
            return Response({"detail": "type must be 'page' or 'post'."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            items = services.list_inventory(business, object_type)
        except services.WordPressIntegrationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(items)


class ChangePacketListCreateView(APIView):
    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        packets = ChangePacket.objects.filter(business=business)
        return Response([services.serialize_packet(p) for p in packets])

    def post(self, request, business_id):
        business = get_business_or_404(request.user, business_id)

        target_object_type = request.data.get("target_object_type", "")
        if target_object_type not in (ChangePacket.ObjectType.PAGE, ChangePacket.ObjectType.POST):
            return Response({"detail": "target_object_type must be 'page' or 'post'."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            target_object_id = int(request.data.get("target_object_id"))
        except (TypeError, ValueError):
            return Response({"detail": "target_object_id must be a number."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            packet = services.prepare_change_packet(
                business,
                user=request.user,
                target_object_type=target_object_type,
                target_object_id=target_object_id,
                proposed_changes=request.data.get("proposed_changes") or {},
                reason=request.data.get("reason", ""),
            )
        except services.WordPressIntegrationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(services.serialize_packet(packet), status=status.HTTP_201_CREATED)


class ChangePacketDecisionView(APIView):
    def post(self, request, business_id, packet_id):
        business = get_business_or_404(request.user, business_id)
        packet = get_object_or_404(ChangePacket, id=packet_id, business=business)
        approve = request.data.get("approve")
        if not isinstance(approve, bool):
            return Response({"detail": "approve must be true or false."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            packet = services.decide_change_packet(packet, user=request.user, approve=approve)
        except services.WordPressIntegrationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(services.serialize_packet(packet))


class ChangePacketApplyView(APIView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "sync"

    def post(self, request, business_id, packet_id):
        business = get_business_or_404(request.user, business_id)
        packet = get_object_or_404(ChangePacket, id=packet_id, business=business)
        if packet.status != ChangePacket.Status.APPROVED:
            return Response({"detail": "Approve this change before applying it."}, status=status.HTTP_400_BAD_REQUEST)
        apply_change_packet_task.delay(packet.id)
        return Response({"detail": "Publishing started."}, status=status.HTTP_202_ACCEPTED)


class ChangePacketRollbackView(APIView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "sync"

    def post(self, request, business_id, packet_id):
        business = get_business_or_404(request.user, business_id)
        packet = get_object_or_404(ChangePacket, id=packet_id, business=business)
        if packet.status != ChangePacket.Status.APPLIED:
            return Response({"detail": "This change isn't ready for rollback."}, status=status.HTTP_400_BAD_REQUEST)
        rollback_change_packet_task.delay(packet.id)
        return Response({"detail": "Rollback started."}, status=status.HTTP_202_ACCEPTED)
