"""Scope vocabulary for the OAuth consent screen (Slice 3.2, #74).

Scopes are constrained and granular so the consent screen can show the user a
plain-language sentence per permission. The MCP resource server does not
enforce scope (single household), so this is a product/UX surface, not a
security boundary — but keeping the set small keeps consent honest.
"""

# Ordered: the consent screen renders sentences in this order regardless of how
# the client ordered the requested scope string.
SCOPES = {
    "brain:read": "Search and read your saved thoughts and memories.",
    "brain:write": "Save new thoughts and memories to your brain.",
}

#: Granted when a client requests no specific scope.
DEFAULT_SCOPE = "brain:read brain:write"


def describe(scope: str | None) -> list[str]:
    """Human sentences for a space-delimited scope string, in ``SCOPES`` order.

    Unknown scopes are dropped so a malformed or over-broad request can never
    inject arbitrary text into the consent screen.
    """
    requested = set((scope or "").split())
    return [sentence for name, sentence in SCOPES.items() if name in requested]
