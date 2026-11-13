import typing
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.db.models import Q
from django.db.models.signals import post_save
from django.dispatch import receiver
import django.utils.timezone as django_tz
from django.utils.crypto import get_random_string, salted_hmac


class UserArchivePrefs(models.Model):

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "archive"], name="unique_user_archive")]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    archive = models.ForeignKey("viewer.Archive", on_delete=models.CASCADE)
    favorite_group = models.IntegerField("Favorite Group", default=1)



class Profile(models.Model):

    class Meta:
        permissions = (
            ("read_private_stats", "Can check the general statistics about the backup"),
            ("use_remote_api", "Can use the remote admin API"),
        )

    user = models.OneToOneField(User, on_delete=models.CASCADE)
    bio = models.TextField(max_length=500, blank=True, default="")
    notify_new_submissions = models.BooleanField(default=False, blank=True)
    notify_new_private_archive = models.BooleanField(default=False, blank=True)
    notify_wanted_gallery_found = models.BooleanField(default=False, blank=True)


def in_10_years() -> datetime:
    return django_tz.now() + timedelta(days=3650)


def default_token_scopes() -> list[str]:
    """Scopes granted to a token when none are specified: all of them (matches the pre-scope behaviour)."""
    return list(UserLongLivedToken.ALL_SCOPES)


class UserLongLivedToken(models.Model):

    VALID_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_"
    TOKEN_LENGTH = 50

    # A token can only do what its scopes allow, and never more than its user is allowed to do.
    SCOPE_READ = "read"
    SCOPE_WRITE = "write"
    SCOPE_REMOTE = "remote"
    SCOPE_CHOICES = (
        (SCOPE_READ, "Read: view archives, images and galleries, including private ones"),
        (SCOPE_WRITE, "Write: modify data (manage archives, extract/reduce, JSON API POST/PUT/DELETE)"),
        (SCOPE_REMOTE, "Remote: submit links and queue downloads through the remote admin API"),
    )
    ALL_SCOPES = tuple(scope for scope, _ in SCOPE_CHOICES)

    # Minimum seconds between two `last_used_date` writes for the same token, to avoid a write on every request.
    LAST_USED_RESOLUTION_SECONDS = 60

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "name"], name="unique_user_token_name")]
        indexes = [
            models.Index(fields=["name"]),
            models.Index(fields=["key"]),
        ]

    @staticmethod
    def create_salted_key_from_key(secret_key):
        salted_key = salted_hmac(
            "panda.backup.py",
            secret_key,
            secret=settings.SECRET_KEY,
            algorithm="sha256",
        ).hexdigest()

        return salted_key

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="long_lived_tokens")
    name = models.CharField(max_length=100)
    key = models.CharField(max_length=256, null=False, blank=False, unique=True)
    # NULL means the token never expires.
    expire_date = models.DateTimeField(null=True, blank=True, default=in_10_years)
    create_date = models.DateTimeField(auto_now_add=True)
    scopes = models.JSONField(default=default_token_scopes, blank=True)
    last_used_date = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.name} ({self.user})"

    @classmethod
    def create_for_user(
        cls,
        user: User,
        name: str,
        expire_date: datetime | None = None,
        scopes: typing.Iterable[str] | None = None,
    ) -> tuple["UserLongLivedToken", str]:
        """Create a token and return it with the plain secret, which is not stored and cannot be recovered later.

        `expire_date=None` creates a token that never expires; `scopes=None` grants all scopes.
        """
        secret_key = get_random_string(cls.TOKEN_LENGTH, cls.VALID_CHARS)
        token = cls.objects.create(
            user=user,
            name=name,
            key=cls.create_salted_key_from_key(secret_key),
            expire_date=expire_date,
            scopes=list(cls.ALL_SCOPES if scopes is None else scopes),
        )
        return token, secret_key

    @classmethod
    def active(cls) -> "models.QuerySet[UserLongLivedToken]":
        """Tokens that have not expired (no expiry date counts as not expired)."""
        return cls.objects.filter(Q(expire_date__isnull=True) | Q(expire_date__gt=django_tz.now()))

    @property
    def never_expires(self) -> bool:
        return self.expire_date is None

    @property
    def is_expired(self) -> bool:
        return self.expire_date is not None and self.expire_date <= django_tz.now()

    def has_scope(self, scope: str) -> bool:
        return scope in (self.scopes or [])

    def mark_used(self) -> None:
        """Record usage at most once per LAST_USED_RESOLUTION_SECONDS, in a single conditional UPDATE."""
        now = django_tz.now()
        stale_before = now - timedelta(seconds=self.LAST_USED_RESOLUTION_SECONDS)
        type(self).objects.filter(pk=self.pk).filter(
            Q(last_used_date__isnull=True) | Q(last_used_date__lt=stale_before)
        ).update(last_used_date=now)


@receiver(post_save, sender=User)
def create_profile(sender, instance, created, **kwargs):
    if created:
        Profile.objects.create(user=instance)


def users_with_perm(app: str, perm_name: str, *args: typing.Any, **kwargs: typing.Any):
    return (
        User.objects.filter(
            Q(is_superuser=True)
            | Q(user_permissions__codename=perm_name, user_permissions__content_type__app_label=app)
            | Q(groups__permissions__codename=perm_name, groups__permissions__content_type__app_label=app)
        )
        .filter(is_active=True)
        .filter(*args, **kwargs)
        .distinct()
    )


