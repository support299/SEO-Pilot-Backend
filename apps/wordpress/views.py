from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from common.permissions import get_business_or_404

from . import services
from .models import ChangePacket
from .tasks import apply_change_packet_task, rollback_change_packet_task


class ConnectionStatusView(APIView):
    def get(self, request, business_id):
        business = get_business_or_404(request.user, business_id)
        connection = services.get_connection(business)
        if connection is None:
            return Response({"connected": False})
        return Response(
            {
                "connected": True,
                "website_url": connection.website_url,
                "wp_user_name": connection.wp_user_name,
                "status": connection.status,
                "connected_at": connection.connected_at.isoformat(),
                "last_verified_at": connection.last_verified_at.isoformat() if connection.last_verified_at else None,
                "last_error": connection.last_error,
            }
        )


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
