from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.utils import timezone

from .models import SsoLink, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    ordering = ["email"]
    list_display = ["email", "full_name", "is_agency_admin", "is_staff", "is_active", "date_joined"]
    search_fields = ["email", "full_name"]
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal info", {"fields": ("full_name",)}),
        ("Permissions", {"fields": ("is_active", "is_agency_admin", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("email", "password1", "password2")}),
    )
    readonly_fields = ["date_joined"]


@admin.register(SsoLink)
class SsoLinkAdmin(admin.ModelAdmin):
    list_display = ["name", "user", "location_id", "secret_hint", "created_at", "last_used_at", "revoked_at"]
    readonly_fields = ["name", "user", "location_id", "secret_hash", "secret_hint", "created_at", "last_used_at"]
    actions = ["revoke"]

    def has_add_permission(self, request):
        return False  # issued with `manage.py create_sso_link` so the secret is shown exactly once

    @admin.action(description="Revoke selected links")
    def revoke(self, request, queryset):
        queryset.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())
