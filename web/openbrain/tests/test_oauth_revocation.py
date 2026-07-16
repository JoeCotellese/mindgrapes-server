"""OAuth revocation + grant-listing services (Slice 3.3, #75).

``revoke_client`` is the issue side of "revoke → 401": it kills a client's
refresh families and writes the ``oauth_revocation`` watermark the MCP resource
server enforces. ``list_active_grants`` backs the connected-clients screen.
"""

import time
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from openbrain.oauth.models import OAuthClient, OAuthRevocation, OAuthToken
from openbrain.oauth.services import (
    list_active_grants,
    revoke_client,
    revoke_refresh_family,
    revoke_user_clients,
)

pytestmark = pytest.mark.django_db

User = get_user_model()


def _client(client_id="cid123", name="Claude"):
    client = OAuthClient(client_id=client_id)
    client.set_client_metadata({"client_name": name, "scope": "brain:read brain:write"})
    client.save()
    return client


def _token(
    user,
    *,
    client_id="cid123",
    family_id=None,
    issued_at=None,
    revoked=False,
    scope="brain:read brain:write",
):
    now = int(time.time())
    return OAuthToken.objects.create(
        user=user,
        client_id=client_id,
        sub=str(user.pk),
        family_id=family_id or uuid.uuid4(),
        access_token="header.payload.sig",
        refresh_token=uuid.uuid4().hex,
        scope=scope,
        issued_at=issued_at if issued_at is not None else now,
        expires_in=600,
        refresh_token_revoked_at=now if revoked else 0,
        access_token_revoked_at=now if revoked else 0,
    )


def test_revoke_refresh_family_revokes_every_token_in_the_family():
    user = User.objects.create_user(email="a@example.net")
    family = uuid.uuid4()
    _token(user, family_id=family)
    _token(user, family_id=family)
    _token(user, family_id=uuid.uuid4())  # a different family survives

    count = revoke_refresh_family(family)

    assert count == 2
    in_family = OAuthToken.objects.filter(family_id=family)
    assert all(t.refresh_token_revoked_at and t.access_token_revoked_at for t in in_family)
    other = OAuthToken.objects.exclude(family_id=family).get()
    assert other.refresh_token_revoked_at == 0


def test_revoke_client_writes_marker_and_revokes_only_that_clients_tokens():
    user = User.objects.create_user(email="b@example.net")
    _client("keep", name="Keeper")
    _client("kill", name="Doomed")
    _token(user, client_id="kill", family_id=uuid.uuid4())
    _token(user, client_id="kill", family_id=uuid.uuid4())
    _token(user, client_id="keep")

    revoke_client(user, "kill")

    killed = OAuthToken.objects.filter(client_id="kill")
    assert all(t.refresh_token_revoked_at for t in killed)
    kept = OAuthToken.objects.get(client_id="keep")
    assert kept.refresh_token_revoked_at == 0
    assert OAuthRevocation.objects.filter(user=user, client_id="kill").exists()
    assert not OAuthRevocation.objects.filter(user=user, client_id="keep").exists()


def test_revoke_client_only_targets_the_given_user():
    a = User.objects.create_user(email="owner@example.net")
    b = User.objects.create_user(email="other@example.net")
    _token(a, client_id="shared")
    _token(b, client_id="shared")

    revoke_client(a, "shared")

    assert OAuthToken.objects.get(user=a).refresh_token_revoked_at != 0
    assert OAuthToken.objects.get(user=b).refresh_token_revoked_at == 0
    assert OAuthRevocation.objects.filter(user=a).count() == 1
    assert not OAuthRevocation.objects.filter(user=b).exists()


def test_revoke_client_is_idempotent_and_moves_the_watermark_forward():
    user = User.objects.create_user(email="c@example.net")
    _token(user, client_id="cid")

    revoke_client(user, "cid")
    first = OAuthRevocation.objects.get(user=user, client_id="cid").revoked_after

    OAuthRevocation.objects.filter(user=user, client_id="cid").update(
        revoked_after=first - timezone.timedelta(hours=1)
    )
    revoke_client(user, "cid")

    markers = OAuthRevocation.objects.filter(user=user, client_id="cid")
    assert markers.count() == 1  # upsert, not a second row
    assert markers.get().revoked_after > first - timezone.timedelta(hours=1)


def test_revoke_client_is_a_noop_when_user_never_had_the_client():
    user = User.objects.create_user(email="d@example.net")

    revoke_client(user, "never-connected")

    # No junk watermark for a client the user never authorized.
    assert not OAuthRevocation.objects.filter(user=user).exists()


def test_list_active_grants_returns_only_the_users_active_grants():
    user = User.objects.create_user(email="e@example.net")
    other = User.objects.create_user(email="f@example.net")
    _client("alive", name="Claude Desktop")
    _client("dead", name="Old Client")
    _token(user, client_id="alive", scope="brain:read")
    _token(user, client_id="dead", revoked=True)
    _token(other, client_id="alive")  # another user's grant for the same client

    grants = list_active_grants(user)

    assert [g.client_id for g in grants] == ["alive"]
    grant = grants[0]
    assert grant.name == "Claude Desktop"
    assert grant.scope_sentences == [
        "Search and read your saved thoughts and memories."
    ]
    assert grant.connected is not None


def test_list_active_grants_connected_is_the_earliest_issuance():
    user = User.objects.create_user(email="g@example.net")
    _client("cid")
    family = uuid.uuid4()
    _token(user, client_id="cid", family_id=family, issued_at=1000)
    _token(user, client_id="cid", family_id=family, issued_at=2000)  # a rotation

    grant = list_active_grants(user)[0]

    assert int(grant.connected.timestamp()) == 1000


def test_revoke_user_clients_revokes_every_active_client_of_the_user():
    user = User.objects.create_user(email="owner@example.net")
    _client("a", name="App A")
    _client("b", name="App B")
    _token(user, client_id="a", family_id=uuid.uuid4())
    _token(user, client_id="b", family_id=uuid.uuid4())

    count = revoke_user_clients(user)

    assert count == 2
    assert all(t.refresh_token_revoked_at for t in OAuthToken.objects.filter(user=user))
    assert OAuthRevocation.objects.filter(user=user, client_id="a").exists()
    assert OAuthRevocation.objects.filter(user=user, client_id="b").exists()


def test_revoke_user_clients_counts_each_client_once_across_rotations():
    user = User.objects.create_user(email="multi@example.net")
    _token(user, client_id="cid", family_id=uuid.uuid4())
    _token(user, client_id="cid", family_id=uuid.uuid4())  # a rotation, same client

    assert revoke_user_clients(user) == 1


def test_revoke_user_clients_leaves_other_users_clients_untouched():
    a = User.objects.create_user(email="a@example.net")
    b = User.objects.create_user(email="b@example.net")
    _token(a, client_id="shared")
    _token(b, client_id="shared")

    revoke_user_clients(a)

    assert OAuthToken.objects.get(user=a).refresh_token_revoked_at != 0
    assert OAuthToken.objects.get(user=b).refresh_token_revoked_at == 0
    assert not OAuthRevocation.objects.filter(user=b).exists()


def test_revoke_user_clients_skips_already_revoked_clients():
    user = User.objects.create_user(email="x@example.net")
    _token(user, client_id="dead", revoked=True)

    assert revoke_user_clients(user) == 0
    # No junk watermark for a client that was already dead.
    assert not OAuthRevocation.objects.filter(user=user, client_id="dead").exists()


def test_revoke_user_clients_is_a_noop_when_the_user_has_none():
    user = User.objects.create_user(email="none@example.net")

    assert revoke_user_clients(user) == 0
    assert not OAuthRevocation.objects.filter(user=user).exists()
