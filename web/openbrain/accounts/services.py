"""Account services: enrollment-link issuance, delivery, and member lifecycle.

Single authoritative place to mint an enrollment token, turn it into an absolute
URL, email it, and to enable/disable members. Used by the bootstrap command and
the admin user-management actions (Slice 2, #64).
"""

import logging

from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.mail import send_mail
from django.urls import reverse
from django.utils import timezone

from openbrain.oauth.services import revoke_user_clients

from .models import EnrollmentToken

logger = logging.getLogger(__name__)

User = get_user_model()


def issue_enrollment_link(user, base_url: str) -> str:
    """Mint a single-use enrollment token for user and return its absolute URL."""
    _, raw = EnrollmentToken.objects.create_for(user)
    path = reverse("accounts:enroll", args=[raw])
    return f"{base_url.rstrip('/')}{path}"


def deliver_enrollment_link(user, link: str) -> bool:
    """Email the enrollment link. Best-effort; the admin always shows the copy
    fallback, so a failed send must never block provisioning. Returns whether
    the email was sent.
    """
    try:
        send_mail(
            subject="Your Mind Grapes passkey enrollment link",
            message=(
                "Open this single-use link to register a passkey:\n\n"
                f"{link}\n\n"
                "It expires in 72 hours and can be used once."
            ),
            from_email=None,
            recipient_list=[user.email],
            fail_silently=False,
        )
        return True
    except Exception:
        logger.exception("enrollment link email failed for user %s", user.pk)
        return False


def revoke_sessions(user) -> int:
    """Delete the given user's unexpired sessions. Returns the count removed.

    DB sessions carry no FK to the user, so match on the decoded auth id. Fine at
    household scale; revisit if membership ever grows large.
    """
    killed = 0
    user_id = str(user.pk)
    for session in Session.objects.filter(expire_date__gte=timezone.now()):
        if session.get_decoded().get("_auth_user_id") == user_id:
            session.delete()
            killed += 1
    return killed


def disable_member(user) -> int:
    """Soft-disable a member, cut their OAuth clients, and revoke live sessions.

    is_active=False is the durable check (allauth's AuthenticationBackend inherits
    ModelBackend.get_user, so the next request yields AnonymousUser); purging the
    sessions makes a live cookie dead immediately, server-side. The OAuth cascade
    (#3.4) revokes every connected client so the member's API tokens stop working
    at the next call too — an explicit service call, not a signal. Returns the
    number of sessions revoked (the client cascade is a silent security side-effect).
    """
    User.objects.filter(pk=user.pk).update(is_active=False)
    revoke_user_clients(user)
    return revoke_sessions(user)


def enable_member(user) -> None:
    """Re-enable a soft-disabled member. They log back in fresh with a passkey."""
    User.objects.filter(pk=user.pk).update(is_active=True)
