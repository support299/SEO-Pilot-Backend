from django.contrib import admin

from .models import Account, Business, Membership


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ["name", "created_at"]
    inlines = [MembershipInline]


@admin.register(Business)
class BusinessAdmin(admin.ModelAdmin):
    list_display = ["name", "account", "website", "created_at"]
    list_filter = ["account"]
