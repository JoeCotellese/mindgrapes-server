"""User accounts for the Mind Grapes product layer.

Email is the identifier; there is no username. v1 auth is passwordless
passkeys (Slice 1, #63), so users are created without a usable password.
"""

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    """Email-based manager. v1 users are passwordless (no usable password)."""

    use_in_migrations = True

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self.create_user(email, password, **extra_fields)


class User(AbstractUser):
    """A household member. Authenticates by passkey (Slice 1); email identity."""

    username = None
    email = models.EmailField(unique=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    def __str__(self):
        return self.email


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class EnrollmentTokenManager(models.Manager):
    def create_for(self, user):
        """Mint a single-use enrollment token. Returns (instance, raw_token).

        The raw token is returned to the caller exactly once for embedding in
        the enrollment URL; only its hash is persisted.
        """
        raw = secrets.token_urlsafe(EnrollmentToken.TOKEN_BYTES)
        token = self.create(
            user=user,
            token_hash=_hash_token(raw),
            expires_at=timezone.now() + EnrollmentToken.ttl(),
        )
        return token, raw

    def validate(self, raw: str):
        """Return the valid token matching raw, or None if unknown/expired/used."""
        token = self.filter(token_hash=_hash_token(raw)).first()
        if token is None or not token.is_valid():
            return None
        return token


class EnrollmentToken(models.Model):
    """A single-use, expiring link that bootstraps first-passkey enrollment.

    Issued by an admin (or the bootstrap command) and consumed when the user
    opens the link: it logs them in just long enough to register a passkey.
    """

    TOKEN_BYTES = 32
    DEFAULT_TTL = timedelta(hours=72)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="enrollment_tokens",
    )
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    objects = EnrollmentTokenManager()

    def __str__(self):
        return f"enrollment token for {self.user} (expires {self.expires_at:%Y-%m-%d})"

    @classmethod
    def ttl(cls) -> timedelta:
        return getattr(settings, "ENROLLMENT_TOKEN_TTL", cls.DEFAULT_TTL)

    def is_valid(self) -> bool:
        return self.used_at is None and self.expires_at > timezone.now()

    def consume(self) -> bool:
        """Stamp the token used. Returns True if this call consumed it.

        The update is conditional on used_at being null so concurrent clicks of
        the same link can never both succeed.
        """
        now = timezone.now()
        updated = EnrollmentToken.objects.filter(
            pk=self.pk, used_at__isnull=True
        ).update(used_at=now)
        if updated:
            self.used_at = now
        return bool(updated)
