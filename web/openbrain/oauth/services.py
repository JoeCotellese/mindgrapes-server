"""Revocation + grant-listing for the connected-clients screen (Slice 3.3, #75).

``revoke_client`` is the issue side of "revoke → 401": it kills the client's
refresh families (so they can't renew) and writes the ``oauth_revocation``
watermark the MCP resource server enforces on every ``/mcp`` call. The
watermark, not jti enumeration, is what makes a still-valid access token stop
working immediately.
"""

import time
from dataclasses import dataclass
from datetime import UTC, datetime

from django.db.models import Min
from django.utils import timezone

from .models import OAuthClient, OAuthRevocation, OAuthToken
from .scopes import describe


@dataclass(frozen=True)
class Grant:
    """One client a user has authorized, as shown on the clients screen."""

    client_id: str
    name: str
    connected: datetime
    scope_sentences: list[str]


def revoke_refresh_family(family_id) -> int:
    """Revoke every token in a refresh lineage. Returns the number revoked.

    Shared with the Slice 2 disable cascade (#3.4): killing a family stops both
    the access token (audit flag) and any further refresh.
    """
    now = int(time.time())
    return OAuthToken.objects.filter(family_id=family_id).update(
        refresh_token_revoked_at=now, access_token_revoked_at=now
    )


def revoke_client(user, client_id) -> None:
    """Disconnect a client for a user: kill its families + write the watermark.

    A no-op when the user never authorized the client, so an arbitrary POST
    can't litter the table with watermarks for clients the user never had.
    The watermark is upserted (``revoked_after`` moves forward) so re-revoking
    after a re-auth still kills the newer tokens.
    """
    families = list(
        OAuthToken.objects.filter(user=user, client_id=client_id)
        .values_list("family_id", flat=True)
        .distinct()
    )
    if not families:
        return
    for family_id in families:
        revoke_refresh_family(family_id)
    OAuthRevocation.objects.update_or_create(
        user=user,
        client_id=client_id,
        defaults={"revoked_after": timezone.now()},
    )


def revoke_user_clients(user) -> int:
    """Disconnect every client a user has connected. Returns clients revoked.

    The Slice 2 disable cascade (#3.4): when a member is disabled, all of their
    connected clients must stop working. Iterate the user's distinct *active*
    client_ids and ``revoke_client`` each — killing the refresh families and
    writing the ``oauth_revocation`` watermark — so even a still-valid access
    token is rejected at the next ``/mcp`` call.
    """
    client_ids = list(
        OAuthToken.objects.filter(user=user, refresh_token_revoked_at=0)
        .values_list("client_id", flat=True)
        .distinct()
    )
    for client_id in client_ids:
        revoke_client(user, client_id)
    return len(client_ids)


def list_active_grants(user) -> list[Grant]:
    """The user's still-live client authorizations, oldest connection first.

    A grant is "active" while it has at least one token whose refresh lineage
    is unrevoked; revoking a client drops it from this list. "Connected" is the
    earliest issuance for the client (rotation keeps issuing within a family).
    Last-used is deliberately omitted — it isn't tracked, and the screen never
    shows a "never".
    """
    active = OAuthToken.objects.filter(user=user, refresh_token_revoked_at=0)
    rows = (
        active.values("client_id")
        .annotate(connected=Min("issued_at"))
        .order_by("connected")
    )
    grants = []
    for row in rows:
        client_id = row["client_id"]
        latest = active.filter(client_id=client_id).order_by("-issued_at").first()
        client = OAuthClient.objects.filter(client_id=client_id).first()
        name = (client.client_name if client else None) or "An unnamed application"
        grants.append(
            Grant(
                client_id=client_id,
                name=name,
                connected=datetime.fromtimestamp(row["connected"], tz=UTC),
                scope_sentences=describe(latest.scope if latest else ""),
            )
        )
    return grants
