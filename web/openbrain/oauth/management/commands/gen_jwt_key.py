"""Print a fresh Ed25519 signing key (PKCS8 PEM) for OAUTH_JWT_PRIVATE_KEY.

    python manage.py gen_jwt_key

Paste the output into .env.dev as OAUTH_JWT_PRIVATE_KEY. A multi-line PEM works
as-is via env_file; a single-line PEM with literal \\n separators also works
(openbrain.oauth.jwt normalizes it).
"""

from django.core.management.base import BaseCommand
from joserfc.jwk import OKPKey


class Command(BaseCommand):
    help = "Generate an Ed25519 signing key (PKCS8 PEM) for OAUTH_JWT_PRIVATE_KEY."

    def handle(self, *args, **options):
        key = OKPKey.generate_key("Ed25519", private=True)
        self.stdout.write(key.as_pem(private=True).decode().rstrip("\n"))
