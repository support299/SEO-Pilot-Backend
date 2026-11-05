from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.subscriptions.checklist import EXPECTED_TOTALS, ImportInvalid, apply_overrides, import_registry, read_rows, validate_rows


class Command(BaseCommand):
    help = (
        "Load the SEO-001..SEO-100 registry from a CSV or JSON file. Columns: id, title, starter, growth, authority "
        "(codes I/C/A/N/X), and optionally prerequisites, policy_notes, category. Refuses anything that isn't exactly 100 "
        "unique IDs with the tier totals from the handoff. Nothing is written unless the whole file is valid."
    )

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--label", default="v11", help="Checklist version label (default: v11)")
        parser.add_argument("--check", action="store_true", help="Validate only. Writes nothing.")
        parser.add_argument("--ignore-totals", action="store_true", help="Skip the tier-total check from the handoff. Only after the owner confirms the file is the right version.")

    def handle(self, *args, path, label, **options):
        file = Path(path)
        if not file.exists():
            raise CommandError(f"No such file: {file}")
        rows = read_rows(file)
        totals = None if options["ignore_totals"] else EXPECTED_TOTALS
        if options["check"]:
            problems = validate_rows(apply_overrides(rows), totals)
            if problems:
                for problem in problems:
                    self.stderr.write(f"  - {problem}")
                raise CommandError("File is not valid. Nothing was changed.")
            self.stdout.write(self.style.SUCCESS("File is valid. Nothing was changed."))
            return
        if options["ignore_totals"]:
            self.stderr.write(self.style.WARNING("Tier totals were NOT checked."))
        try:
            count = import_registry(rows, label, totals)
        except ImportInvalid as error:
            for problem in error.problems:
                self.stderr.write(f"  - {problem}")
            raise CommandError("Import refused. Nothing was changed.")
        self.stdout.write(self.style.SUCCESS(f"Imported {count} checklist items ({label})."))
