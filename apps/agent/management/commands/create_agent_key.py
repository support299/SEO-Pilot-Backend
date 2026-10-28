from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.agent.models import AgentKey


class Command(BaseCommand):
    help = "Create an API key for an AI agent. The key is printed once and cannot be recovered."

    def add_arguments(self, parser):
        parser.add_argument("name", help='A label for the agent, e.g. "claude-daily"')
        parser.add_argument("--admin-email", required=True, help="The agency admin this key acts for")

    def handle(self, *args, name, admin_email, **options):
        user = get_user_model().objects.filter(email=admin_email.lower()).first()
        if user is None or not user.is_agency_admin:
            raise CommandError(f"{admin_email} is not an agency admin.")
        key, raw_key = AgentKey.issue(name=name, created_by=user)
        self.stdout.write(self.style.SUCCESS(f"Created agent key '{key.name}'. Copy it now; it is not shown again:\n\n{raw_key}\n"))
