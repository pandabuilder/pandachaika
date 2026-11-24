from django.conf import settings
from django.db import models
import django.utils.timezone as django_tz


class ReleaseSeries(models.Model):
    """A periodical (magazine, numbered anthology...) whose issues should all be in the collection.

    The release completeness check (``viewer.services.release_series``) collects the galleries and archives that
    belong to the series, reads each one's issue from its title with ``title_regex``, compares them with the issues
    the ``schedule`` expects and reports the missing ones, which can then be searched for in the backlog sources.
    """

    class KeyType(models.TextChoices):
        YEAR_MONTH = "year_month", "Year and month (2026-12)"
        YEAR_NUMBER = "year_number", "Year and issue number (2026 No.42)"
        NUMBER = "number", "Issue number (#141)"

    name = models.CharField(max_length=200, unique=True)
    key_type = models.CharField(max_length=20, choices=KeyType.choices, default=KeyType.YEAR_MONTH)
    title_contains = models.TextField(
        blank=True,
        default="",
        help_text="Galleries and archives whose title contains any of these texts (one per line, case-insensitive) "
        "are candidates.",
    )
    title_excludes = models.TextField(
        blank=True,
        default="",
        help_text="Titles containing any of these texts (one per line, case-insensitive) are skipped, e.g. a spin-off "
        "magazine with a longer name.",
    )
    title_regex = models.TextField(
        blank=True,
        default="",
        help_text="Regular expressions (one per line, case-insensitive) that read the issue from a title, with named "
        "groups: year and month, year and number, or number. Optional month_end / number_end groups cover combined "
        "issues.",
    )
    tags = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text="Candidates must have at least one of these tags (comma separated, scope:name or name).",
    )
    allowed_languages = models.CharField(
        max_length=200,
        blank=True,
        default="japanese",
        help_text="Candidates whose language tags aren't all in this list are skipped (comma separated). A candidate "
        "without language tags is always accepted. Empty: no language filter.",
    )
    schedule = models.TextField(
        blank=True,
        default="",
        help_text="Release periods, one per line: '<first issue> <rule>'. Empty: every issue from the first one found.",
    )
    lead_days = models.IntegerField(
        null=True,
        blank=True,
        help_text="Days before its cover month an issue is released (year and month series). Estimates when a "
        "missing issue came out, and keeps issues not released yet from counting as missing.",
    )
    backlog_query = models.TextField(
        blank=True,
        default="",
        help_text="Search sent to the backlog sources that are es-gallery-json endpoints for a missing issue. "
        "Placeholders: {title} (the title searched: the name and the issue, unless changed in the backlog search "
        "dialog), {name}, {key}, {year}, {month}, {number}, e.g. {month:02d}.",
    )
    backlog_page_query = models.TextField(
        blank=True,
        default="",
        help_text="Search sent to the backlog sources that are provider search pages (panda) for a missing issue. "
        "Same placeholders.",
    )
    active = models.BooleanField(default=True, help_text="Checked on the overview page.")
    notes = models.TextField(blank=True, default="")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(default=django_tz.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "release series"

    def __str__(self) -> str:
        return self.name


class ReleaseSeriesDismissal(models.Model):
    """An expected issue that isn't missing: it was never released, or it came out as part of a combined issue."""

    class Reason(models.TextChoices):
        NOT_RELEASED = "not_released", "Not released"
        COMBINED = "combined", "Combined issue"
        NOT_WANTED = "not_wanted", "Not wanted"
        OTHER = "other", "Other"

    series = models.ForeignKey(ReleaseSeries, on_delete=models.CASCADE, related_name="dismissals")
    key = models.CharField(max_length=50, help_text="Issue key, as shown on the page (2024-05, 2024 No.11, #109).")
    reason = models.CharField(max_length=20, choices=Reason.choices, default=Reason.NOT_RELEASED)
    comment = models.CharField(max_length=500, blank=True, default="")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(default=django_tz.now)

    class Meta:
        ordering = ["series", "key"]
        constraints = [models.UniqueConstraint(fields=["series", "key"], name="unique_release_series_dismissal")]

    def __str__(self) -> str:
        return f"{self.series.name} {self.key}: {self.get_reason_display()}"
