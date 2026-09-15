from rest_framework import serializers

from .models import SearchConsoleConnection, SearchConsoleDailyMetric, SearchConsoleTopPage, SearchConsoleTopQuery


class ConnectionStatusSerializer(serializers.ModelSerializer):
    connected = serializers.SerializerMethodField()

    class Meta:
        model = SearchConsoleConnection
        fields = ["connected", "site_url", "connected_at", "last_synced_at", "last_sync_error"]

    def get_connected(self, _obj) -> bool:
        return True


class DailyMetricSerializer(serializers.ModelSerializer):
    class Meta:
        model = SearchConsoleDailyMetric
        fields = ["date", "clicks", "impressions", "ctr", "position"]


class TopQuerySerializer(serializers.ModelSerializer):
    class Meta:
        model = SearchConsoleTopQuery
        fields = ["query", "clicks", "impressions", "ctr", "position"]


class TopPageSerializer(serializers.ModelSerializer):
    class Meta:
        model = SearchConsoleTopPage
        fields = ["page", "clicks", "impressions", "ctr", "position"]
