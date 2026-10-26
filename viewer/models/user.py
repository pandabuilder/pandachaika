import typing
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.db.models import Q
from django.db.models.signals import post_save
from django.dispatch import receiver
import django.utils.timezone as django_tz
from django.utils.crypto import salted_hmac


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


class UserLongLivedToken(models.Model):

    VALID_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_"
    TOKEN_LENGTH = 50

    class Meta:
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
    name = models.CharField(max_length=100, unique=True)
    key = models.CharField(max_length=256, null=False, blank=False, unique=True)
    expire_date = models.DateTimeField(null=False, blank=False, default=in_10_years)
    create_date = models.DateTimeField(auto_now_add=True)


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


