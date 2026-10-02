from rest_framework import serializers

from .models import AnalyticsConnection, AnalyticsDailyMetric


class ConnectionStatusSerializer(serializers.ModelSerializer):
    connected = serializers.SerializerMethodField()
    conversions_measurable = serializers.SerializerMethodField()

    class Meta:
        model = AnalyticsConnection
        fields = [
            "connected",
            "property_id",
            "property_name",
            "website_url",
            "conversion_events",
            "conversions_measurable",
            "connected_at",
            "last_synced_at",
            "last_sync_error",
        ]

    def get_connected(self, _obj) -> bool:
        return True

    def get_conversions_measurable(self, obj) -> bool:
        return bool(obj.conversion_events)
