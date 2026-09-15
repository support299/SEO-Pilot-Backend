from rest_framework import serializers

from common.permissions import accounts_for_user

from .models import Business


class BusinessSerializer(serializers.ModelSerializer):
    class Meta:
        model = Business
        fields = ["id", "account", "name", "website", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_account(self, account):
        request = self.context["request"]
        if account.id not in accounts_for_user(request.user):
            raise serializers.ValidationError("You don't have access to this account.")
        return account
