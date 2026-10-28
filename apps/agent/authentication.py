from datetime import timedelta

from django.utils import timezone
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import BasePermission

from .models import AgentKey, hash_key


class AgentKeyAuthentication(BaseAuthentication):
    """`Authorization: Agent <key>`. Only the agent views list this class, so a key is useless anywhere else."""

    keyword = b"agent"

    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if not parts or parts[0].lower() != self.keyword:
            return None
        if len(parts) != 2:
            raise AuthenticationFailed("Invalid agent key header.")

        key = AgentKey.objects.select_related("created_by").filter(key_hash=hash_key(parts[1].decode("utf-8", "ignore")), revoked_at__isnull=True).first()
        if key is None or not key.created_by.is_active or not key.created_by.is_agency_admin:
            raise AuthenticationFailed("Invalid or revoked agent key.")

        now = timezone.now()
        if key.last_used_at is None or now - key.last_used_at > timedelta(minutes=1):
            AgentKey.objects.filter(pk=key.pk).update(last_used_at=now)
        return key.created_by, key

    def authenticate_header(self, request):
        return "Agent"


class IsAgentKey(BasePermission):
    def has_permission(self, request, view):
        return isinstance(request.auth, AgentKey)
