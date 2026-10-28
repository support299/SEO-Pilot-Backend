import hashlib
import secrets

from django.conf import settings
from django.db import models

KEY_PREFIX = "sp_agent_"


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


class AgentKey(models.Model):
    """
    A credential for an AI agent (Claude, Codex, ...) acting for the agency. It
    can read business data and *propose* changes through /api/v1/agent/ — it is
    only accepted by those endpoints, so it can never approve, apply, or roll
    back anything. Only a SHA-256 hash is stored; the raw key is shown once.
    """

    name = models.CharField(max_length=100)
    key_hash = models.CharField(max_length=64, unique=True)
    key_hint = models.CharField(max_length=12)  # last few characters, to tell keys apart in the admin
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="agent_keys")
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "agent_key"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.name} (…{self.key_hint})"

    @classmethod
    def issue(cls, *, name: str, created_by) -> tuple["AgentKey", str]:
        raw_key = KEY_PREFIX + secrets.token_urlsafe(32)
        key = cls.objects.create(name=name, key_hash=hash_key(raw_key), key_hint=raw_key[-6:], created_by=created_by)
        return key, raw_key
