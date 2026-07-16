"""Fixtures for the Playwright passkey e2e test.

A CDP virtual authenticator stands in for a platform authenticator (Touch ID /
Windows Hello): a discoverable, user-verifying credential store with presence
auto-confirmed, so the WebAuthn ceremony runs headless with no hardware.
"""

import os

# Playwright's sync API runs the test inside an active event loop, which trips
# Django's async-context guard during ORM access. The live_server and ORM calls
# here are genuinely synchronous, so opt out of the guard for this suite.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest  # noqa: E402


def _add_authenticator(cdp, transport):
    result = cdp.send(
        "WebAuthn.addVirtualAuthenticator",
        {
            "options": {
                "protocol": "ctap2",
                "transport": transport,
                "hasResidentKey": True,
                "hasUserVerification": True,
                "isUserVerified": True,
                "automaticPresenceSimulation": True,
            }
        },
    )
    return result["authenticatorId"]


@pytest.fixture
def virtual_authenticators(page):
    """A discoverable, user-verified virtual authenticator with a way to add more.

    Returns ``(cdp, add)`` where ``add()`` attaches a second authenticator — a
    roaming key standing in for a second device, needed to register a second
    passkey (a single authenticator excludes its own existing credential, and
    Chrome allows only one internal/platform authenticator per environment).
    """
    cdp = page.context.new_cdp_session(page)
    cdp.send("WebAuthn.enable")
    _add_authenticator(cdp, transport="internal")
    return cdp, lambda: _add_authenticator(cdp, transport="usb")
