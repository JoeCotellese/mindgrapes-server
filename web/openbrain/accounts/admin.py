"""Admin user-management for the email-identity User (Slice 2, #64).

The stock auth UserAdmin assumes a username + password; this trims it to the
email-based, passwordless v1 model and adds the household member workflow: add a
member by email (provisioning a passwordless account + emailing a single-use
enrollment link), see passkey/last-login status, and disable/re-enable members.
"""

from allauth.mfa.models import Authenticator
from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User
from .services import (
    deliver_enrollment_link,
    disable_member,
    enable_member,
    issue_enrollment_link,
)


class MemberCreationForm(forms.ModelForm):
    """Add a member by email only — accounts are passwordless (passkey-based)."""

    class Meta:
        model = User
        fields = ("email",)


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    add_form = MemberCreationForm
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("email",)}),)
    ordering = ("email",)
    list_display = ("email", "is_active", "last_login", "has_passkey")
    search_fields = ("email",)
    actions = ["issue_link", "disable_members", "enable_members"]
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )

    @admin.display(boolean=True, description="Passkey")
    def has_passkey(self, obj):
        return Authenticator.objects.filter(
            user=obj, type=Authenticator.Type.WEBAUTHN
        ).exists()

    def save_model(self, request, obj, form, change):
        if not change:
            # Passwordless: members authenticate by passkey, never a password.
            obj.set_unusable_password()
        super().save_model(request, obj, form, change)
        if not change:
            self._hand_out_link(request, obj)

    def _hand_out_link(self, request, user):
        """Mint, email, and surface a single-use enrollment link for the member.

        The link is built against the host the admin is on so it matches the
        WebAuthn relying-party id. Email is best-effort; the copy link in the
        banner is the reliable path.
        """
        base_url = request.build_absolute_uri("/").rstrip("/")
        link = issue_enrollment_link(user, base_url)
        sent = deliver_enrollment_link(user, link)
        prefix = (
            "Enrollment link emailed; copy:"
            if sent
            else "Enrollment link (email failed) — copy:"
        )
        self.message_user(
            request,
            f"{prefix} {link}",
            level=messages.SUCCESS if sent else messages.WARNING,
        )

    @admin.action(description="Issue a new enrollment link (email + copy)")
    def issue_link(self, request, queryset):
        for user in queryset:
            self._hand_out_link(request, user)

    @admin.action(description="Disable selected members (revoke sessions)")
    def disable_members(self, request, queryset):
        for user in queryset:
            killed = disable_member(user)
            self.message_user(
                request,
                f"Disabled {user.email}; revoked {killed} session(s).",
                level=messages.SUCCESS,
            )

    @admin.action(description="Re-enable selected members")
    def enable_members(self, request, queryset):
        for user in queryset:
            enable_member(user)
            self.message_user(
                request, f"Re-enabled {user.email}.", level=messages.SUCCESS
            )
