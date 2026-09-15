from django.contrib import admin

from .models import SearchConsoleConnection, SearchConsoleDailyMetric, SearchConsoleTopPage, SearchConsoleTopQuery


@admin.register(SearchConsoleConnection)
class SearchConsoleConnectionAdmin(admin.ModelAdmin):
    list_display = ["business", "site_url", "last_synced_at", "last_sync_error"]


admin.site.register(SearchConsoleDailyMetric)
admin.site.register(SearchConsoleTopQuery)
admin.site.register(SearchConsoleTopPage)
