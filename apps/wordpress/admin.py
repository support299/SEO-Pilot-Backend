from django.contrib import admin

from .models import ChangePacket, WordPressConnection


@admin.register(WordPressConnection)
class WordPressConnectionAdmin(admin.ModelAdmin):
    list_display = ["business", "website_url", "status", "last_verified_at"]


@admin.register(ChangePacket)
class ChangePacketAdmin(admin.ModelAdmin):
    list_display = ["business", "target_object_type", "target_object_id", "status", "created_at"]
    list_filter = ["status", "target_object_type"]
