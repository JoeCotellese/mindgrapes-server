"""Seed the first administrator and print a passkey-enrollment link.

There is no web signup, so the first admin is created from the CLI: a
passwordless superuser plus a single-use enrollment link to open in a browser
and register the first passkey.

    python manage.py bootstrap_admin you@example.com --base-url https://brain.example.net
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from openbrain.accounts.services import issue_enrollment_link

User = get_user_model()

DEFAULT_BASE_URL = "http://localhost:8080"


class Command(BaseCommand):
    help = "Create the first passwordless admin and print a passkey-enrollment link."

    def add_arguments(self, parser):
        parser.add_argument("email", help="Email identity for the new admin.")
        parser.add_argument(
            "--base-url",
            default=getattr(settings, "SITE_BASE_URL", DEFAULT_BASE_URL),
            help="Origin to build the absolute enrollment URL against.",
        )

    def handle(self, *args, **options):
        email = User.objects.normalize_email(options["email"])
        if User.objects.filter(email=email).exists():
            raise CommandError(f"A user with email {email} already exists.")

        # Passwordless: create the superuser without a usable password.
        user = User.objects.create_superuser(email=email)

        link = issue_enrollment_link(user, options["base_url"])
        self.stdout.write(self.style.SUCCESS(f"Created admin {email}."))
        self.stdout.write("Open this single-use link to register a passkey:")
        self.stdout.write(link)
