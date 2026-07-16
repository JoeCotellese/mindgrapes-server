"""Connected-clients screen + self-revoke (Slice 3.3, #75).

The list shows a user only their own active grants; revoke is an HTMX
inline-confirm with a no-JS standalone fallback. Revoking calls
``services.revoke_client`` (covered in ``test_oauth_revocation.py``); here we
assert the HTTP seam: auth-gating, owner-scoping, the HTMX-vs-full-page branch,
and the accessibility hooks (autofocus, aria-live).
"""

import time
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from openbrain.oauth.models import OAuthClient, OAuthToken

pytestmark = pytest.mark.django_db

User = get_user_model()

CLIENTS_URL = "/connect/clients"


def _client_row(client_id="cid123", name="Claude"):
    client = OAuthClient(client_id=client_id)
    client.set_client_metadata({"client_name": name, "scope": "brain:read brain:write"})
    client.save()
    return client


def _token(user, *, client_id="cid123", revoked=False, scope="brain:read brain:write"):
    now = int(time.time())
    return OAuthToken.objects.create(
        user=user,
        client_id=client_id,
        sub=str(user.pk),
        family_id=uuid.uuid4(),
        access_token="h.p.s",
        refresh_token=uuid.uuid4().hex,
        scope=scope,
        issued_at=now,
        expires_in=600,
        refresh_token_revoked_at=now if revoked else 0,
        access_token_revoked_at=now if revoked else 0,
    )


def _logged_in(email="member@example.net"):
    user = User.objects.create_user(email=email)
    http = Client()
    http.force_login(user)
    return http, user


def test_client_list_requires_login():
    res = Client().get(CLIENTS_URL)
    assert res.status_code == 302
    assert "/accounts/login" in res["Location"]


def test_client_list_shows_only_the_requesting_users_grants():
    http, user = _logged_in()
    other = User.objects.create_user(email="other@example.net")
    _client_row("mine", name="Claude Desktop")
    _client_row("theirs", name="Someone Else's App")
    _token(user, client_id="mine")
    _token(other, client_id="theirs")

    res = http.get(CLIENTS_URL)

    assert res.status_code == 200
    assert b"Claude Desktop" in res.content
    assert b"Someone Else's App" not in res.content
    # A human scope sentence, not the raw scope token.
    assert b"Search and read" in res.content


def test_client_list_empty_state_links_to_connect():
    http, _ = _logged_in()

    res = http.get(CLIENTS_URL)

    assert res.status_code == 200
    assert b'href="/connect"' in res.content


def test_revoke_confirm_htmx_returns_inline_partial():
    http, user = _logged_in()
    _client_row("mine", name="Claude Desktop")
    _token(user, client_id="mine")

    res = http.get(f"{CLIENTS_URL}/mine/revoke/confirm", HTTP_HX_REQUEST="true")

    assert res.status_code == 200
    # A partial, not a whole document.
    assert b"<!DOCTYPE html>" not in res.content
    # The destructive action posts to revoke and takes focus.
    assert b'hx-post="/connect/clients/mine/revoke"' in res.content
    assert b"autofocus" in res.content


def test_revoke_confirm_without_htmx_returns_standalone_page():
    http, user = _logged_in()
    _client_row("mine", name="Claude Desktop")
    _token(user, client_id="mine")

    res = http.get(f"{CLIENTS_URL}/mine/revoke/confirm")

    assert res.status_code == 200
    assert b"<!DOCTYPE html>" in res.content  # full page, no-JS fallback
    assert b'<form method="post"' in res.content
    assert b"autofocus" in res.content


def test_revoke_confirm_404s_for_another_users_client():
    http, _ = _logged_in()
    other = User.objects.create_user(email="other@example.net")
    _client_row("theirs", name="Not Yours")
    _token(other, client_id="theirs")

    res = http.get(f"{CLIENTS_URL}/theirs/revoke/confirm")

    assert res.status_code == 404


def test_revoke_do_htmx_revokes_and_announces_via_aria_live():
    http, user = _logged_in()
    _client_row("mine", name="Claude Desktop")
    _token(user, client_id="mine")

    res = http.post(f"{CLIENTS_URL}/mine/revoke", HTTP_HX_REQUEST="true")

    assert res.status_code == 200
    assert b"<!DOCTYPE html>" not in res.content
    assert b"aria-live" in res.content
    assert b"Disconnected" in res.content
    # The token is dead and its family can't renew.
    assert OAuthToken.objects.get(user=user, client_id="mine").refresh_token_revoked_at != 0


def test_revoke_do_without_htmx_redirects_to_the_list():
    http, user = _logged_in()
    _client_row("mine", name="Claude Desktop")
    _token(user, client_id="mine")

    res = http.post(f"{CLIENTS_URL}/mine/revoke")

    assert res.status_code == 302
    assert res["Location"] == CLIENTS_URL
    assert OAuthToken.objects.get(user=user, client_id="mine").refresh_token_revoked_at != 0


def test_revoke_do_does_not_leak_another_users_client_name():
    http, _ = _logged_in()
    other = User.objects.create_user(email="other@example.net")
    _client_row("theirs", name="Secret Client Name")
    _token(other, client_id="theirs")

    # A crafted POST for a client the requester never connected: no-op, and the
    # confirmation must not echo back the other user's client name.
    res = http.post(f"{CLIENTS_URL}/theirs/revoke", HTTP_HX_REQUEST="true")

    assert res.status_code == 200
    assert b"Secret Client Name" not in res.content
    assert b"the application" in res.content
    # The other user's token is untouched.
    assert OAuthToken.objects.get(user=other).refresh_token_revoked_at == 0


def test_revoke_do_requires_login():
    user = User.objects.create_user(email="member@example.net")
    _client_row("mine")
    _token(user, client_id="mine")

    res = Client().post(f"{CLIENTS_URL}/mine/revoke")

    assert res.status_code == 302
    assert "/accounts/login" in res["Location"]
    assert OAuthToken.objects.get().refresh_token_revoked_at == 0  # untouched
