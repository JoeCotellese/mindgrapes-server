"""django-allauth adapters for the Mind Grapes product layer.

OpenBrainMFAAdapter pins the WebAuthn relying-party identity; the account
adapter closes self-serve signup (users are provisioned by an admin, Slice 2).
"""

from allauth.account.adapter import DefaultAccountAdapter
from allauth.mfa.adapter import DefaultMFAAdapter
from django.conf import settings
from django.http import HttpRequest


class OpenBrainMFAAdapter(DefaultMFAAdapter):
    """WebAuthn relying-party configuration.

    In production the relying-party id derives from the request host
    (brain.example.net behind the Caddy edge). In development it is pinned to
    'localhost' so http://localhost works regardless of 127.0.0.1 variants.
    """

    def get_public_key_credential_rp_entity(self) -> dict[str, str]:
        rp = super().get_public_key_credential_rp_entity()
        if settings.DEBUG:
            rp["id"] = "localhost"
            rp["name"] = "Mind Grapes (Development)"
        else:
            rp["name"] = "Mind Grapes"
        return rp


class OpenBrainAccountAdapter(DefaultAccountAdapter):
    """No self-serve signup: users are provisioned by an admin (Slice 2)."""

    def is_open_for_signup(self, request: HttpRequest) -> bool:
        return False
