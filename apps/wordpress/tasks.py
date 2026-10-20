import logging

from celery import shared_task

logger = logging.getLogger("django")


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def apply_change_packet_task(self, packet_id: int) -> None:
    from . import services
    from .models import ChangePacket

    packet = ChangePacket.objects.filter(id=packet_id).first()
    if packet is None:
        logger.warning("apply_change_packet_task: packet %s not found", packet_id)
        return
    try:
        services.apply_change_packet(packet)
    except services.WordPressIntegrationError as exc:
        logger.warning("apply_change_packet_task failed for packet %s: %s", packet_id, exc)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def rollback_change_packet_task(self, packet_id: int) -> None:
    from . import services
    from .models import ChangePacket

    packet = ChangePacket.objects.filter(id=packet_id).first()
    if packet is None:
        logger.warning("rollback_change_packet_task: packet %s not found", packet_id)
        return
    try:
        services.rollback_change_packet(packet)
    except services.WordPressIntegrationError as exc:
        logger.warning("rollback_change_packet_task failed for packet %s: %s", packet_id, exc)
