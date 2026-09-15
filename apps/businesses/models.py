from django.conf import settings
from django.db import models


class Account(models.Model):
    """
    The tenant boundary. Everything else (memberships, businesses, and — later —
    Search Console connections/metrics) is scoped to an Account, never directly
    to a User, so a user belonging to multiple accounts (or an account having
    multiple users) is supported from day one instead of bolted on later.
    """

    name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "businesses_account"

    def __str__(self) -> str:
        return self.name


class Membership(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        MEMBER = "member", "Member"

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.OWNER)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "businesses_membership"
        constraints = [
            models.UniqueConstraint(fields=["account", "user"], name="unique_account_user_membership"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} @ {self.account_id} ({self.role})"


class Business(models.Model):
    """A single website/business being managed for SEO, owned by an Account (not directly by a User)."""

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="businesses")
    name = models.CharField(max_length=200)
    website = models.URLField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "businesses_business"
        verbose_name_plural = "businesses"

    def __str__(self) -> str:
        return self.name
