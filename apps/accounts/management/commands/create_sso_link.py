from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import SsoLink


class Command(BaseCommand):
    help = "Create a GoHighLevel sign-in link for a user. The secret is printed once and cannot be recovered."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="The user the link signs in as")
        parser.add_argument("--location-id", required=True, help="The GoHighLevel sub-account (location) id")
        parser.add_argument("--name", default="ghl-menu", help="A label for the link")
        parser.add_argument("--base-url", default="https://seopilot.theservicepilot.com")

    def handle(self, *args, email, location_id, name, base_url, **options):
        user = get_user_model().objects.filter(email=email.lower()).first()
        if user is None:
            raise CommandError(f"No user with email {email}.")
        link, secret = SsoLink.issue(name=name, user=user, location_id=location_id)
        url = f"{base_url.rstrip('/')}/?location_id={location_id}&sso={secret}"
        self.stdout.write(self.style.SUCCESS(f"Created sign-in link '{link.name}' for {user.email}. Paste this into the GHL custom menu link; it is not shown again:\n\n{url}\n"))
