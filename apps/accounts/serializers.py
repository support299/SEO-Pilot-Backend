from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from rest_framework import serializers

from apps.businesses.models import Account, Membership

User = get_user_model()


class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, validators=[validate_password])
    full_name = serializers.CharField(max_length=150, required=False, allow_blank=True)

    def validate_email(self, value: str) -> str:
        value = value.lower()
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    @transaction.atomic
    def create(self, validated_data) -> User:
        user = User.objects.create_user(
            email=validated_data["email"],
            password=validated_data["password"],
            full_name=validated_data.get("full_name", ""),
        )
        # Every new user gets their own Account as its owner. This is the
        # equivalent of the old single `portal_businesses.owner_user_id` row,
        # but modeled so an account can later gain more members without a
        # schema change.
        account = Account.objects.create(name=validated_data.get("full_name") or validated_data["email"])
        Membership.objects.create(account=account, user=user, role=Membership.Role.OWNER)
        return user


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "email", "full_name", "date_joined"]
        read_only_fields = fields


class AccountSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()

    class Meta:
        model = Account
        fields = ["id", "name", "role", "created_at"]
        read_only_fields = fields

    def get_role(self, obj: Account) -> str | None:
        membership = obj.memberships.filter(user=self.context["request"].user).first()
        return membership.role if membership else None


class MeSerializer(serializers.Serializer):
    user = UserSerializer()
    accounts = AccountSerializer(many=True)
