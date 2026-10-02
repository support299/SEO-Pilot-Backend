from django.contrib import admin

from .models import AnalyticsConnection, AnalyticsDailyMetric


@admin.register(AnalyticsConnection)
class AnalyticsConnectionAdmin(admin.ModelAdmin):
    list_display = ["business", "property_name", "property_id", "last_synced_at", "last_sync_error"]


admin.site.register(AnalyticsDailyMetric)
