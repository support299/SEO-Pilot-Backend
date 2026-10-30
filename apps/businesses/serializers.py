from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from rest_framework import serializers

from common.permissions import accounts_for_user, is_agency_admin

from .models import Account, Business, Membership

User = get_user_model()


class WebsiteField(serializers.URLField):
    """A website address as people type it: "acme.com" is accepted and stored as "https://acme.com"."""

    def to_internal_value(self, data):
        value = data.strip() if isinstance(data, str) else data
        if isinstance(value, str) and value and "://" not in value:
            value = f"https://{value}"
        return super().to_internal_value(value)


class BusinessSerializer(serializers.ModelSerializer):
    website = WebsiteField(max_length=200, required=False, allow_blank=True, allow_null=True)
    account_name = serializers.CharField(source="account.name", read_only=True)
    is_account_paused = serializers.BooleanField(source="account.is_paused", read_only=True)
    effective_paused = serializers.BooleanField(source="is_effective_paused", read_only=True)
    can_edit = serializers.SerializerMethodField()

    class Meta:
        model = Business
        fields = [
            "id",
            "account",
            "account_name",
            "name",
            "website",
            "is_paused",
            "is_account_paused",
            "effective_paused",
            "can_edit",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "account_name", "is_account_paused", "effective_paused", "created_at", "updated_at"]

    def validate_account(self, account):
        request = self.context["request"]
        if account.id not in accounts_for_user(request.user):
            raise serializers.ValidationError("You don't have access to this account.")
        return account

    def get_can_edit(self, obj: Business) -> bool:
        """False for customers (viewer role): the UI hides every action button for them."""
        user = self.context["request"].user
        if is_agency_admin(user):
            return True
        writable = self.context.get("_writable_account_ids")
        if writable is None:
            writable = set(Membership.objects.filter(user=user).exclude(role=Membership.Role.VIEWER).values_list("account_id", flat=True))
            self.context["_writable_account_ids"] = writable
        return obj.account_id in writable


class CustomerCreateSerializer(serializers.Serializer):
    """Agency admin onboarding: one customer account, its first business, and (optionally) a read-only login."""

    customer_name = serializers.CharField(max_length=200)
    business_name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    website = WebsiteField(required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    full_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)

    def validate(self, attrs):
        email = (attrs.get("email") or "").lower()
        attrs["email"] = email
        if email:
            existing = User.objects.filter(email=email).first()
            if existing is None:
                if not attrs.get("password"):
                    raise serializers.ValidationError({"password": "Set a starting password for the new customer login."})
                validate_password(attrs["password"])
            elif existing.is_agency_admin:
                raise serializers.ValidationError({"email": "That email belongs to an agency admin."})
        return attrs

    @transaction.atomic
    def create(self, validated_data) -> dict:
        account = Account.objects.create(name=validated_data["customer_name"])
        business = Business.objects.create(
            account=account,
            name=validated_data.get("business_name") or validated_data["customer_name"],
            website=validated_data.get("website") or None,
        )
        email = validated_data.get("email")
        if email:
            user = User.objects.filter(email=email).first()
            if user is None:
                user = User.objects.create_user(email=email, password=validated_data["password"], full_name=validated_data.get("full_name", ""))
            Membership.objects.get_or_create(account=account, user=user, defaults={"role": Membership.Role.VIEWER})
        return {"account": account, "business": business, "login_email": email or None}
