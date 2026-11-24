import datetime

from django.conf import settings
from django.db import models
import django.utils.timezone as django_tz


class AutoUpdaterSchedule(models.Model):
    """When the ``auto_updater`` scheduler refreshes the metadata of a provider's galleries (one per provider).

    ``rules`` lists the gallery ages, counted from the gallery's posted date, at which a gallery is updated: each
    rule is ``{"from": "6h", "to": "2d", "every": "6h"}`` (an update at 6h, 12h, ... up to 2d) or a single age
    (``from`` equal to ``to``). Every ``queue_every`` the scheduler queues a ``gallery_updater`` task with the
    galleries that reached one of those ages since its previous run, so each gallery is updated once per age. See
    ``viewer.services.auto_updater``.
    """

    class RunStatus(models.TextChoices):
        OK = "ok", "OK"
        FAILED = "failed", "Failed"

    provider = models.CharField(max_length=50, unique=True)
    active = models.BooleanField(default=True, help_text="Run by the auto_updater scheduler.")
    rules = models.JSONField(
        default=list,
        blank=True,
        help_text='Gallery ages to update at: [{"from": "6h", "to": "2d", "every": "6h"}, {"from": "30d", "to": "30d"}].',
    )
    queue_every = models.DurationField(
        default=datetime.timedelta(hours=1),
        help_text="How often the galleries that reached an update age are queued, e.g. 01:00:00. It doesn't change "
        "how many times a gallery is updated, only how many galleries each task gets.",
    )
    notes = models.TextField(blank=True, default="")

    # End of the last posted date window queued: the next run takes the galleries that reached an update age after it.
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_run_status = models.CharField(max_length=10, choices=RunStatus.choices, blank=True, default="")
    last_run_message = models.TextField(blank=True, default="")
    last_run_queued = models.IntegerField(default=0, help_text="Galleries queued for update on the last run.")
    last_run_task_id = models.IntegerField(null=True, blank=True)

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(default=django_tz.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["provider"]

    def __str__(self) -> str:
        return self.provider

    def next_run_at(self) -> datetime.datetime | None:
        """When it's due again; None when it never ran (due now)."""
        if self.last_run_at is None:
            return None
        return self.last_run_at + self.queue_every
