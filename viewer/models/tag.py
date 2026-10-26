import typing
from django.db import models
from django.db.models import QuerySet
from django.urls import reverse

from viewer.models.common import SpacedSearch


type SortedTagList = list[tuple[str, list["Tag"]]]


class TagQuerySet(models.QuerySet["Tag"]):
    def are_custom(self) -> QuerySet:
        return self.filter(source="user")

    def not_custom(self) -> QuerySet:
        return self.exclude(source="user")

    def first_artist_tag(self, **kwargs: typing.Any) -> "Tag | None":
        return self.filter(scope__exact="artist", **kwargs).first()


class TagManager(models.Manager["Tag"]):
    def get_queryset(self) -> TagQuerySet:
        return TagQuerySet(self.model, using=self._db)

    def are_custom(self) -> QuerySet:
        return self.get_queryset().are_custom()

    def first_artist_tag(self, **kwargs: typing.Any) -> "Tag | None":
        return self.get_queryset().first_artist_tag(**kwargs)



class Tag(models.Model):
    name = models.CharField(max_length=200)
    scope = models.CharField(max_length=200, default="", blank=True)
    source = models.CharField("Source", max_length=50, blank=True, null=True, default="web")
    create_date = models.DateTimeField(auto_now_add=True)

    objects = TagManager()

    class Meta:
        ordering = ["-id"]
        constraints = [models.UniqueConstraint(fields=["scope", "name"], name="unique_scope_name")]

    def natural_key(self):
        return self.scope, self.name

    def __str__(self) -> str:
        if self.scope != "":
            return self.scope + ":" + self.name
        else:
            return self.name



