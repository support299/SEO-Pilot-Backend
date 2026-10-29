import hashlib
import secrets

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models


class UserManager(BaseUserManager):
    """Custom manager since this User has no `username` field — email is the identifier."""

    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra_fields):
        if not email:
            raise ValueError("Users must have an email address.")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email: str, password: str | None = None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    """
    Custom user with email as the login identifier (no separate username).
    This mirrors what Supabase Auth gave us for free — we own it explicitly now.
    """

    email = models.EmailField(unique=True, db_index=True)
    full_name = models.CharField(max_length=150, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    # Agency operator (Service Pilot staff): can see and manage every customer's businesses.
    is_agency_admin = models.BooleanField(default=False)
    date_joined = models.DateTimeField(auto_now_add=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        db_table = "accounts_user"

    def __str__(self) -> str:
        return self.email


class SsoLink(models.Model):
    """
    Lets a GoHighLevel sub-account's custom menu link sign a user in without a password. The link carries
    the GHL location id AND a long random secret; both must match. Only a SHA-256 hash of the secret is
    stored, and it is shown once when issued. Revoke it any time to cut access.
    """

    name = models.CharField(max_length=100)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sso_links")
    location_id = models.CharField(max_length=64)
    secret_hash = models.CharField(max_length=64, unique=True)
    secret_hint = models.CharField(max_length=12)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "accounts_sso_link"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.name} (…{self.secret_hint})"

    @staticmethod
    def hash_secret(raw_secret: str) -> str:
        return hashlib.sha256(raw_secret.encode("utf-8")).hexdigest()

    @classmethod
    def issue(cls, *, name: str, user, location_id: str) -> tuple["SsoLink", str]:
        raw_secret = secrets.token_urlsafe(32)
        link = cls.objects.create(name=name, user=user, location_id=location_id, secret_hash=cls.hash_secret(raw_secret), secret_hint=raw_secret[-6:])
        return link, raw_secret
