import datetime

from django.conf import settings
from django.db import models
import django.utils.timezone as django_tz


class AutoWantedSource(models.Model):
    """A provider query that creates wanted galleries for the new releases it lists (auto wanted).

    ``provider`` is the slug of a provider with a wanted generator (``core/providers/<provider>/wanted.py``), and
    ``params`` its query parameters, as declared by that provider's ``WANTED_PARAMS``. The other fields are the
    values given to the wanted galleries it creates. The ``auto_wanted`` scheduler runs the active sources; see
    ``viewer.services.auto_wanted``.
    """

    class RunStatus(models.TextChoices):
        OK = "ok", "OK"
        ERRORS = "errors", "Finished with errors"
        FAILED = "failed", "Failed"

    name = models.CharField(max_length=200, unique=True)
    provider = models.CharField(max_length=50, help_text="Provider whose wanted generator runs this query.")
    params = models.JSONField(default=dict, blank=True, help_text="Query parameters, specific to the provider.")
    active = models.BooleanField(default=True, help_text="Run by the auto_wanted scheduler.")
    frequency = models.DurationField(
        null=True,
        blank=True,
        help_text="How often the scheduler runs it, e.g. 06:00:00 for 6h, 1 00:00:00 for 1d. Empty: every "
        "auto_wanted.cycle_timer hours.",
    )
    force_process = models.BooleanField(
        default=False,
        help_text="Process every link listed, even the ones already processed before (they're normally skipped, and a "
        "page with only those stops the query).",
    )

    # Values for the wanted galleries created.
    reason = models.CharField(max_length=200, blank=True, default="", help_text="Reason of the wanted galleries.")
    public = models.BooleanField(default=False)
    should_search = models.BooleanField(default=False, help_text="Search the wanted galleries in the providers.")
    keep_searching = models.BooleanField(default=False, help_text="Keep searching after a gallery is found.")
    notify_when_found = models.BooleanField(default=False)
    wanted_providers = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text="Providers the wanted galleries are searched in (slugs, comma or space separated). Empty: any.",
    )
    unwanted_title = models.CharField(
        max_length=500, blank=True, default="", help_text="Unwanted title of the wanted galleries, e.g. [Chinese]."
    )
    regexp_unwanted_title = models.BooleanField(default=False, help_text="The unwanted title is a regular expression.")
    regexp_unwanted_title_icase = models.BooleanField(
        default=False, help_text="The unwanted title regular expression is case-insensitive."
    )

    notes = models.TextField(blank=True, default="")

    last_run_at = models.DateTimeField(null=True, blank=True)
    last_run_status = models.CharField(max_length=10, choices=RunStatus.choices, blank=True, default="")
    last_run_message = models.TextField(blank=True, default="")
    last_run_links = models.IntegerField(default=0, help_text="New links processed on the last run.")
    last_run_created = models.IntegerField(default=0, help_text="Wanted galleries created on the last run.")
    last_run_mentions = models.IntegerField(default=0, help_text="Mentions added on the last run.")

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(default=django_tz.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["provider", "name"]

    def __str__(self) -> str:
        return self.name

    def period(self, default: datetime.timedelta) -> datetime.timedelta:
        """How often it runs: its frequency, or the scheduler's default."""
        return self.frequency or default

    def next_run_at(self, default: datetime.timedelta) -> datetime.datetime | None:
        """When it's due again; None when it never ran (due now)."""
        if self.last_run_at is None:
            return None
        return self.last_run_at + self.period(default)

    def wanted_provider_slugs(self) -> list[str]:
        return [slug for slug in self.wanted_providers.replace(",", " ").split() if slug]
