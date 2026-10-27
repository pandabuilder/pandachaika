import itertools
import json
import logging
import os
import re
import shutil
import subprocess
import time
import typing
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from os.path import basename, join as pjoin
from tempfile import NamedTemporaryFile
from urllib.parse import urlparse

import elasticsearch
from PIL import Image as PImage
from PIL import ImageFile
import requests
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.files import File
from django.db import models, transaction
from django.db.models import CharField, Count, F, Q, QuerySet, Value
from django.db.models.functions import Concat, Replace
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.templatetags.static import static
from django.urls import reverse
import django.utils.timezone as django_tz
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from core.base.image_ops import img_to_thumbnail
from core.base.types import DataDict, GalleryData
from core.base.utilities import available_filename, clean_title, get_dict_allowed_fields, request_with_retries
from viewer.models.common import EMPTY_TITLE, ItemProperties, SpacedSearch, fs
from viewer.models.tag import Tag
from viewer.services import CompareObjectsService
from viewer.utils.elasticsearch import add_gallery_data_to_match_index, remove_gallery_from_match_index
from viewer.utils.tags import sort_tags, sort_tags_str

if typing.TYPE_CHECKING:
    from viewer.models.archive import Archive
    from viewer.models.wanted import FoundGallery, GalleryMatch, WantedGallery

logger = logging.getLogger(__name__)

type FetchTypes = str | float | int | datetime | timedelta | bool | None


def gallery_thumb_path_handler(instance: "Gallery", filename: str) -> str:
    return "images/gallery_thumbs/{id}/{file}".format(id=instance.id, file=filename)



class GalleryQuerySet(models.QuerySet["Gallery"]):
    def filter(self, *args: typing.Any, **kwargs: typing.Any) -> typing.Self:
        return super().filter(*args, **kwargs)

    def exclude(self, *args: typing.Any, **kwargs: typing.Any) -> typing.Self:
        return super().exclude(*args, **kwargs)

    def order_by(self, *field_names: typing.Any) -> typing.Self:
        return super().order_by(*field_names)

    def prefetch_related(self, *lookups: typing.Any) -> typing.Self:
        return super().prefetch_related(*lookups)

    def select_related(self, *fields: typing.Any) -> typing.Self:
        return super().select_related(*fields)

    def defer(self, *fields: typing.Any) -> typing.Self:
        return super().defer(*fields)

    def only(self, *fields: typing.Any) -> typing.Self:
        return super().only(*fields)

    def annotate(self, *args: typing.Any, **kwargs: typing.Any) -> typing.Self:
        return super().annotate(*args, **kwargs)

    def several_archives(self) -> typing.Self:
        return (
            self.annotate(num_archives=Count("archive"))
            .filter(num_archives__gt=1)
            .order_by("-id")
            .prefetch_related("archive_set")
        )

    def different_filesize_archive(self, **kwargs: typing.Any) -> typing.Self:
        return (
            self.filter(
                ~Q(filesize=F("archive__filesize")),
                ~Q(filesize=0),
                Q(archive__isnull=False),
                **kwargs,
            )
            .prefetch_related("archive_set")
            .order_by("provider", "-create_date")
        )

    def non_used_galleries(self, **kwargs: typing.Any) -> typing.Self:
        return self.filter(
            Q(status=Gallery.StatusChoices.NORMAL),
            ~Q(dl_type__contains="skipped"),
            Q(archive__isnull=True),
            Q(gallery_container__archive__isnull=True),
            Q(magazine__archive__isnull=True),
            Q(alternative_sources__isnull=True),
            **kwargs,
        ).order_by("-create_date")

    def not_used_including_groups(self, **kwargs: typing.Any) -> typing.Self:
        return (
            self.filter(
                Q(
                    Q(status=Gallery.StatusChoices.NORMAL),
                    ~Q(dl_type__contains="skipped"),
                    Q(archive__isnull=True),
                    Q(gallery_container__archive__isnull=True),
                    Q(magazine__archive__isnull=True),
                    Q(alternative_sources__isnull=True),
                ),
                **kwargs,
            )
            .annotate(
                gallery_groups_all_not_used=Count(
                    "gallerymatchgroupentry__gallery_match_group__galleries",
                    filter=Q(
                        Q(gallerymatchgroupentry__gallery_match_group__galleries__status=Gallery.StatusChoices.NORMAL),
                        ~Q(gallerymatchgroupentry__gallery_match_group__galleries__dl_type__contains="skipped"),
                        Q(gallerymatchgroupentry__gallery_match_group__galleries__archive__isnull=True),
                        Q(
                            gallerymatchgroupentry__gallery_match_group__galleries__gallery_container__archive__isnull=True
                        ),
                        Q(gallerymatchgroupentry__gallery_match_group__galleries__magazine__archive__isnull=True),
                        Q(gallerymatchgroupentry__gallery_match_group__galleries__alternative_sources__isnull=True),
                    )
                )
            )
            .filter(
                gallery_groups_all_not_used=Count(
                    "gallerymatchgroupentry__gallery_match_group__galleries"
                )
            )
            .order_by("-create_date")
        )

    def only_used_galleries(self, **kwargs: typing.Any) -> typing.Self:
        return self.filter(
            Q(status=Gallery.StatusChoices.NORMAL),
            Q(
                Q(archive__isnull=False)
                | Q(gallery_container__archive__isnull=False)
                | Q(magazine__archive__isnull=False)
                | Q(alternative_sources__isnull=False)
            ),
            **kwargs,
        ).order_by("-create_date")

    def report_as_missing_galleries(self, **kwargs: typing.Any) -> typing.Self:
        return (
            self.annotate(number_of_archives=Count("archive"))
            .filter(
                Q(
                    Q(status=Gallery.StatusChoices.NORMAL),
                    ~Q(dl_type__contains="skipped"),
                    Q(number_of_archives=0),
                    Q(gallery_container__archive__isnull=True),
                    Q(magazine__archive__isnull=True),
                    Q(alternative_sources__isnull=True),
                )
                | Q(
                    Q(status=Gallery.StatusChoices.NORMAL),
                    # Hardcode fakku restriction, older galleries had a reported filesize that was wrong.
                    (~Q(filesize=F("archive__filesize")) & ~Q(provider="fakku")),
                    Q(number_of_archives=1),
                    ~Q(filesize=0),
                    Q(archive__isnull=False),
                ),
                **kwargs,
            )
            .order_by("-create_date")
        )

    def submitted_galleries(self, *args: typing.Any, **kwargs: typing.Any) -> typing.Self:
        return self.filter(
            Q(origin=Gallery.OriginChoices.ORIGIN_SUBMITTED),
            ~Q(status=Gallery.StatusChoices.DELETED),
            Q(archive__isnull=True),
            Q(gallery_container__archive__isnull=True),
            Q(magazine__archive__isnull=True),
            *args,
            **kwargs,
        ).order_by("-create_date")

    def eligible_for_use(self, **kwargs: typing.Any) -> typing.Self:
        return self.filter(Q(status=Gallery.StatusChoices.NORMAL), **kwargs)


class GalleryManager(models.Manager["Gallery"]):
    def get_queryset(self) -> GalleryQuerySet:
        return GalleryQuerySet(self.model, using=self._db)

    def all(self) -> GalleryQuerySet:
        return self.get_queryset()

    def filter(self, *args: typing.Any, **kwargs: typing.Any) -> GalleryQuerySet:
        return self.get_queryset().filter(*args, **kwargs)

    def exclude(self, *args: typing.Any, **kwargs: typing.Any) -> GalleryQuerySet:
        return self.get_queryset().exclude(*args, **kwargs)

    def order_by(self, *field_names: typing.Any) -> GalleryQuerySet:
        return self.get_queryset().order_by(*field_names)

    def exists_by_gid_provider(self, gid: str, provider: str) -> bool:
        return bool(self.filter(gid=gid, provider=provider))

    def filter_order_created(self, **kwargs: typing.Any) -> QuerySet:
        return self.filter(**kwargs).order_by("-create_date")

    def filter_order_modified(self, **kwargs: typing.Any) -> QuerySet:
        return self.filter(**kwargs).order_by("-last_modified")

    def after_posted_date(self, date: datetime) -> QuerySet:
        return self.filter(posted__gte=date).order_by("posted")

    def different_filesize_archive(self, **kwargs: typing.Any) -> GalleryQuerySet:
        return self.get_queryset().different_filesize_archive(**kwargs)

    def several_archives(self) -> GalleryQuerySet:
        return self.get_queryset().several_archives()

    def non_used_galleries(self, **kwargs: typing.Any) -> GalleryQuerySet:
        return self.get_queryset().non_used_galleries(**kwargs)

    def not_used_including_groups(self, **kwargs: typing.Any) -> GalleryQuerySet:
        return self.get_queryset().not_used_including_groups(**kwargs)

    def only_used_galleries(self, **kwargs: typing.Any) -> GalleryQuerySet:
        return self.get_queryset().only_used_galleries(**kwargs)

    def report_as_missing_galleries(self, **kwargs: typing.Any) -> GalleryQuerySet:
        return self.get_queryset().report_as_missing_galleries(**kwargs)

    def submitted_galleries(self, *args: typing.Any, **kwargs: typing.Any) -> GalleryQuerySet:
        return self.get_queryset().submitted_galleries(*args, **kwargs)

    def eligible_for_use(self, **kwargs: typing.Any) -> GalleryQuerySet:
        return self.get_queryset().eligible_for_use(**kwargs)

    def filter_dl_type(self, dl_type_filter: str, **kwargs: typing.Any) -> QuerySet:
        return self.filter(dl_type__icontains=dl_type_filter, **kwargs)

    def filter_dl_type_and_posted_dates(
        self, dl_type_filter: str, start_date: datetime, end_date: datetime
    ) -> QuerySet:
        return self.filter(posted__gte=start_date, posted__lte=end_date, dl_type__icontains=dl_type_filter)

    def filter_first(self, **kwargs: typing.Any) -> "Gallery | None":
        return self.filter(**kwargs).first()

    def update_by_gid_provider(self, gallery_data: GalleryData) -> bool:
        gallery = self.filter(gid=gallery_data.gid, provider=gallery_data.provider).first()
        if gallery:
            with transaction.atomic():
                if gallery_data.tags is not None:
                    new_tags = []
                    for tag in gallery_data.tags:
                        if tag == "":
                            continue
                        scope_name = tag.split(":", maxsplit=1)
                        if len(scope_name) > 1:
                            tag_object, _ = Tag.objects.get_or_create(scope=scope_name[0], name=scope_name[1])
                        else:
                            scope = ""
                            tag_object, _ = Tag.objects.get_or_create(name=tag, scope=scope)

                        new_tags.append(tag_object)
                    gallery.tags.set(new_tags)
                values = get_dict_allowed_fields(gallery_data)
                if "gallery_container_gid" in values:
                    gallery_container = Gallery.objects.filter(
                        gid=values["gallery_container_gid"], provider=gallery_data.provider
                    ).first()
                    if gallery_container:
                        values["gallery_container"] = gallery_container
                    del values["gallery_container_gid"]
                if "magazine_gid" in values:
                    magazine = Gallery.objects.filter(
                        gid=values["magazine_gid"], provider=gallery_data.provider
                    ).first()
                    if magazine:
                        values["magazine"] = magazine
                    del values["magazine_gid"]
                if "parent_gallery_gid" in values:
                    gallery_parent = Gallery.objects.filter(
                        gid=values["parent_gallery_gid"], provider=gallery_data.provider
                    ).first()
                    if gallery_parent:
                        values["parent_gallery"] = gallery_parent
                    del values["parent_gallery_gid"]
                if "first_gallery_gid" in values:
                    gallery_first = Gallery.objects.filter(
                        gid=values["first_gallery_gid"], provider=gallery_data.provider
                    ).first()
                    if gallery_first:
                        values["first_gallery"] = gallery_first
                    del values["first_gallery_gid"]
                for key, value in values.items():
                    setattr(gallery, key, value)
                gallery.save()

                if gallery_data.extra_provider_data:
                    for gallery_provider_data in gallery_data.extra_provider_data:
                        data_name, data_type, data_value = gallery_provider_data
                        obj, _ = GalleryProviderData.objects.update_or_create(
                            gallery=gallery, name=data_name, defaults={"data_type": data_type}
                        )
                        obj.data_type = data_type
                        obj.value = data_value
                        obj.save()

                if gallery_data.magazine_chapters_gids:
                    chapters = Gallery.objects.filter(
                        gid__in=gallery_data.magazine_chapters_gids, provider=gallery_data.provider
                    )
                    chapters.update(magazine=gallery.pk)

                if gallery_data.gallery_contains_gids:
                    contained = Gallery.objects.filter(
                        gid__in=gallery_data.gallery_contains_gids, provider=gallery_data.provider
                    )
                    contained.update(gallery_container=gallery.pk)

                return True
        else:
            return False

    @staticmethod
    def add_from_values(gallery_data: GalleryData) -> "Gallery":
        tags = gallery_data.tags

        values = get_dict_allowed_fields(gallery_data)
        if "gallery_container_gid" in values:
            gallery_container = Gallery.objects.filter(
                gid=values["gallery_container_gid"], provider=values["provider"]
            ).first()
            if gallery_container:
                values["gallery_container"] = gallery_container
            del values["gallery_container_gid"]
        if "magazine_gid" in values:
            magazine = Gallery.objects.filter(gid=values["magazine_gid"], provider=values["provider"]).first()
            if magazine:
                values["magazine"] = magazine
            del values["magazine_gid"]
        if "parent_gallery_gid" in values:
            gallery_parent = Gallery.objects.filter(
                gid=values["parent_gallery_gid"], provider=values["provider"]
            ).first()
            if gallery_parent:
                values["parent_gallery"] = gallery_parent
            del values["parent_gallery_gid"]
        if "first_gallery_gid" in values:
            gallery_first = Gallery.objects.filter(gid=values["first_gallery_gid"], provider=values["provider"]).first()
            if gallery_first:
                values["first_gallery"] = gallery_first
            del values["first_gallery_gid"]

        with transaction.atomic():
            gallery = Gallery(**values)
            gallery.save()

            if gallery_data.extra_provider_data:
                for gallery_provider_data in gallery_data.extra_provider_data:
                    data_name, data_type, data_value = gallery_provider_data
                    obj, _ = GalleryProviderData.objects.update_or_create(
                        gallery=gallery, name=data_name, defaults={"data_type": data_type}
                    )
                    obj.data_type = data_type
                    obj.value = data_value
                    obj.save()

            if gallery_data.magazine_chapters_gids:
                chapters = Gallery.objects.filter(
                    gid__in=gallery_data.magazine_chapters_gids, provider=values["provider"]
                )
                chapters.update(magazine=gallery.pk)

            if gallery_data.gallery_contains_gids:
                contained = Gallery.objects.filter(
                    gid__in=gallery_data.gallery_contains_gids, provider=values["provider"]
                )
                contained.update(gallery_container=gallery.pk)

            if tags:
                new_tags = []
                for tag in tags:
                    if tag == "":
                        continue
                    scope_name = tag.split(":", maxsplit=1)
                    if len(scope_name) > 1:
                        tag_object, _ = Tag.objects.get_or_create(scope=scope_name[0], name=scope_name[1])
                    else:
                        scope = ""
                        tag_object, _ = Tag.objects.get_or_create(name=tag, scope=scope)

                    new_tags.append(tag_object)

                gallery.tags.set(new_tags)

        return gallery

    def update_or_create_from_values(self, gallery_data: GalleryData) -> "Gallery":
        tags = gallery_data.tags

        values = get_dict_allowed_fields(gallery_data)
        if "gallery_container_gid" in values:
            gallery_container = Gallery.objects.filter(
                gid=values["gallery_container_gid"], provider=values["provider"]
            ).first()
            if gallery_container:
                values["gallery_container"] = gallery_container
            del values["gallery_container_gid"]

        if "magazine_gid" in values:
            magazine = Gallery.objects.filter(gid=values["magazine_gid"], provider=values["provider"]).first()
            if magazine:
                values["magazine"] = magazine
            del values["magazine_gid"]

        if "parent_gallery_gid" in values:
            gallery_parent = Gallery.objects.filter(
                gid=values["parent_gallery_gid"], provider=values["provider"]
            ).first()
            if gallery_parent:
                values["parent_gallery"] = gallery_parent
            del values["parent_gallery_gid"]
        if "first_gallery_gid" in values:
            gallery_first = Gallery.objects.filter(gid=values["first_gallery_gid"], provider=values["provider"]).first()
            if gallery_first:
                values["first_gallery"] = gallery_first
            del values["first_gallery_gid"]

        with transaction.atomic():
            gallery, _ = self.update_or_create(defaults=values, gid=values["gid"], provider=values["provider"])

            if gallery_data.extra_provider_data:
                for gallery_provider_data in gallery_data.extra_provider_data:
                    data_name, data_type, data_value = gallery_provider_data
                    obj, _ = GalleryProviderData.objects.update_or_create(
                        gallery=gallery, name=data_name, defaults={"data_type": data_type}
                    )
                    obj.data_type = data_type
                    obj.value = data_value
                    obj.save()

            if gallery_data.magazine_chapters_gids:
                chapters = Gallery.objects.filter(
                    gid__in=gallery_data.magazine_chapters_gids, provider=values["provider"]
                )
                chapters.update(magazine=gallery.pk)

            if gallery_data.gallery_contains_gids:
                contained = Gallery.objects.filter(
                    gid__in=gallery_data.gallery_contains_gids, provider=values["provider"]
                )
                contained.update(gallery_container=gallery.pk)

            if tags:
                new_tags = []

                for tag in tags:
                    if tag == "":
                        continue
                    scope_name = tag.split(":", maxsplit=1)
                    if len(scope_name) > 1:
                        tag_object, _ = Tag.objects.get_or_create(scope=scope_name[0], name=scope_name[1])
                    else:
                        scope = ""
                        tag_object, _ = Tag.objects.get_or_create(name=tag, scope=scope)

                    new_tags.append(tag_object)

                gallery.tags.set(new_tags)

        return gallery

    # This method is mainly used to update own fields, no related fields need to be checked
    def update_by_dl_type(self, values: DataDict, gallery_id: str, dl_type: str) -> "Gallery | None":

        instance = self.filter(id=gallery_id, dl_type__contains=dl_type).first()
        if instance:
            for key, value in values.items():
                setattr(instance, key, value)
            instance.save()
            return instance
        else:
            return None



class Gallery(models.Model):

    class StatusChoices(models.IntegerChoices):
        NORMAL = 1, _("Normal")
        # Denied status is intended for submitted galleries not accepted by a moderator. Different from deleted.
        # To remove denied galleries from other lists, an admin should mark as DELETED.
        DENIED = 4, _("Denied")
        # The deleted status hides the gallery from some user facing interfaces, as match galleries, gallery list, etc.
        # And makes that trying to parse it again results in it being skipped.
        DELETED = 5, _("Deleted")
        # This status is meant for galleries that are only kept as a way to mark already seen galleries, that don't live
        # With metadata
        NO_METADATA = 6, _("No metadata")

    class OriginChoices(models.IntegerChoices):
        ORIGIN_NORMAL = 1, _("Normal")
        ORIGIN_SUBMITTED = 2, _("Submitted")

    gid = models.CharField(max_length=200)
    token = models.CharField(max_length=50, blank=True, null=True)
    title = models.CharField(max_length=500, blank=True, null=True, default="")
    title_jpn = models.CharField(max_length=500, blank=True, null=True, default="")
    tags: 'models.ManyToManyField[Tag, models.Model]' = models.ManyToManyField('viewer.Tag', blank=True, default="")
    gallery_container = models.ForeignKey(
        "self", blank=True, null=True, on_delete=models.SET_NULL, related_name="gallery_contains"
    )
    magazine = models.ForeignKey(
        "self", blank=True, null=True, on_delete=models.SET_NULL, related_name="magazine_chapters"
    )
    first_gallery = models.ForeignKey(
        "self", blank=True, null=True, on_delete=models.SET_NULL, related_name="newer_galleries"
    )
    parent_gallery = models.ForeignKey(
        "self", blank=True, null=True, on_delete=models.SET_NULL, related_name="children_galleries"
    )
    category = models.CharField(max_length=20, blank=True, null=True, default="")
    uploader = models.CharField(max_length=50, blank=True, null=True, default="")
    comment = models.TextField(blank=True, default="")
    posted = models.DateTimeField("Date posted", blank=True, null=True)
    filecount = models.IntegerField("File count", blank=True, null=True, default=0)
    filesize = models.BigIntegerField("Size", blank=True, null=True, default=0)
    expunged = models.BooleanField(default=False)
    disowned = models.BooleanField(default=False)
    rating = models.CharField(max_length=10, blank=True, null=True, default="")
    hidden = models.BooleanField(default=False)
    fjord = models.BooleanField(default=False)
    public = models.BooleanField(default=False)
    provider = models.CharField("Provider", max_length=50, default="generic")
    dl_type = models.CharField(max_length=100, default="")
    reason = models.CharField(max_length=200, blank=True, null=True, default="backup")
    create_date = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True, blank=True, null=True)
    thumbnail_url = models.URLField(max_length=2000, blank=True, null=True, default="")
    thumbnail_height = models.PositiveIntegerField(blank=True, null=True)
    thumbnail_width = models.PositiveIntegerField(blank=True, null=True)
    thumbnail = models.ImageField(
        blank=True,
        upload_to=gallery_thumb_path_handler,
        default="",
        max_length=500,
        height_field="thumbnail_height",
        width_field="thumbnail_width",
    )
    status = models.SmallIntegerField(choices=StatusChoices, db_index=True, default=StatusChoices.NORMAL)
    origin = models.SmallIntegerField(choices=OriginChoices, db_index=True, default=OriginChoices.ORIGIN_NORMAL)
    provider_metadata = models.TextField(blank=True, default="")

    history = HistoricalRecords(
        excluded_fields=[
            "origin",
            "thumbnail",
            "thumbnail_height",
            "thumbnail_width",
            "last_modified",
            "create_date",
            "provider",
        ],
        m2m_fields=[tags],
    )

    objects = GalleryManager()

    class Meta:
        es_index_name = settings.ES_GALLERY_INDEX_NAME
        es_mapping = {
            "properties": {
                "gid": {"type": "keyword"},
                "title": {"type": "text"},
                "title_jpn": {"type": "text"},
                "title_complete": {
                    "type": "completion",
                    "analyzer": "simple",
                    "preserve_separators": True,
                    "preserve_position_increments": True,
                    "max_input_length": 50,
                },
                "title_suggest": {
                    "type": "text",
                    "analyzer": "edge_ngram_analyzer",
                    "search_analyzer": "standard",
                },
                "tags": {
                    "type": "object",
                    "properties": {
                        "scope": {"type": "keyword", "store": True},
                        "name": {"type": "keyword", "store": True},
                        "full": {"type": "keyword"},
                    },
                },
                "size": {"type": "long"},
                "image_count": {"type": "integer"},
                "create_date": {"type": "date"},
                "posted_date": {"type": "date"},
                "provider": {"type": "keyword"},
                "reason": {"type": "keyword"},
                "public": {"type": "boolean"},
                "category": {"type": "keyword"},
                "rating": {"type": "float"},
                "expunged": {"type": "boolean"},
                "disowned": {"type": "boolean"},
                "uploader": {"type": "keyword"},
                "source_url": {"type": "text", "index": False},
                "last_in_chain": {"type": "boolean"},
                "gallery_chain_urls": {"type": "text", "index": False},
                "thumbnail": {"type": "text", "index": False},
                "source_thumbnail": {"type": "text", "index": False},
                "has_container": {"type": "boolean"},
                "has_magazine": {"type": "boolean"},
                "has_contained": {"type": "boolean"},
                "has_chapters": {"type": "boolean"},
            }
        }
        verbose_name_plural = "galleries"
        permissions = (
            ("publish_gallery", "Can publish available galleries"),
            ("private_gallery", "Can set private available galleries"),
            ("download_gallery", "Can download present galleries"),
            ("mark_delete_gallery", "Can mark galleries as deleted"),
            ("add_deleted_gallery", "Can add galleries as deleted"),
            ("manage_missing_archives", "Can manage missing archives"),
            ("view_submitted_gallery", "Can view submitted galleries"),
            ("approve_gallery", "Can approve submitted galleries"),
            ("wanted_gallery_found", "Can be notified of new wanted gallery matches"),
            ("crawler_adder", "Can add links to the crawler with more options"),
            ("download_history", "Can check the download history"),
            ("read_gallery_change_log", "Can read the Gallery change log"),
            ("manage_gallery", "Can manage available galleries"),
        )
        constraints = [models.UniqueConstraint(fields=["gid", "provider"], name="unique_gallery")]

        indexes = [
            models.Index(
                F("create_date").desc(nulls_last=True),
                F("public").asc(nulls_last=True),
                "status",
                name="gallery_pub_norm_create_date",
            ),
            models.Index(
                F("posted").desc(nulls_last=True),
                F("public").asc(nulls_last=True),
                "status",
                name="gallery_pub_norm_pub_date",
            ),
        ]

    def es_repr(self) -> DataDict:
        data = {}
        mapping = getattr(self._meta, "es_mapping")
        data["_id"] = self.pk
        for field_name in mapping["properties"].keys():
            data[field_name] = self.field_es_repr(field_name)
        return data

    def field_es_repr(self, field_name: str) -> typing.Any:
        config = getattr(self._meta, "es_mapping")["properties"][field_name]
        if hasattr(self, "get_es_%s" % field_name):
            field_es_value = getattr(self, "get_es_%s" % field_name)()
        else:
            if config["type"] == "object":
                related_object = getattr(self, field_name)
                if related_object:
                    field_es_value = {"_id": related_object.pk}
                    for prop in config["properties"].keys():
                        field_es_value[prop] = getattr(related_object, prop)
                else:
                    field_es_value = None
            else:
                field_es_value = getattr(self, field_name)
        return field_es_value

    def get_es_last_in_chain(self):
        if self.first_gallery:
            gallery_filters = (
                Q(first_gallery=self.first_gallery)
                | Q(first_gallery=self)
                | Q(pk=self.pk)
                | Q(pk=self.first_gallery.pk)
            )
        else:
            gallery_filters = Q(first_gallery=self) | Q(pk=self.pk)

        gallery_chain = Gallery.objects.filter(gallery_filters, provider=self.provider).order_by("gid")

        last_in_chain = gallery_chain.last()

        if last_in_chain is None:
            return True
        elif last_in_chain.pk == self.pk:
            return True
        else:
            return False

    def get_es_gallery_chain_urls(self):
        if self.first_gallery:
            gallery_filters = (
                Q(first_gallery=self.first_gallery)
                | Q(first_gallery=self)
                | Q(pk=self.pk)
                | Q(pk=self.first_gallery.pk)
            )
        else:
            gallery_filters = Q(first_gallery=self) | Q(pk=self.pk)

        gallery_chain = Gallery.objects.filter(gallery_filters, provider=self.provider).order_by("gid")

        return [x.get_link() for x in gallery_chain]

    def get_es_title_complete(self) -> DataDict:
        if self.title_jpn:
            title_input = [self.title, self.title_jpn]
        else:
            title_input = [self.title]
        return {
            "input": title_input,
        }

    def get_es_title_suggest(self) -> list[str]:
        title_input = []
        if self.title:
            title_input.append(self.title)
        if self.title_jpn:
            title_input.append(self.title_jpn)
        return title_input

    def get_es_size(self) -> int | None:
        return self.filesize

    def get_es_image_count(self) -> int | None:
        return self.filecount

    def get_es_posted_date(self) -> str | None:
        data = None
        if self.posted:
            data = self.posted.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
        return data

    def get_es_create_date(self) -> str | None:
        if self.create_date:
            return self.create_date.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
        else:
            return None

    def get_es_tags(self) -> list[DataDict]:
        data: list[DataDict] = []
        if self.tags.exists():
            data += [{"scope": c.scope, "name": c.name, "full": str(c)} for c in self.tags.all()]
        return data

    def get_es_tag_names(self) -> list[str]:
        data: list[str] = []
        if self.tags.exists():
            data += [c.name for c in self.tags.all()]
        return data

    def get_es_tag_scopes(self) -> list[str]:
        data: list[str] = []
        if self.tags.exists():
            data += [c.scope for c in self.tags.all()]
        return data

    def get_es_thumbnail(self) -> str | None:
        if self.thumbnail:
            return self.thumbnail.url
        else:
            return None

    def get_es_source_thumbnail(self) -> str | None:
        if self.thumbnail_url:
            return self.thumbnail_url
        else:
            return None

    def get_es_source_url(self) -> str:
        return self.get_link()

    def get_es_has_container(self) -> bool:
        return self.gallery_container is not None

    def get_es_has_magazine(self) -> bool:
        return self.magazine is not None

    def get_es_has_contained(self) -> bool:
        if self.gallery_contains.count() == 0:
            return False
        else:
            return True

    def get_es_has_chapters(self) -> bool:
        if self.magazine_chapters.count() == 0:
            return False
        else:
            return True


    def __str__(self) -> str:
        return self.title or self.title_jpn or ""

    @property
    def best_title(self) -> str:
        if self.title:
            return self.title
        elif self.title_jpn:
            return self.title_jpn
        return EMPTY_TITLE

    def tags_str(self) -> str:
        lst = [str(x) for x in self.tags.all()]
        return ", ".join(lst)

    def tag_list(self) -> list[str]:
        lst = [str(x) for x in self.tags.all()]
        return lst

    def tag_list_sorted(self) -> list[str]:
        return sort_tags_str(self.tags.all())

    def tag_lists(self) -> list[tuple[str, list["Tag"]]]:
        return sort_tags(self.tags.all())

    def get_available_thumbnail_plus_size(self) -> tuple[str, int | None, int | None]:
        if self.thumbnail.name and self.thumbnail.url:
            return self.thumbnail.url, None, 196
        else:
            return static("imgs/no_cover.png"), 290, 196

    def public_toggle(self) -> None:

        self.public = not self.public
        self.save()

    def set_public(self) -> None:

        self.public = True
        self.save()

    def set_private(self) -> None:

        self.public = False
        self.save()

    def is_deleted(self) -> bool:
        return self.status == self.StatusChoices.DELETED

    def is_normal(self) -> bool:
        return self.status == self.StatusChoices.NORMAL

    def is_denied(self) -> bool:
        return self.status == self.StatusChoices.DENIED

    # Kinda not optimal
    def is_submitted(self) -> bool:
        return (
            self.origin == self.OriginChoices.ORIGIN_SUBMITTED
            and self.status != self.StatusChoices.DENIED
            and self.status != self.StatusChoices.DELETED
            and not self.archive_set.all()
        )

    def get_link(self) -> str:
        return settings.PROVIDER_CONTEXT.resolve_all_urls(self)

    def get_absolute_url(self) -> str:
        return reverse("viewer:gallery", args=[str(self.id)])

    def as_gallery_data(self) -> GalleryData:
        gallery_data = GalleryData(
            self.gid,
            self.provider,
            token=self.token,
            link=self.get_link(),
            tags=self.tag_list(),
            title=self.title,
            title_jpn=self.title_jpn,
            comment=self.comment,
            gallery_container_gid=self.gallery_container.gid if self.gallery_container else None,
            gallery_contains_gids=list(self.gallery_contains.all().values_list("gid", flat=True)),
            magazine_gid=self.magazine.gid if self.magazine else None,
            magazine_chapters_gids=list(self.magazine_chapters.all().values_list("gid", flat=True)),
            parent_gallery_gid=self.parent_gallery.gid if self.parent_gallery else None,
            first_gallery_gid=self.first_gallery.gid if self.first_gallery else None,
            category=self.category,
            posted=self.posted,
            filesize=self.filesize,
            filecount=self.filecount,
            expunged=self.expunged,
            disowned=self.disowned,
            rating=self.rating,
            fjord=self.fjord,
            hidden=self.hidden,
            uploader=self.uploader,
            thumbnail_url=self.thumbnail_url,
            dl_type=self.dl_type,
            public=self.public,
            status=self.status,
            origin=self.origin,
            reason=self.reason,
        )
        return gallery_data

    # Should be used by small changes.
    def simple_save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super(Gallery, self).save(*args, **kwargs)

    def save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        is_new = self.pk
        super(Gallery, self).save(*args, **kwargs)
        if settings.ES_CLIENT and settings.ES_AUTOREFRESH_GALLERY:
            if (settings.ES_ONLY_INDEX_PUBLIC and self.public) or not settings.ES_ONLY_INDEX_PUBLIC:
                payload = self.es_repr()
                del payload["_id"]
                es_index = getattr(self._meta, "es_index_name")
                if is_new is not None:
                    try:
                        settings.ES_CLIENT.update(
                            index=es_index,
                            id=str(self.pk),
                            refresh=True,
                            doc=payload
                        )
                    except elasticsearch.exceptions.NotFoundError:
                        settings.ES_CLIENT.create(
                            index=es_index,
                            id=str(self.pk),
                            refresh=True,
                            document=payload
                        )

                else:
                    settings.ES_CLIENT.create(
                        index=es_index,
                        id=self.pk,
                        refresh=True,
                        document=payload
                    )
        self.fetch_thumbnail()

    def fetch_thumbnail(self, force_redownload: bool = False, force_provider: str | None = None) -> bool:
        if self.thumbnail_url and (not self.thumbnail or force_redownload):
            request_dict = {
                "stream": True,
                "headers": settings.CRAWLER_SETTINGS.requests_headers,
                "timeout": settings.CRAWLER_SETTINGS.timeout_timer,
            }

            if force_provider:
                provider_to_use = force_provider
            else:
                provider_to_use = self.provider

            if self.provider in settings.CRAWLER_SETTINGS.providers:
                if settings.CRAWLER_SETTINGS.providers[provider_to_use].cookies:
                    request_dict["cookies"] = settings.CRAWLER_SETTINGS.providers[provider_to_use].cookies
                if settings.CRAWLER_SETTINGS.providers[provider_to_use].proxies:
                    request_dict["proxies"] = settings.CRAWLER_SETTINGS.providers[provider_to_use].proxies
                if settings.CRAWLER_SETTINGS.providers[provider_to_use].timeout_timer:
                    request_dict["timeout"] = settings.CRAWLER_SETTINGS.providers[provider_to_use].timeout_timer
            response = request_with_retries(
                self.thumbnail_url,
                request_dict,
                post=False,
            )
            if response:
                disassembled = urlparse(self.thumbnail_url)
                file_name = basename(disassembled.path)
                if not file_name:
                    file_name = "g_thumbnail"
                lf = NamedTemporaryFile()
                if response.status_code == requests.codes.ok:
                    for chunk in response.iter_content(chunk_size=1024):
                        if chunk:  # filter out keep-alive new chunks
                            lf.write(chunk)
                    self.thumbnail.save(file_name, File(lf), save=False)
                lf.close()

                super(Gallery, self).save(force_update=True)

                if settings.CRAWLER_SETTINGS.auto_phash_images:
                    self.create_or_update_thumbnail_hash("phash")

                return True

        return False

    def create_or_update_thumbnail_hash(self, algorithm: str):

        if self.thumbnail:
            gallery_type = ContentType.objects.get_for_model(Gallery)

            hash_result = CompareObjectsService.hash_thumbnail(self.thumbnail.path, algorithm)
            if hash_result:
                hash_object, _ = ItemProperties.objects.update_or_create(
                    content_type=gallery_type,
                    object_id=self.pk,
                    tag="hash-compare",
                    name=algorithm,
                    defaults={"value": hash_result},
                )

    def update_index(self) -> None:
        if settings.ES_CLIENT and settings.ES_AUTOREFRESH_GALLERY:
            if (settings.ES_ONLY_INDEX_PUBLIC and self.public) or not settings.ES_ONLY_INDEX_PUBLIC:
                payload = self.es_repr()
                del payload["_id"]
                es_index = getattr(self._meta, "es_index_name")
                try:
                    settings.ES_CLIENT.update(
                        index=es_index,
                        id=str(self.pk),
                        refresh=True,
                        doc=payload
                    )
                except elasticsearch.exceptions.NotFoundError:
                    settings.ES_CLIENT.create(
                        index=es_index,
                        id=str(self.pk),
                        refresh=True,
                        document=payload
                    )

    def delete(self, *args: typing.Any, **kwargs: typing.Any) -> tuple[int, dict[str, int]]:
        self.thumbnail.delete(save=False)
        self.remove_wanted_relations()
        prev_pk = self.pk
        deleted = super(Gallery, self).delete(*args, **kwargs)
        if settings.ES_CLIENT and settings.ES_AUTOREFRESH_GALLERY:
            try:
                settings.ES_CLIENT.options(request_timeout=30).delete(
                    index=getattr(self._meta, "es_index_name"), id=str(prev_pk), refresh=True
                )
            except elasticsearch.exceptions.NotFoundError:
                pass
        return deleted

    def remove_wanted_relations(self) -> None:
        from viewer.models.wanted import FoundGallery, GalleryMatch

        for wanted_gallery in self.found_galleries.all():
            fg = FoundGallery.objects.filter(wanted_gallery=wanted_gallery, gallery=self)
            if fg:
                fg.delete()
            wanted_gallery.save()
        for wanted_gallery in self.gallery_matches.all():
            gm = GalleryMatch.objects.filter(wanted_gallery=wanted_gallery, gallery=self)
            if gm:
                gm.delete()
            wanted_gallery.save()
        self.found_galleries.clear()
        self.possible_matches.clear()

    def mark_as_deleted(self) -> None:
        self.remove_wanted_relations()
        self.status = self.StatusChoices.DELETED
        self.public = False
        self.save()

    def mark_as_denied(self) -> None:
        self.remove_wanted_relations()
        self.status = self.StatusChoices.DENIED
        self.public = False
        self.save()

    def mark_as_normal(self) -> None:
        self.status = self.StatusChoices.NORMAL
        self.save()

    def match_against_wanted_galleries(
        self, wanted_filters: "QuerySet[WantedGallery] | None" = None, skip_already_found: bool = True
    ) -> "list[WantedGallery]":
        from viewer.models.wanted import WantedGallery

        found_wanted_galleries = []

        if not wanted_filters:
            wanted_filters = WantedGallery.objects.all()

        if skip_already_found:
            wanted_filters = wanted_filters.filter(~Q(foundgallery__gallery=self))

        if self.title or self.title_jpn:
            q_objects = Q()
            q_objects_unwanted = Q()
            q_objects_regexp = Q()
            q_objects_regexp_icase = Q()
            q_objects_unwanted_regexp = Q()
            q_objects_unwanted_regexp_icase = Q()
            if self.title:
                wanted_filters = wanted_filters.annotate(g_title=Value(self.title, output_field=CharField()))

                q_objects.add(
                    Q(g_title__ss=Concat(Value("%"), Replace(F("search_title"), Value(" "), Value("%")), Value("%"))),
                    Q.OR,
                )
                q_objects_unwanted.add(
                    ~Q(
                        g_title__ss=Concat(Value("%"), Replace(F("unwanted_title"), Value(" "), Value("%")), Value("%"))
                    ),
                    Q.AND,
                )

                q_objects_regexp.add(Q(g_title__regex=F("search_title")), Q.OR)
                q_objects_regexp_icase.add(Q(g_title__iregex=F("search_title")), Q.OR)
                q_objects_unwanted_regexp.add(~Q(g_title__regex=F("unwanted_title")), Q.AND)
                q_objects_unwanted_regexp_icase.add(~Q(g_title__iregex=F("unwanted_title")), Q.AND)

            if self.title_jpn:
                wanted_filters = wanted_filters.annotate(g_title_jpn=Value(self.title_jpn, output_field=CharField()))
                q_objects.add(
                    Q(
                        g_title_jpn__ss=Concat(
                            Value("%"), Replace(F("search_title"), Value(" "), Value("%")), Value("%")
                        )
                    ),
                    Q.OR,
                )
                q_objects_unwanted.add(
                    ~Q(
                        g_title_jpn__ss=Concat(
                            Value("%"), Replace(F("unwanted_title"), Value(" "), Value("%")), Value("%")
                        )
                    ),
                    Q.AND,
                )

                q_objects_regexp.add(Q(g_title_jpn__regex=F("search_title")), Q.OR)
                q_objects_regexp_icase.add(Q(g_title_jpn__iregex=F("search_title")), Q.OR)
                q_objects_unwanted_regexp.add(~Q(g_title_jpn__regex=F("unwanted_title")), Q.AND)
                q_objects_unwanted_regexp_icase.add(~Q(g_title_jpn__iregex=F("unwanted_title")), Q.AND)

            filtered_wanted = wanted_filters.filter(
                Q(search_title__isnull=True)
                | Q(search_title="")
                | Q(Q(regexp_search_title=False), q_objects)
                | Q(Q(regexp_search_title=True, regexp_search_title_icase=False), q_objects_regexp)
                | Q(Q(regexp_search_title=True, regexp_search_title_icase=True), q_objects_regexp_icase)
            ).filter(
                Q(unwanted_title__isnull=True)
                | Q(unwanted_title="")
                | Q(Q(regexp_unwanted_title=False), q_objects_unwanted)
                | Q(Q(regexp_unwanted_title=True, regexp_unwanted_title_icase=False), q_objects_unwanted_regexp)
                | Q(Q(regexp_unwanted_title=True, regexp_unwanted_title_icase=True), q_objects_unwanted_regexp_icase)
            )

        else:
            filtered_wanted = wanted_filters.filter(Q(search_title__isnull=True) | Q(search_title="")).filter(
                Q(unwanted_title__isnull=True) | Q(unwanted_title="")
            )

        if self.category:
            filtered_wanted = filtered_wanted.filter(
                Q(category__isnull=True) | Q(category="") | Q(category__iexact=self.category)
            )
            filtered_wanted = filtered_wanted.filter(Q(categories=None) | Q(categories__name=self.category))

        if self.filecount:
            filtered_wanted = filtered_wanted.filter(
                Q(wanted_page_count_upper=0) | Q(wanted_page_count_upper__gte=self.filecount)
            )
            filtered_wanted = filtered_wanted.filter(
                Q(wanted_page_count_lower=0) | Q(wanted_page_count_lower__lte=self.filecount)
            )

        if self.provider:
            filtered_wanted = filtered_wanted.filter(Q(wanted_providers=None) | Q(wanted_providers__slug=self.provider))
            filtered_wanted = filtered_wanted.filter(
                Q(unwanted_providers=None) | ~Q(unwanted_providers__slug=self.provider)
            )

        if self.posted:
            filtered_wanted = filtered_wanted.filter(
                Q(wait_for_time__isnull=True) | Q(wait_for_time__lte=django_tz.now() - self.posted)
            )

        for wanted_filter in filtered_wanted:
            # if wanted_filter.wanted_providers.count():
            #     if not wanted_filter.wanted_providers.filter(slug=self.provider).first():
            #         continue
            accepted = True
            if bool(wanted_filter.wanted_tags.all()):
                if not set(wanted_filter.wanted_tags_list()).issubset(set(self.tag_list())):
                    accepted = False
                # Review based on 'accept if none' scope.
                if not accepted and wanted_filter.wanted_tags_accept_if_none_scope:
                    missing_tags = set(wanted_filter.wanted_tags_list()).difference(set(self.tag_list()))
                    # If all the missing tags start with the parameter,
                    # and no other tag is in gallery with this parameter, mark as accepted
                    scope_formatted = wanted_filter.wanted_tags_accept_if_none_scope + ":"
                    if all(x.startswith(scope_formatted) for x in missing_tags) and not any(
                        x.startswith(scope_formatted) for x in self.tag_list()
                    ):
                        accepted = True
                # Do not accept galleries that have more than 1 tag in the same wanted tag scope.
                if accepted & wanted_filter.wanted_tags_exclusive_scope:
                    accepted_tags = set(wanted_filter.wanted_tags_list()).intersection(set(self.tag_list()))
                    gallery_tags_scopes = [x.split(":", maxsplit=1)[0] for x in self.tag_list() if len(x) > 1]
                    wanted_gallery_tags_scopes = [x.split(":", maxsplit=1)[0] for x in accepted_tags if len(x) > 1]
                    scope_count: dict[str, int] = defaultdict(int)
                    for scope_name in gallery_tags_scopes:
                        if scope_name in wanted_gallery_tags_scopes:
                            if wanted_filter.exclusive_scope_name:
                                if wanted_filter.exclusive_scope_name == scope_name:
                                    scope_count[scope_name] += 1
                            else:
                                scope_count[scope_name] += 1
                    for scope, count in scope_count.items():
                        if count > 1:
                            accepted = False
                    if not accepted:
                        continue

            if not accepted:
                continue

            if bool(wanted_filter.unwanted_tags.all()):
                if any(item in self.tag_list() for item in wanted_filter.unwanted_tags_list()):
                    continue

            if wanted_filter.match_expression:
                expression_matched = wanted_filter.evaluate_match_expression(self)
                if not expression_matched:
                    continue

            found_wanted_galleries.append(wanted_filter)

        return found_wanted_galleries





class GalleryProviderData(models.Model):
    ORIGIN_NATIVE = 1
    ORIGIN_PROCESSED = 2
    ORIGIN_OTHER = 3

    ORIGIN_CHOICES = (
        (ORIGIN_NATIVE, "Native"),
        (ORIGIN_PROCESSED, "Processed"),
        (ORIGIN_OTHER, "Other"),
    )

    TYPE_TEXT = "text"
    TYPE_FLOAT = "float"
    TYPE_INT = "int"
    TYPE_DATE = "date"
    TYPE_DURATION = "duration"
    TYPE_BOOLEAN = "bool"

    DATA_TYPES_CHOICES = (
        (TYPE_TEXT, _("Text")),
        (TYPE_FLOAT, _("Float")),
        (TYPE_INT, _("Integer")),
        (TYPE_DATE, _("Date")),
        (TYPE_DURATION, _("Duration")),
        (TYPE_BOOLEAN, _("Boolean")),
    )

    DATA_TYPES = (
        TYPE_TEXT,
        TYPE_FLOAT,
        TYPE_INT,
        TYPE_DATE,
        TYPE_DURATION,
        TYPE_BOOLEAN,
    )

    class Meta:
        unique_together = ("gallery", "name")
        verbose_name_plural = "Gallery provider datas"

    name = models.CharField(max_length=100)
    origin = models.SmallIntegerField(choices=ORIGIN_CHOICES, default=ORIGIN_OTHER, db_index=True, blank=True)

    data_type = models.CharField(max_length=10, choices=DATA_TYPES_CHOICES)
    value_text = models.TextField(blank=True, null=True)
    value_float = models.FloatField(blank=True, null=True)
    value_int = models.IntegerField(blank=True, null=True)
    value_date = models.DateTimeField(blank=True, null=True)
    value_duration = models.DurationField(blank=True, null=True)
    value_bool = models.BooleanField(blank=True, null=True)

    gallery = models.ForeignKey(Gallery, blank=True, null=True, on_delete=models.CASCADE)

    def _get_value(self) -> FetchTypes:
        return getattr(self, "value_%s" % self.data_type)

    def _set_value(self, new_value: FetchTypes) -> None:
        setattr(self, "value_%s" % self.data_type, new_value)

    def clean(self) -> None:
        # Only allowed types
        if self.data_type not in self.DATA_TYPES:
            raise ValidationError("{} must be one of: {}".format(self.data_type, self.DATA_TYPES))
        # Don't allow empty string for all but text.
        if self.value == "" and self.data_type is not self.TYPE_TEXT:
            raise ValidationError("value_{0} cannot be blank when data type is {0}".format(self.data_type))

    def save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        self.full_clean()
        super(GalleryProviderData, self).save(*args, **kwargs)

    value = property(_get_value, _set_value)



class GallerySubmitEntryQuerySet(models.QuerySet):
    def to_be_resolved(self, *args: typing.Any, **kwargs: typing.Any) -> QuerySet:
        return self.filter(Q(resolved_status=GallerySubmitEntry.RESOLVED_SUBMITTED), **kwargs)


class GallerySubmitEntryManager(models.Manager["GallerySubmitEntry"]):
    def get_queryset(self) -> GallerySubmitEntryQuerySet:
        return GallerySubmitEntryQuerySet(self.model, using=self._db)

    def to_be_resolved(self, *args: typing.Any, **kwargs: typing.Any) -> QuerySet:
        return self.get_queryset().to_be_resolved(*args, **kwargs)


class GallerySubmitEntry(models.Model):
    objects = GallerySubmitEntryManager()

    RESOLVED_SUBMITTED = 1
    RESOLVED_APPROVED = 2
    RESOLVED_DENIED = 3
    RESOLVED_ALREADY_PRESENT = 4

    RESOLVED_STATUS_CHOICES = (
        (RESOLVED_SUBMITTED, "Submitted"),
        (RESOLVED_APPROVED, "Approved"),
        (RESOLVED_DENIED, "Denied"),
        (RESOLVED_ALREADY_PRESENT, "Already present"),
    )

    class Meta:
        verbose_name_plural = "Gallery submit entries"

    gallery = models.ForeignKey(Gallery, blank=True, null=True, on_delete=models.SET_NULL)
    submit_url = models.TextField(blank=True, null=True, default="")
    submit_reason = models.TextField(blank=True, null=True, default="")
    submit_extra = models.TextField(blank=True, null=True, default="")
    submit_result = models.CharField(blank=True, null=True, default="", max_length=200)
    submit_date = models.DateTimeField(blank=True, default=django_tz.now)
    submit_group = models.UUIDField(blank=True, null=True)
    create_date = models.DateTimeField(auto_now_add=True)
    resolved_date = models.DateTimeField(blank=True, null=True)
    resolved_status = models.SmallIntegerField(
        choices=RESOLVED_STATUS_CHOICES, db_index=True, default=RESOLVED_SUBMITTED
    )
    resolved_reason = models.CharField("Reason", max_length=200, blank=True, null=True, default="backup")
    resolved_comment = models.TextField(blank=True, null=True, default="")
    # Related name set to + to avoid clutter on Gallery model
    similar_galleries = models.ManyToManyField(Gallery, related_name="+", blank=True, default="")

    def mark_as_denied(self, reason="", comment="") -> None:
        self.resolved_status = self.RESOLVED_DENIED
        if reason:
            self.resolved_reason = reason
        if comment:
            self.resolved_comment = comment
        self.resolved_date = django_tz.now()
        self.save()

    def mark_as_approved(self, reason="", comment="") -> None:
        self.resolved_status = self.RESOLVED_APPROVED
        if reason:
            self.resolved_reason = reason
        if comment:
            self.resolved_comment = comment
        self.resolved_date = django_tz.now()
        self.save()

    def __str__(self) -> str:
        return self.submit_url or "Empty url"

    def save(self, *args, **kwargs):
        # Only calculate similar galleries for similar galleries
        if self.gallery and self.resolved_status == self.RESOLVED_SUBMITTED:
            similar_galleries = (
                Gallery.objects.filter(filesize=self.gallery.filesize, filecount=self.gallery.filecount)
                .exclude(filesize__isnull=True)
                .exclude(filecount__isnull=True)
                .exclude(filesize=0)
                .exclude(filecount=0)
                .exclude(gid=self.gallery.gid, provider=self.gallery.provider)
            )
            self.similar_galleries.set(similar_galleries)
        # Clear similar galleries for resolved galleries (will add more code to galleries without similar galleries)
        if self.pk is not None and self.resolved_status != self.RESOLVED_SUBMITTED:
            self.similar_galleries.clear()
        super().save(*args, **kwargs)


@receiver(post_delete, sender=Gallery)
def thumbnail_post_delete_handler(sender: typing.Any, **kwargs: typing.Any) -> None:
    gallery = kwargs["instance"]
    gallery.thumbnail.delete(save=False)


def upload_mention_handler(instance: "Mention", filename: str) -> str:
    return "mentions/{id}/fn_{file}{ext}".format(id=instance.id, file=uuid.uuid4(), ext=filename)


def upload_mention_thumb_handler(instance: "Mention", filename: str) -> str:
    return "mentions/{id}/tn_{file}{ext}".format(id=instance.id, file=uuid.uuid4(), ext=filename)


class Mention(models.Model):
    mention_date = models.DateTimeField("Mention date", blank=True, null=True)
    release_date = models.DateTimeField("Release date", blank=True, null=True)
    type = models.CharField(max_length=50, blank=True, null=True, default="")
    source = models.CharField(max_length=50, blank=True, null=True, default="")
    comment = models.CharField(max_length=100, blank=True, null=True, default="")
    image = models.ImageField(
        upload_to=upload_mention_handler,
        blank=True,
        null=True,
        max_length=500,
        height_field="image_height",
        width_field="image_width",
    )
    image_height = models.PositiveIntegerField(blank=True, null=True)
    image_width = models.PositiveIntegerField(blank=True, null=True)
    thumbnail_height = models.PositiveIntegerField(blank=True, null=True)
    thumbnail_width = models.PositiveIntegerField(blank=True, null=True)
    thumbnail = models.ImageField(
        upload_to=upload_mention_thumb_handler,
        blank=True,
        null=True,
        max_length=500,
        height_field="thumbnail_height",
        width_field="thumbnail_width",
    )

    def __str__(self) -> str:
        return str(self.mention_date)

    def save_img(self, img_link: str) -> None:
        tf2 = NamedTemporaryFile()

        request_dict = {
            "stream": True,
            "headers": settings.CRAWLER_SETTINGS.requests_headers,
            "timeout": settings.CRAWLER_SETTINGS.timeout_timer,
        }

        response = request_with_retries(
            img_link,
            request_dict,
            post=False,
        )

        if response:
            if response.status_code == requests.codes.ok:
                for chunk in response.iter_content(chunk_size=4096):
                    if chunk:  # filter out keep-alive new chunks
                        tf2.write(chunk)
                self.image.save(os.path.splitext(img_link)[1], File(tf2), save=False)
                tf2.close()
                self.regen_tn()

    def copy_img(self, img_path: str) -> None:
        tf2 = NamedTemporaryFile()

        shutil.copy(img_path, tf2.name)

        self.image.save(os.path.splitext(img_path)[1], File(tf2), save=False)
        tf2.close()

        self.regen_tn()

    def regen_tn(self) -> None:
        if not self.image or not self.image.name:
            return
        im: PImage.Image | ImageFile.ImageFile = PImage.open(self.image.path)
        im = img_to_thumbnail(im)
        thumb_relative_path = upload_mention_thumb_handler(self, os.path.splitext(self.image.name)[1])
        thumb_fn = pjoin(settings.MEDIA_ROOT, thumb_relative_path)
        os.makedirs(os.path.dirname(thumb_fn), exist_ok=True)
        im.save(thumb_fn, "JPEG")
        im_size = im.size
        im.close()
        self.thumbnail = thumb_relative_path
        if im_size:
            self.thumbnail_width, self.thumbnail_height = im_size

        self.save()


class Artist(models.Model):
    name = models.CharField(max_length=50, blank=True, null=True, default="")
    name_jpn = models.CharField(max_length=50, blank=True, null=True, default="")
    twitter_handle = models.CharField(max_length=50, blank=True, null=True, default="")

    def __str__(self) -> str:
        return self.name or self.name_jpn or self.twitter_handle or ""



class ProcessedLinks(models.Model):
    provider = models.ForeignKey("viewer.Provider", on_delete=models.SET_NULL, null=True, blank=True)
    source_id = models.CharField("Source Id", max_length=200, unique=True)
    url = models.URLField(max_length=2000)
    title = models.CharField(max_length=500, blank=True, null=True, default="")
    link_date = models.DateTimeField("Link date", blank=True, null=True)
    content = models.TextField(blank=True, default="")
    create_date = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "Processed Links"



class TweetPost(models.Model):
    tweet_id = models.BigIntegerField(blank=True, null=True)
    text = models.CharField(max_length=200, blank=True, null=True, default="")
    user = models.CharField(max_length=200, blank=True, null=True, default="")
    posted_date = models.DateTimeField("Posted date", blank=True, null=True, default=django_tz.now)
    media_url = models.CharField(max_length=200, blank=True, null=True, default="")


class Provider(models.Model):
    name = models.CharField(max_length=100, help_text=_("User friendly name"))
    slug = models.SlugField(unique=True)
    home_page = models.URLField(blank=True, default="")
    description = models.CharField(max_length=500, blank=True, default="")
    information = models.TextField(blank=True, default="")

    create_date = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True, blank=True, null=True)

    def __str__(self) -> str:
        return self.name


class Category(models.Model):
    name = models.CharField(max_length=100, help_text=_("User friendly name"))
    slug = models.SlugField(unique=True)

    create_date = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True, blank=True, null=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "Categories"

    def __str__(self) -> str:
        return self.name

    def save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        if not self.slug:

            slug_candidate = slugify(self.name, allow_unicode=True)
            for i in itertools.count(1):
                if not Category.objects.filter(slug=slug_candidate).exists():
                    break
                slug_candidate = slugify("{}-{}".format(self.name, i), allow_unicode=True)

            self.slug = slug_candidate
        super(Category, self).save(*args, **kwargs)


class AttributeQuerySet(models.QuerySet["Attribute"]):
    def fetch_value[T](self, name: str) -> T | None:
        attr = self.filter(name=name).first()

        if attr:
            return typing.cast(T, attr.value)
        else:
            return None


class AttributeManager(models.Manager["Attribute"]):
    def get_queryset(self) -> AttributeQuerySet:
        return AttributeQuerySet(self.model, using=self._db)

    def fetch_value(self, name: str) -> FetchTypes:
        return self.get_queryset().fetch_value(name)


class Attribute(models.Model):
    objects = AttributeManager()

    class Meta:
        unique_together = ("name", "provider")
        verbose_name_plural = "Provider attributes"

    TYPE_TEXT = "text"
    TYPE_FLOAT = "float"
    TYPE_INT = "int"
    TYPE_DATE = "date"
    TYPE_DURATION = "duration"
    TYPE_BOOLEAN = "bool"

    DATA_TYPES_CHOICES = (
        (TYPE_TEXT, _("Text")),
        (TYPE_FLOAT, _("Float")),
        (TYPE_INT, _("Integer")),
        (TYPE_DATE, _("Date")),
        (TYPE_DURATION, _("Duration")),
        (TYPE_BOOLEAN, _("Boolean")),
    )

    DATA_TYPES = (
        TYPE_TEXT,
        TYPE_FLOAT,
        TYPE_INT,
        TYPE_DATE,
        TYPE_DURATION,
        TYPE_BOOLEAN,
    )

    name = models.CharField(max_length=100)
    data_type = models.CharField(max_length=10, choices=DATA_TYPES_CHOICES)
    value_text = models.TextField(blank=True, null=True)
    value_float = models.FloatField(blank=True, null=True)
    value_int = models.IntegerField(blank=True, null=True)
    value_date = models.DateTimeField(blank=True, null=True)
    value_duration = models.DurationField(blank=True, null=True)
    value_bool = models.BooleanField(blank=True, null=True)

    provider = models.ForeignKey(Provider, on_delete=models.CASCADE)

    create_date = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True, blank=True, null=True)

    def _get_value(self) -> typing.Any:
        return getattr(self, "value_%s" % self.data_type)

    def _set_value(self, new_value: str) -> None:
        setattr(self, "value_%s" % self.data_type, new_value)

    def clean(self) -> None:
        # Only allowed types
        if self.data_type not in self.DATA_TYPES:
            raise ValidationError("{} must be one of: {}".format(self.data_type, self.DATA_TYPES))
        # Don't allow empty string for all but text.
        if self.value == "" and self.data_type is not self.TYPE_TEXT:
            raise ValidationError("value_{0} cannot be blank when data type is {0}".format(self.data_type))

    def save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        self.full_clean()
        super(Attribute, self).save(*args, **kwargs)

    value = property(_get_value, _set_value)



