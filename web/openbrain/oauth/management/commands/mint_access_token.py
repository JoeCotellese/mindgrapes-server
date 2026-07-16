"""Mint a signed OAuth access token for a user (dev/manual testing of #73).

    python manage.py mint_access_token you@example.com [--scope "brain:read"]

Prints the token; pair it with
`curl -H "Authorization: Bearer <token>"` against the /mcp resource server.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from openbrain.oauth.jwt import sign_access_token

User = get_user_model()


class Command(BaseCommand):
    help = "Mint a signed OAuth access token for the given user."

    def add_arguments(self, parser):
        parser.add_argument("email", help="Email identity of the user to mint for.")
        parser.add_argument(
            "--scope",
            default="brain:read brain:write",
            help="Space-separated scope string to embed in the token.",
        )

    def handle(self, *args, **options):
        email = User.objects.normalize_email(options["email"])
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist as exc:
            raise CommandError(f"No user with email {email}.") from exc

        token = sign_access_token(user, scope=options["scope"])
        self.stdout.write(token)
