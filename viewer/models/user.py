import typing
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.contrib.auth.models import User
from django.core.validators import MaxValueValidator, MinValueValidator
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


def default_report_time() -> dt_time:
    return dt_time(8, 0)


def default_report_time_zone() -> str:
    return settings.TIME_ZONE or "UTC"


class AdminReportSubscription(models.Model):
    """A staff user's own schedule for receiving the admin report by email.

    ``next_send_at`` is when the next report is due; the admin report scheduler sends due reports and moves it forward.
    Each report covers the time since ``last_sent_at`` (the end of the last report sent), so nothing is missed when a
    send fails or is skipped.
    """

    class Frequency(models.TextChoices):
        HOURLY = "hourly", "Every N hours"
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"

    WEEKDAY_CHOICES = (
        (0, "Monday"),
        (1, "Tuesday"),
        (2, "Wednesday"),
        (3, "Thursday"),
        (4, "Friday"),
        (5, "Saturday"),
        (6, "Sunday"),
    )

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="admin_report_subscription")
    enabled = models.BooleanField(default=False)
    frequency = models.CharField(max_length=10, choices=Frequency.choices, default=Frequency.DAILY)
    interval_hours = models.PositiveSmallIntegerField(
        default=6,
        validators=[MinValueValidator(1), MaxValueValidator(168)],
        help_text="Hours between reports, for the 'Every N hours' frequency.",
    )
    send_time = models.TimeField(default=default_report_time, help_text="Time of day for daily and weekly reports.")
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAY_CHOICES, default=0, help_text="Day for weekly reports.")
    time_zone = models.CharField(
        max_length=64, default=default_report_time_zone, help_text="Time zone of the send time (e.g. Europe/Madrid)."
    )
    # Report sections to include. Empty means all of them.
    sections = models.JSONField(default=list, blank=True)
    min_log_level = models.PositiveSmallIntegerField(default=30, help_text="Minimum level of the server logs included.")
    top_n = models.PositiveSmallIntegerField(
        default=10, validators=[MinValueValidator(1), MaxValueValidator(100)], help_text="Size of the lists."
    )
    skip_if_quiet = models.BooleanField(
        default=False,
        help_text="Don't send the email when nothing needs attention. The next report then covers both periods.",
    )
    next_send_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_result = models.CharField(max_length=500, blank=True, default="")
    create_date = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Admin report for {self.user} ({self.get_frequency_display()})"

    @property
    def zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.time_zone)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    @property
    def period(self) -> timedelta:
        """Nominal time between two reports."""
        if self.frequency == self.Frequency.HOURLY:
            return timedelta(hours=self.interval_hours)
        if self.frequency == self.Frequency.WEEKLY:
            return timedelta(days=7)
        return timedelta(days=1)

    def compute_next_send(self, after: datetime) -> datetime:
        """First scheduled send time strictly after ``after``."""
        if self.frequency == self.Frequency.HOURLY:
            return after + timedelta(hours=self.interval_hours)

        zone = self.zone
        local_after = after.astimezone(zone)
        day = local_after.date()
        for _ in range(9):
            candidate = datetime.combine(day, self.send_time, tzinfo=zone)
            if candidate > after and (self.frequency != self.Frequency.WEEKLY or day.weekday() == self.weekday):
                return candidate
            day += timedelta(days=1)
        raise AssertionError("A daily or weekly schedule always has a send time within 8 days.")

    def advance_schedule(self, now: datetime) -> datetime:
        """Next send time after a run at ``now``, keeping the schedule's rhythm (no drift for hourly schedules)."""
        if self.frequency == self.Frequency.HOURLY and self.next_send_at is not None:
            next_send = self.next_send_at
            while next_send <= now:
                next_send += timedelta(hours=self.interval_hours)
            return next_send
        return self.compute_next_send(now)

    def report_window(self, now: datetime, max_window: timedelta) -> tuple[datetime, datetime]:
        """Window for a report sent at ``now``: since the last report, or one period back for the first one."""
        since = self.last_sent_at or (now - self.period)
        return max(since, now - max_window), now


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


