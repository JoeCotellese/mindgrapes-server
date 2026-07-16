"""Passkey enrollment views.

The enroll view consumes a single-use link, logs the user in, and hands off to
passkey registration. EnrollPasskeyView registers a *discoverable*,
user-verifying passkey (passwordless=True) so that "Sign in with a passkey"
— which queries only discoverable credentials — can later find it. The stock
allauth add view registers non-discoverable second-factor keys, hence the
override.
"""

from allauth.account.internal.flows.login import record_authentication
from allauth.mfa.webauthn.internal import auth as webauthn_auth
from allauth.mfa.webauthn.views import AddWebAuthnView
from django.contrib.auth import login
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods
from django.views.generic.edit import FormView

from .models import EnrollmentToken

ALLAUTH_BACKEND = "allauth.account.auth_backends.AuthenticationBackend"


@require_http_methods(["GET"])
def enroll_confirm(request, token):
    """Land an enrollment link: validate and render a confirm page only.

    Consuming nothing on GET keeps the single-use token safe from email
    scanners and browser link-prefetchers (#69); the human's POST is what
    spends it.
    """
    if EnrollmentToken.objects.validate(token) is None:
        return render(request, "accounts/enroll_invalid.html", status=410)
    return render(request, "accounts/enroll_confirm.html", {"token": token})


@require_http_methods(["POST"])
def enroll(request, token):
    """Consume a single-use enrollment link: log in and go register a passkey."""
    enrollment = EnrollmentToken.objects.validate(token)
    if enrollment is None or not enrollment.consume():
        return render(request, "accounts/enroll_invalid.html", status=410)
    login(request, enrollment.user, backend=ALLAUTH_BACKEND)
    # Record the authentication so the just-enrolled passkey can immediately
    # reach reauthentication-gated pages (recovery codes, adding more keys)
    # without bouncing through a reauth challenge the user can't yet satisfy.
    record_authentication(request, enrollment.user, method="enrollment")
    return redirect("accounts:passkey_add")


class EnrollPasskeyView(AddWebAuthnView):
    """Register a discoverable, user-verifying passkey on the current user.

    Inherits the add flow (which stores the credential and auto-generates
    recovery codes on the first authenticator, then redirects to view them) but
    begins registration with passwordless=True so the credential is a resident
    key with userVerification required.
    """

    template_name = "accounts/passkey_add.html"

    def get_context_data(self, **kwargs):
        # Bypass AddWebAuthnView.get_context_data (which begins a non-passwordless
        # registration) and begin a passwordless one instead.
        ret = FormView.get_context_data(self, **kwargs)
        user = self.request.user
        assert user.is_authenticated  # nosec
        ret["js_data"] = {
            "creation_options": webauthn_auth.begin_registration(
                user, passwordless=True
            )
        }
        return ret
