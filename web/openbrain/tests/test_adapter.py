"""Adapters: WebAuthn relying-party identity and a closed signup door.

The relying-party id must match the origin the browser sees. In production it
derives from the request host (brain.example.net behind Caddy); in
development it is pinned to 'localhost' so http://localhost works regardless of
127.0.0.1 variants. Self-serve signup is closed — users are provisioned by an
admin (Slice 2).
"""

from allauth.core import context
from django.test import RequestFactory, override_settings

from openbrain.accounts.adapter import OpenBrainAccountAdapter, OpenBrainMFAAdapter


@override_settings(DEBUG=True)
def test_rp_id_is_localhost_in_debug():
    request = RequestFactory().get("/", HTTP_HOST="localhost:8080")
    with context.request_context(request):
        rp = OpenBrainMFAAdapter().get_public_key_credential_rp_entity()
    assert rp["id"] == "localhost"
    assert "Development" in rp["name"]


@override_settings(DEBUG=False, ALLOWED_HOSTS=["brain.example.net"])
def test_rp_id_derives_from_host_in_production():
    request = RequestFactory().get("/", HTTP_HOST="brain.example.net")
    with context.request_context(request):
        rp = OpenBrainMFAAdapter().get_public_key_credential_rp_entity()
    assert rp["id"] == "brain.example.net"
    assert rp["name"] == "Mind Grapes"


def test_signup_is_closed():
    request = RequestFactory().get("/")
    assert OpenBrainAccountAdapter().is_open_for_signup(request) is False
