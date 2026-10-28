from django.contrib import admin
from django.utils import timezone

from .models import AgentKey


@admin.register(AgentKey)
class AgentKeyAdmin(admin.ModelAdmin):
    list_display = ["name", "key_hint", "created_by", "created_at", "last_used_at", "revoked_at"]
    readonly_fields = ["name", "key_hash", "key_hint", "created_by", "created_at", "last_used_at"]
    actions = ["revoke"]

    def has_add_permission(self, request):
        return False  # keys are issued with `manage.py create_agent_key` so the raw value is shown exactly once

    @admin.action(description="Revoke selected keys")
    def revoke(self, request, queryset):
        queryset.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())
