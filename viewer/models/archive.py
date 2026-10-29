import base64
import io
import itertools
import json
import logging
import multiprocessing
import os
import re
import shutil
import subprocess
import time
import typing
import uuid
import warnings
import zipfile
import zlib
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta, timezone
from operator import itemgetter
from os.path import basename, join as pjoin
from tempfile import NamedTemporaryFile, mkdtemp
from urllib.parse import quote, urlparse

import elasticsearch
from PIL import Image as PImage
from PIL import ImageFile, ImageOps
import requests
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.files import File
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models import CharField, Count, F, Q, QuerySet, Value
from django.db.models.functions import Concat, Replace
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.http import HttpRequest
from django.templatetags.static import static
from django.urls import reverse
import django.utils.timezone as django_tz
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from core.base.comparison import get_list_closer_text_from_list
from core.base.image_ops import img_to_thumbnail
from core.base.tag_logic import ArchiveTagsComparer
from core.base.types import ArchiveGenericFile, ArchiveStatisticsCalculator, DataDict, GalleryData
from core.base.utilities import (
    available_filename,
    calc_crc32,
    check_and_convert_to_zip,
    clean_title,
    file_matches_any_filter,
    get_dict_allowed_fields,
    get_images_from_zip,
    get_title_from_path,
    get_zip_fileinfo,
    get_zip_filesize,
    hamming_distance,
    replace_illegal_name,
    request_with_retries,
    sha1_from_file_object,
)
from viewer.models.common import EMPTY_TITLE, IMAGE_PHASH_BLACK, IMAGE_PHASH_WHITE, ItemProperties, SpacedSearch, fs
from viewer.models.gallery import Gallery
from viewer.models.matching import ArchiveMatches
from viewer.models.tag import SortedTagList, Tag
from viewer.services import CompareObjectsService
from viewer.utils import image_processing
from viewer.utils.tags import sort_tags, sort_tags_str

if typing.TYPE_CHECKING:
    from core.base.setup import CloningImageToolSettings
    from viewer.models.wanted import FoundWantedImageOnArchive, WantedImage

logger = logging.getLogger(__name__)


def archive_path_handler(instance: "Archive", filename: str) -> str:
    return "galleries/archive_uploads/{file}".format(file=filename)


def thumb_path_handler(instance: "Archive", filename: str) -> str:
    return "images/thumbs/archive_{id}/{file}".format(id=instance.id, file=filename)



class ArchiveQuerySet(models.QuerySet["Archive"]):
    def filter(self, *args: typing.Any, **kwargs: typing.Any) -> typing.Self:
        return super().filter(*args, **kwargs)

    def exclude(self, *args: typing.Any, **kwargs: typing.Any) -> typing.Self:
        return super().exclude(*args, **kwargs)

    def order_by(self, *field_names: typing.Any) -> typing.Self:
        return super().order_by(*field_names)

    def select_related(self, *fields: typing.Any) -> typing.Self:
        return super().select_related(*fields)

    def prefetch_related(self, *lookups: typing.Any) -> typing.Self:
        return super().prefetch_related(*lookups)

    def distinct(self, *field_names: typing.Any) -> typing.Self:
        return super().distinct(*field_names)

    def none(self) -> typing.Self:
        return super().none()

    def filter_non_existent(self, root: str, **kwargs: typing.Any) -> list["Archive"]:
        archives = self.filter(**kwargs, file_deleted=False, binned=False).order_by("-id")

        return [
            archive
            for archive in archives
            if (not archive.zipped or not archive.zipped.name)
            or (not os.path.isfile(os.path.join(root, archive.zipped.path)))
        ]


class ArchiveManager(models.Manager["Archive"]):
    def get_queryset(self) -> ArchiveQuerySet:
        return ArchiveQuerySet(self.model, using=self._db)

    def all(self) -> ArchiveQuerySet:
        return self.get_queryset()

    def none(self) -> ArchiveQuerySet:
        return self.get_queryset().none()

    def filter(self, *args: typing.Any, **kwargs: typing.Any) -> ArchiveQuerySet:
        return self.get_queryset().filter(*args, **kwargs)

    def exclude(self, *args: typing.Any, **kwargs: typing.Any) -> ArchiveQuerySet:
        return self.get_queryset().exclude(*args, **kwargs)

    def order_by(self, *field_names: typing.Any) -> ArchiveQuerySet:
        return self.get_queryset().order_by(*field_names)

    def filter_and_order_by_posted(self, **kwargs: typing.Any) -> ArchiveQuerySet:
        return self.get_queryset().filter(**kwargs).order_by("gallery__posted")

    def filter_non_existent(self, root: str, **kwargs: typing.Any) -> list["Archive"]:
        return self.get_queryset().filter_non_existent(root, **kwargs)

    def filter_by_dl_remote(self) -> ArchiveQuerySet:
        from .events import DownloadEvent

        qs = self.get_queryset().filter(
            Q(crc32="") & (Q(match_type__startswith="torrent") | Q(match_type__startswith="hath")) & Q(binned=False)
        )
        dead_archive_ids = (
            DownloadEvent.objects.filter(
                Q(status__in=[DownloadEvent.Status.FAILED, DownloadEvent.Status.CANCELLED]) | Q(failed=True),
                archive__isnull=False,
            )
            .exclude(
                archive__in=DownloadEvent.objects.filter(completed=False)
                .exclude(status__in=[DownloadEvent.Status.FAILED, DownloadEvent.Status.CANCELLED])
                .values("archive_id")
            )
            .values_list("archive_id", flat=True)
        )
        return qs.exclude(id__in=dead_archive_ids)

    def filter_by_missing_file_info(self) -> ArchiveQuerySet:
        return self.get_queryset().filter(crc32="")

    def filter_by_authenticated_status(self, authenticated: bool, **kwargs: typing.Any) -> ArchiveQuerySet:
        if authenticated:
            return self.get_queryset().filter(**kwargs)
        else:
            return self.get_queryset().filter(public=True, **kwargs)

    def filter_matching_gallery_filesize(self, gid: str, provider: str) -> "Archive | None":
        return self.filter(
            Q(gallery__gid=gid), Q(gallery__provider=provider), Q(filesize=F("gallery__filesize"))
        ).first()

    def update_or_create_by_values_and_gid(
        self, values: DataDict, gid_provider: tuple[str, str] | None, **kwargs: typing.Any
    ) -> "Archive":
        archive, _ = self.update_or_create(defaults=values, **kwargs)
        if gid_provider:
            gallery, _ = Gallery.objects.get_or_create(gid=gid_provider[0], provider=gid_provider[1])
            archive.gallery = gallery
        archive.ingest()
        if archive.gallery:
            archive.set_tags_from_gallery(archive.gallery)

        return archive

    @staticmethod
    def create_by_values_and_gid(values: DataDict, gid_provider: tuple[str, str] | None) -> "Archive":
        archive = Archive(**values)
        archive.simple_save()
        if gid_provider:
            gallery, _ = Gallery.objects.get_or_create(gid=gid_provider[0], provider=gid_provider[1])
            archive.gallery = gallery
            archive.set_tags_from_gallery(gallery)

        if archive.title:
            base_file_name = replace_illegal_name(archive.title)
        elif archive.title_jpn:
            base_file_name = replace_illegal_name(archive.title_jpn)
        else:
            base_file_name = str(archive.id)
        archive.zipped = os.path.join(
            "galleries/archives/{id}/{file}".format(id=archive.id, file=base_file_name + ".zip"),
        )
        os.makedirs(os.path.join(settings.MEDIA_ROOT, "galleries/archives/{id}".format(id=archive.id)), exist_ok=True)
        archive.ingest()

        return archive

    def add_or_update_from_values(self, values: DataDict, **kwargs: typing.Any) -> "Archive":

        archive, _ = self.update_or_create(defaults=values, **kwargs)
        archive.ingest()
        return archive

    def delete_by_filter(self, **kwargs: typing.Any) -> bool:
        t = self.filter(**kwargs)
        if t.exists():
            t.delete()
            return True
        else:
            return False



class Archive(models.Model):

    ORIGIN_DEFAULT = 1
    ORIGIN_ACCEPT_SUBMITTED = 2
    ORIGIN_ADD_URL = 3
    ORIGIN_UPLOAD_ARCHIVE = 4
    ORIGIN_WANTED_GALLERY = 5
    ORIGIN_FOLDER_SCAN = 6

    ORIGIN_CHOICES = (
        (ORIGIN_DEFAULT, "Default"),
        (ORIGIN_ACCEPT_SUBMITTED, "Accept Submitted"),
        (ORIGIN_ADD_URL, "Add URL"),
        (ORIGIN_UPLOAD_ARCHIVE, "Upload Archive"),
        (ORIGIN_WANTED_GALLERY, "Wanted Gallery"),
        (ORIGIN_FOLDER_SCAN, "Folder Scan"),
    )

    gallery = models.ForeignKey("viewer.Gallery", blank=True, null=True, on_delete=models.SET_NULL)
    title = models.CharField(max_length=500, blank=True, null=True)
    title_jpn = models.CharField(max_length=500, blank=True, null=True, default="")
    zipped = models.FileField(verbose_name="File", upload_to=archive_path_handler, max_length=500, storage=fs)
    original_filename = models.CharField("Original Filename", max_length=500, blank=True, null=True)
    crc32 = models.CharField("CRC32", max_length=10, blank=True)
    match_type = models.CharField("Match type", max_length=40, blank=True, null=True, default="")
    filesize = models.BigIntegerField("Size", blank=True, null=True)
    filecount = models.IntegerField("File count", blank=True, null=True)
    public_date = models.DateTimeField(blank=True, null=True)
    create_date = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True, blank=True, null=True)
    user = models.ForeignKey(User, default=1, on_delete=models.SET_NULL, null=True)
    source_type = models.CharField("Source type", max_length=50, blank=True, null=True, default="")
    reason = models.CharField("Reason", max_length=200, blank=True, null=True, default="backup")
    public = models.BooleanField(default=False)
    thumbnail_height = models.PositiveIntegerField(blank=True, null=True)
    thumbnail_width = models.PositiveIntegerField(blank=True, null=True)
    thumbnail = models.ImageField(
        blank=True,
        upload_to=thumb_path_handler,
        default="",
        max_length=500,
        height_field="thumbnail_height",
        width_field="thumbnail_width",
    )
    possible_matches: models.ManyToManyField = models.ManyToManyField(
        "viewer.Gallery",
        related_name="possible_matches",
        blank=True,
        default="",
        through="ArchiveMatches",
        through_fields=("archive", "gallery"),
    )
    extracted = models.BooleanField(default=False)
    binned = models.BooleanField(default=False)
    file_deleted = models.BooleanField(default=False)

    tags: 'models.ManyToManyField[Tag, models.Model]' = models.ManyToManyField(
        "viewer.Tag", related_name="archive_tags", blank=True, through="ArchiveTag", through_fields=("archive", "tag")
    )

    alternative_sources: models.ManyToManyField = models.ManyToManyField(
        "viewer.Gallery", related_name="alternative_sources", blank=True, default=""
    )

    details = models.TextField(blank=True, null=True, default="")

    origin = models.SmallIntegerField(choices=ORIGIN_CHOICES, db_index=True, default=ORIGIN_DEFAULT)

    history = HistoricalRecords(
        excluded_fields=[
            "origin",
            "thumbnail",
            "thumbnail_height",
            "thumbnail_width",
            "last_modified",
            "create_date",
            "original_filename",
        ]
    )

    objects = ArchiveManager()

    class Meta:
        es_index_name = settings.ES_INDEX_NAME
        es_mapping = {
            "properties": {
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
                "public_date": {"type": "date"},
                "original_date": {"type": "date"},
                "source_type": {"type": "keyword"},
                "reason": {"type": "keyword"},
                "public": {"type": "boolean"},
                "category": {"type": "keyword"},
                "thumbnail": {"type": "text", "index": False},
            }
        }
        permissions = (
            ("publish_archive", "Can publish available archives"),
            ("manage_archive", "Can manage available archives"),
            ("mark_archive", "Can mark available archives"),
            ("view_marks", "Can view archive marks"),
            ("edit_system_marks", "Can edit system archive marks"),
            ("match_archive", "Can match archives"),
            ("update_metadata", "Can update metadata"),
            ("recalc_fileinfo", "Can recalculate file info"),
            ("upload_with_metadata_archive", "Can upload a file with an associated metadata source"),
            ("expand_archive", "Can extract and reduce archives"),
            ("compare_archives", "Can compare archives based on different algorithms"),
            ("recycle_archive", "Can utilize the Archive Recycle Bin"),
            ("archive_internal_info", "Can see selected internal Archive information"),
            ("mark_similar_archive", "Can run the similar Archives process"),
            ("read_archive_change_log", "Can read the Archive change log"),
            ("modify_archive_tools", "Can use tools that modify the underlying file"),
        )

        indexes = [
            models.Index(
                F("create_date").desc(nulls_last=True),
                F("public").asc(nulls_last=True),
                "binned",
                name="archive_pub_binned",
            ),
            models.Index(F("create_date").desc(nulls_last=True), "binned", name="archive_binned"),
            models.Index("binned", name="archive_binned_only"),
            models.Index(
                F("public_date").desc(nulls_last=True),
                F("public").asc(nulls_last=True),
                "binned",
                name="archive_pub2_binned",
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

    def get_es_title(self) -> str:
        return self.best_title

    def get_es_title_complete(self) -> DataDict:
        title_input = []
        if self.title:
            title_input.append(self.title)
        if self.title_jpn:
            title_input.append(self.title_jpn)
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

    def get_es_category(self) -> str | None:
        data = None
        if self.gallery:
            data = self.gallery.category
        return data

    def get_es_image_count(self) -> int | None:
        return self.filecount

    def get_es_original_date(self) -> str | None:
        data = None
        if self.gallery and self.gallery.posted:
            data = self.gallery.posted.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
        return data

    def get_es_public_date(self) -> str | None:
        if self.public_date:
            return self.public_date.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
        else:
            return None

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

    def __str__(self) -> str:
        return self.title or self.title_jpn or ""

    def get_absolute_url(self) -> str:
        return reverse("viewer:archive", args=[str(self.id)])

    @property
    def pretty_name(self) -> str:
        if not self.zipped.name:
            return ''
        return "{0}{1}".format(
            quote(replace_illegal_name(self.title or self.title_jpn or self.zipped.name)),
            os.path.splitext(self.zipped.name)[1],
        )

    @property
    def best_title(self) -> str:
        if self.title:
            return self.title
        elif self.title_jpn:
            return self.title_jpn
        return EMPTY_TITLE

    def filename(self) -> str:
        if not self.zipped.name:
            return ''
        return os.path.basename(self.zipped.name)

    def tags_str(self) -> str:
        lst = [str(x) for x in self.tags.all()]
        return ", ".join(lst)

    def tag_list(self) -> list[str]:
        lst = [str(x) for x in self.tags.all()]
        return lst

    def tag_list_sorted(self) -> list[str]:
        return sort_tags_str(self.tags.all())

    def tag_lists(self) -> SortedTagList:
        return sort_tags(self.tags.all())

    def gallery_tag_lists(self) -> SortedTagList:
        return sort_tags(self.tags.exclude(archivetag__origin=ArchiveTag.ORIGIN_USER))

    def custom_tag_lists(self) -> SortedTagList:
        return sort_tags(self.tags.filter(archivetag__origin=ArchiveTag.ORIGIN_USER))

    def regular_and_custom_tag_lists(self) -> tuple[SortedTagList, SortedTagList]:
        all_tags = self.archivetag_set.all()
        regular_tags = [x.tag for x in all_tags if x.origin == ArchiveTag.ORIGIN_SYSTEM]
        custom_tags = [x.tag for x in all_tags if x.origin == ArchiveTag.ORIGIN_USER]
        return sort_tags(regular_tags), sort_tags(custom_tags)

    def custom_tags(self) -> QuerySet[Tag]:
        return self.tags.filter(archivetag__origin=ArchiveTag.ORIGIN_USER)

    def is_recycled(self) -> bool:
        return self.binned

    def is_file_deleted(self) -> bool:
        return self.file_deleted

    def delete_text_report(self) -> str:
        data: dict[str, typing.Any] = {}
        if self.details:
            data["details"] = self.details
        if self.reason:
            data["reason"] = self.reason
        if self.filecount:
            data["filecount"] = self.filecount
        if self.title:
            data["title"] = self.title
        data["binned"] = self.binned
        if self.binned:
            data["binned_reason"] = self.recycle_entry.reason

        try:
            for count, manage_entry in enumerate(self.manage_entries.all(), start=1):
                mark_name = "mark_{}".format(count)
                data[mark_name] = {}
                if manage_entry.mark_user:
                    data[mark_name]["mark_user"] = (
                        "ServiceAccount" if manage_entry.mark_user.pk == 1 else str(manage_entry.mark_user)
                    )
                if manage_entry.mark_reason:
                    data[mark_name]["mark_reason"] = manage_entry.mark_reason
                if manage_entry.mark_comment:
                    data[mark_name]["mark_comment"] = manage_entry.mark_comment
        except ArchiveManageEntry.DoesNotExist:
            pass
        if not data:
            return ""
        return json.dumps(data, ensure_ascii=False)

    def check_and_convert_to_zip(self) -> tuple[str, int]:
        if os.path.isfile(self.zipped.path):
            extension, result = check_and_convert_to_zip(
                self.zipped.path, settings.CRAWLER_SETTINGS.temp_directory_path
            )
            return extension, result
        else:
            return "unknown", 0

    def set_public(self, reason: str = "", skip_zip_check: bool = False) -> bool:

        if not os.path.isfile(self.zipped.path) or not self.crc32:
            return False
        activity_cb = getattr(self, "_activity_callback", None)
        if callable(activity_cb):
            activity_cb()
        self.public = True
        self.public_date = django_tz.now()
        self.generate_image_set(skip_zip_check=skip_zip_check)
        if callable(activity_cb):
            activity_cb()
        self.generate_thumbnails(skip_zip_check=True)
        if callable(activity_cb):
            activity_cb()
        # Only calculate if all images are sha1 null, (no calc in process)
        if self.image_set.filter(sha1__isnull=False).count() == 0:
            self.calculate_sha1_and_data_for_images(skip_zip_check=True)
        if callable(activity_cb):
            activity_cb()
        if reason:
            self.reason = reason
        self.simple_save()
        if self.gallery:
            if reason:
                self.gallery.reason = reason
            self.gallery.public = True
            self.gallery.save()
        # Save instead of update to trigger save method stuff (index)
        for alt_gallery in self.alternative_sources.all():
            alt_gallery.public = True
            alt_gallery.save()
        return True

    def set_private(self, reason: str = "") -> None:

        if not os.path.isfile(self.zipped.path) or not self.crc32:
            return
        self.public = False
        self.public_date = None
        if reason:
            self.reason = reason
        self.simple_save()
        if self.gallery:
            if reason:
                self.gallery.reason = reason
            self.gallery.public = False
            self.gallery.save()
        # Save instead of update to trigger save method stuff (index)
        for alt_gallery in self.alternative_sources.all():
            alt_gallery.public = False
            alt_gallery.save()

    def delete_all_files(self, create_mark: bool = False, preserve_image_data: bool = False) -> None:

        results = self.image_set.filter(extracted=True)

        if results:
            for img in results:
                img.image.delete(save=False)
                img.thumbnail.delete(save=False)
        self.thumbnail.delete(save=False)
        self.zipped.delete(save=False)
        if not preserve_image_data:
            self.image_set.all().delete()
        self.extracted = False
        if create_mark:
            manager_entry, _ = ArchiveManageEntry.objects.update_or_create(
                archive=self,
                mark_reason="deleted_file",
                defaults={
                    "mark_comment": "This Archive's file has been deleted",
                    "mark_priority": 5.0,
                    "mark_check": True,
                    "origin": ArchiveManageEntry.ORIGIN_SYSTEM,
                },
            )
        self.file_deleted = True
        self.simple_save()

    def delete_files_but_archive(self) -> None:

        results = self.image_set.filter(extracted=True)

        if results:
            for img in results:
                img.image.delete(save=False)
                img.thumbnail.delete(save=False)
        self.thumbnail.delete(save=False)
        self.image_set.all().delete()
        self.extracted = False
        self.simple_save()

    def get_link(self) -> str:
        if self.gallery:
            return self.gallery.get_link()
        else:
            return ""

    def create_new_archive_ordered_by_sha1(self, sha1s: list[str]) -> "tuple[Archive | None, str]":
        from viewer.services.archive_cloning import ArchiveCloningService

        return ArchiveCloningService.create_new_archive_ordered_by_sha1(self, sha1s)

    def clone_archive_plus(
        self, sha1s: list[str] | None = None, image_tool: "CloningImageToolSettings | None" = None
    ) -> "tuple[Archive | None, str]":
        from viewer.services.archive_cloning import ArchiveCloningService

        return ArchiveCloningService.clone_archive_plus(self, sha1s, image_tool)

    def split_archive(
        self, split_data: list[tuple[int, int, str | None, bool]], split_from_nested: bool = False
    ) -> "tuple[list[Archive] | None, str]":
        from viewer.services.archive_cloning import ArchiveCloningService

        return ArchiveCloningService.split_archive(self, split_data, split_from_nested)

    def get_available_thumbnail_plus_size(self) -> tuple[str, int | None, int | None]:
        from viewer.services.archive_thumbnail import ArchiveThumbnailService

        return ArchiveThumbnailService.get_available_thumbnail_plus_size(self)

    def calculate_sha1_and_data_for_images(
        self,
        process_image_data: bool = True,
        process_other_data: bool = True,
        process_archive_statistics: bool = True,
        skip_zip_check: bool = False,
    ) -> bool:
        from viewer.services.archive_hashing import ArchiveHashingService

        return ArchiveHashingService.calculate_sha1_and_data_for_images(
            self,
            process_image_data=process_image_data,
            process_other_data=process_other_data,
            process_archive_statistics=process_archive_statistics,
            skip_zip_check=skip_zip_check,
        )

    def hash_images_with_function(self, hashing_function: typing.Callable[[typing.IO], str]) -> dict[int, str]:
        from viewer.services.archive_hashing import ArchiveHashingService

        return ArchiveHashingService.hash_images_with_function(self, hashing_function)

    def extract_delete(self) -> None:
        from viewer.services.archive_extraction import ArchiveExtractionService

        ArchiveExtractionService.extract_delete(self)

    def extract_toggle(self, resized=True):
        """Deprecated: Use explicit extract() or reduce() to avoid race conditions."""
        warnings.warn(
            "Archive.extract_toggle is deprecated due to race conditions. Use explicit extract() or reduce() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        if self.extracted:
            return self.reduce()
        else:
            return self.extract(resized=resized)

    def reduce(self) -> bool:
        from viewer.services.archive_extraction import ArchiveExtractionService

        return ArchiveExtractionService.reduce(self)

    def extract(self, resized: bool = False) -> bool:
        from viewer.services.archive_extraction import ArchiveExtractionService

        return ArchiveExtractionService.extract(self, resized=resized)

    def generate_image_set(self, force: bool = False, ignore_files: bool = False, skip_zip_check: bool = False) -> bool:

        if not os.path.isfile(self.zipped.path):
            return False
        # large thumbnail and image set
        if force or not bool(self.image_set.all()):
            my_zip = None
            try:
                my_zip = zipfile.ZipFile(self.zipped.path, "r")
                if not skip_zip_check:
                    bad_file = my_zip.testzip()
                    if bad_file:
                        logger.error(
                            "Zip file CRC check failed on file %s for archive: %s on file: %s",
                            bad_file,
                            self,
                            self.zipped.path,
                        )
                        my_zip.close()
                        return False
            except (zipfile.BadZipFile, NotImplementedError, zlib.error) as e:
                logger.error(
                    "Error opening or testing zipfile for archive: %s on file: %s: %s",
                    self,
                    self.zipped.path,
                    e,
                )
                if my_zip:
                    my_zip.close()
                return False

            filtered_files = get_images_from_zip(my_zip)

            if ignore_files:
                self.image_set.all().delete()
            else:
                for img in self.image_set.all():
                    if img.extracted:
                        img.image.delete(save=False)
                        img.thumbnail.delete(save=False)
                    img.delete()
            if filtered_files:
                Image.objects.bulk_create(
                    [
                        Image(archive=self, archive_position=count, position=count, image=None)
                        for count, _ in enumerate(filtered_files, start=1)
                    ]
                )

            my_zip.close()

        return True

    def fix_image_positions(self) -> None:
        image_set_present = bool(self.image_set.all())
        # large thumbnail and image set
        if image_set_present:
            for count, image in enumerate(self.image_set.all(), start=1):
                image.position = count
                # image.archive_position = count
                image.save()

    def delete(self, *args: typing.Any, **kwargs: typing.Any) -> tuple[int, dict[str, int]]:
        prev_pk = self.pk
        deleted = super(Archive, self).delete(*args, **kwargs)
        if settings.ES_CLIENT and settings.ES_AUTOREFRESH and prev_pk is not None:
            es_index = getattr(self._meta, "es_index_name")

            def _delete_from_es() -> None:
                if not settings.ES_CLIENT:
                    return
                try:
                    settings.ES_CLIENT.options(request_timeout=30).delete(
                        index=es_index, id=str(prev_pk), refresh=True
                    )
                except elasticsearch.exceptions.NotFoundError:
                    pass

            transaction.on_commit(_delete_from_es)
        return deleted

    def update_index(self) -> None:
        if settings.ES_CLIENT and settings.ES_AUTOREFRESH and self.pk is not None:
            if (settings.ES_ONLY_INDEX_PUBLIC and self.public) or not settings.ES_ONLY_INDEX_PUBLIC:
                payload = self.es_repr()
                del payload["_id"]
                es_index = getattr(self._meta, "es_index_name")
                try:
                    settings.ES_CLIENT.update(
                        index=es_index,
                        id=str(self.pk),
                        refresh=True,
                        doc=payload,
                    )
                except elasticsearch.exceptions.NotFoundError:
                    settings.ES_CLIENT.create(
                        index=es_index,
                        id=str(self.pk),
                        refresh=True,
                        document=payload,
                    )

    def schedule_index_update(self) -> None:
        if not (settings.ES_CLIENT and settings.ES_AUTOREFRESH):
            return
        if settings.ES_ONLY_INDEX_PUBLIC and not self.public:
            return
        if self.pk is None:
            return
        conn = transaction.get_connection()
        key = (self._meta.label, self.pk)
        if conn.in_atomic_block and any(
            getattr(func, "_es_index_key", None) == key for _, func, _ in conn.run_on_commit
        ):
            return

        def _on_commit() -> None:
            self.update_index()

        setattr(_on_commit, "_es_index_key", key)
        transaction.on_commit(_on_commit)

    def simple_save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super(Archive, self).save(*args, **kwargs)
        self.schedule_index_update()

    def save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super(Archive, self).save(*args, **kwargs)
        self.schedule_index_update()

    def ingest(self, save: bool = True) -> bool:
        from viewer.services.archive_ingestion import ArchiveIngestionService

        return ArchiveIngestionService.ingest(self, save=save)

    def sync_gallery_metadata(self) -> None:
        from viewer.services.archive_ingestion import ArchiveIngestionService

        ArchiveIngestionService.sync_gallery_metadata(self)

    def create_mark_if_parent_gallery(self):
        if (
            self.gallery
            and self.gallery.parent_gallery
            and (
                self.gallery.parent_gallery.archive_set.filter(binned=True).count() > 0
                or self.gallery.parent_gallery.alternative_sources.filter(binned=True).count() > 0
            )
        ):
            mark_comment = (
                "Linked gallery: (special-link):({})({}), has a parent gallery: (special-link):({})({})".format(
                    self.gallery.id,
                    self.gallery.get_absolute_url(),
                    self.gallery.parent_gallery.id,
                    self.gallery.parent_gallery.get_absolute_url(),
                )
            )
            manager_entry, _ = ArchiveManageEntry.objects.update_or_create(
                archive=self,
                mark_reason="gallery_with_parent",
                defaults={
                    "mark_comment": mark_comment,
                    "mark_priority": 1.0,
                    "mark_check": True,
                    "origin": ArchiveManageEntry.ORIGIN_SYSTEM,
                },
            )
        else:
            ArchiveManageEntry.objects.filter(
                archive=self, mark_reason="gallery_with_parent", mark_user__isnull=True
            ).delete()

    def create_or_update_thumbnail_hash(self, algorithm: str):

        if self.thumbnail:
            archive_type = ContentType.objects.get_for_model(Archive)

            hash_result = CompareObjectsService.hash_thumbnail(self.thumbnail.path, algorithm)
            if hash_result:
                hash_object, _ = ItemProperties.objects.update_or_create(
                    content_type=archive_type,
                    object_id=self.pk,
                    tag="hash-compare",
                    name=algorithm,
                    defaults={"value": hash_result},
                )

    def fill_other_file_data(self, other_file_datas: list[ArchiveGenericFile] | None):
        self.archivefileentry_set.all().delete()
        if other_file_datas:
            ArchiveFileEntry.objects.bulk_create(
                [
                    ArchiveFileEntry(
                        archive=self,
                        archive_position=other_file_data.position,
                        position=other_file_data.position,
                        file_name=other_file_data.file_name,
                        file_size=other_file_data.file_size,
                        file_type=os.path.splitext(other_file_data.file_name)[1],
                    )
                    for other_file_data in other_file_datas
                ]
            )

    def create_thumbnail_from_io_image(self, current_img):
        from viewer.services.archive_thumbnail import ArchiveThumbnailService

        ArchiveThumbnailService.create_thumbnail_from_io_image(self, current_img)

    def rename_zipped_tail(self, new_file_name: str) -> bool:
        if not self.zipped.name:
            return False
        initial_path = self.zipped.path
        current_head, current_tail = os.path.split(self.zipped.name)
        new_file_name = os.path.join(current_head, new_file_name)
        if os.path.exists(new_file_name):
            return False
        self.zipped.name = new_file_name
        if os.path.exists(initial_path):
            shutil.move(initial_path, pjoin(settings.MEDIA_ROOT, new_file_name))
        self.simple_save()
        return True

    def rename_zipped_pathname(self, new_file_name: str) -> bool:
        initial_path = self.zipped.path
        if os.path.exists(new_file_name):
            return False
        self.zipped.name = new_file_name
        if os.path.exists(initial_path):
            shutil.move(initial_path, pjoin(settings.MEDIA_ROOT, new_file_name))
        self.simple_save()
        return True

    def generate_possible_matches(
        self, cutoff: float = 0.4, max_matches: int = 20, clear_title: bool = False, provider_filter: str = ""
    ) -> None:
        if not self.match_type == "non-match":
            return

        if not self.zipped.name:
            return

        galleries_title_id = []

        if provider_filter:
            galleries = Gallery.objects.eligible_for_use(provider__contains=provider_filter)
        else:
            galleries = Gallery.objects.eligible_for_use()
        for gallery in galleries:
            if gallery.title:
                galleries_title_id.append((replace_illegal_name(gallery.title), gallery.pk))
            if gallery.title_jpn:
                galleries_title_id.append((replace_illegal_name(gallery.title_jpn), gallery.pk))

        if provider_filter:
            matchers = settings.PROVIDER_CONTEXT.get_matchers(
                settings.CRAWLER_SETTINGS, filter_name="{}_title".format(provider_filter), force=True
            )
            if matchers:
                adj_title = matchers[0][0].format_to_compare_title(self.zipped.name)
            else:
                adj_title = get_title_from_path(self.zipped.name)
        else:
            adj_title = get_title_from_path(self.zipped.name)

        if clear_title:
            adj_title = clean_title(adj_title)

        similar_list = get_list_closer_text_from_list(adj_title, galleries_title_id, cutoff, max_matches)

        if similar_list is not None:

            self.possible_matches.clear()

            for similar in similar_list:
                gallery = Gallery.objects.get(pk=similar[1])

                ArchiveMatches.objects.create(
                    archive=self, gallery=gallery, match_type="title", match_accuracy=similar[2]
                )

        if self.filesize is None or self.filesize <= 0:
            return
        galleries_same_size = Gallery.objects.filter(filesize=self.filesize)
        if galleries_same_size.exists():

            for similar_gallery in galleries_same_size:
                gallery = Gallery.objects.get(pk=similar_gallery.pk)

                ArchiveMatches.objects.create(archive=self, gallery=gallery, match_type="size", match_accuracy=1)

    def create_marks_for_similar_archives(self, excluded_archives: list[int] | None = None, use_recycled_archives: bool = False) -> None:
        if self.crc32:
            if use_recycled_archives:
                similar_crc32 = Archive.objects.filter(crc32=self.crc32).exclude(pk=self.pk)
            else:
                similar_crc32 = Archive.objects.filter(crc32=self.crc32).exclude(pk=self.pk).exclude(binned=True)

            # 4.2 means high level priority
            if similar_crc32.count() > 0:
                mark_comment = "\n".join(["special-link:" + x.get_absolute_url() for x in similar_crc32])

                manager_entry, _ = ArchiveManageEntry.objects.update_or_create(
                    archive=self,
                    mark_reason="same_crc32",
                    defaults={
                        "mark_comment": mark_comment,
                        "mark_priority": 4.2,
                        "mark_check": True,
                        "origin": ArchiveManageEntry.ORIGIN_SYSTEM,
                    },
                )
            else:
                ArchiveManageEntry.objects.filter(
                    archive=self, mark_reason="same_crc32", mark_user__isnull=True
                ).delete()

        if self.filesize and self.filecount:
            if use_recycled_archives:
                similar_fileinfo = (
                    Archive.objects.filter(filesize=self.filesize, filecount=self.filecount)
                    .exclude(pk=self.pk)
                )
            else:
                similar_fileinfo = (
                    Archive.objects.filter(filesize=self.filesize, filecount=self.filecount)
                    .exclude(pk=self.pk)
                    .exclude(binned=True)
                )

            # 4.1 means high level priority
            if similar_fileinfo.count() > 0:
                mark_comment = "\n".join(["special-link:" + x.get_absolute_url() for x in similar_fileinfo])

                manager_entry, _ = ArchiveManageEntry.objects.update_or_create(
                    archive=self,
                    mark_reason="same_file_info",
                    defaults={
                        "mark_comment": mark_comment,
                        "mark_priority": 4.1,
                        "mark_check": True,
                        "origin": ArchiveManageEntry.ORIGIN_SYSTEM,
                    },
                )
            else:
                ArchiveManageEntry.objects.filter(
                    archive=self, mark_reason="same_file_info", mark_user__isnull=True
                ).delete()

        if self.title:
            if use_recycled_archives:
                similar_title = Archive.objects.filter(title=self.title).exclude(pk=self.pk)
            else:
                similar_title = Archive.objects.filter(title=self.title).exclude(pk=self.pk).exclude(binned=True)


            if similar_title.count() > 0:

                mark_comment = "\n".join(["special-link:" + x.get_absolute_url() for x in similar_title])

                # 0.5 means low level priority
                manager_entry, _ = ArchiveManageEntry.objects.update_or_create(
                    archive=self,
                    mark_reason="same_title",
                    defaults={
                        "mark_comment": mark_comment,
                        "mark_priority": 0.5,
                        "mark_check": True,
                        "origin": ArchiveManageEntry.ORIGIN_SYSTEM,
                    },
                )
            else:
                ArchiveManageEntry.objects.filter(
                    archive=self, mark_reason="same_title", mark_user__isnull=True
                ).delete()

        self.create_sha1_similarity_mark(excluded_archives, use_recycled_archives=use_recycled_archives)
        self.create_phash_similarity_mark(excluded_archives, use_recycled_archives=use_recycled_archives, match_similar_tags=settings.CRAWLER_SETTINGS.auto_match_phash_tags_archives)
        self.create_phash_gallery_similarity_mark()
        self.create_wanted_image_similarity_mark()

    def create_sha1_similarity_mark(self, excluded_archives: list[int] | None = None, use_recycled_archives: bool = False) -> None:
        images_from_archive = self.image_set.filter(sha1__isnull=False)

        if images_from_archive:
            images_sha1 = images_from_archive.values_list("sha1", flat=True)

            if use_recycled_archives:
                similar_images = (
                    Image.objects.filter(sha1__in=images_sha1)
                    .exclude(pk__in=images_from_archive)
                    # Remove this filter, does not make sense to remove archives with fewer images.
                    # .filter(archive__filecount__gte=self.filecount)
                    .distinct()
                )
            else:
                similar_images = (
                    Image.objects.filter(sha1__in=images_sha1)
                    .exclude(pk__in=images_from_archive)
                    # Remove this filter, does not make sense to remove archives with fewer images.
                    # .filter(archive__filecount__gte=self.filecount)
                    .exclude(archive__binned=True)
                    .distinct()
                )

            if excluded_archives:
                similar_images = similar_images.exclude(archive__pk__in=excluded_archives)

            if similar_images:
                similar_images_pk = similar_images.values_list("pk", flat=True)
                archives_sha1 = Archive.objects.filter(image__in=similar_images_pk).distinct()
                per_archives_comment = []

                mark_priority = 0.0

                for archive_sha1 in archives_sha1:
                    other_images_sha1 = archive_sha1.image_set.filter(sha1__isnull=False).values_list("sha1", flat=True)
                    found_sha1 = list((Counter(images_sha1) & Counter(other_images_sha1)).elements())

                    if self.filecount:
                        possible_priority = (len(found_sha1) / self.filecount) * 4.0
                        if possible_priority > mark_priority:
                            mark_priority = possible_priority
                    per_archives_comment.append(
                        (
                            len(found_sha1),
                            "(special-link):({})({}): {} matches, other has {}, (special-link):(compare)({})".format(
                                archive_sha1.pk,
                                archive_sha1.get_absolute_url(),
                                len(found_sha1),
                                archive_sha1.filecount,
                                reverse("viewer:compare-archives-viewer")
                                + "?archives={}&archives={}".format(self.pk, archive_sha1.pk),
                            ),
                        )
                    )

                per_archives_comment = sorted(per_archives_comment, key=itemgetter(0), reverse=True)

                if per_archives_comment:

                    mark_comment = "\n".join([x[1] for x in per_archives_comment])

                    manager_entry, _ = ArchiveManageEntry.objects.update_or_create(
                        archive=self,
                        mark_reason="images_sha1_similarity",
                        defaults={
                            "mark_comment": mark_comment,
                            "mark_priority": mark_priority,
                            "mark_check": True,
                            "origin": ArchiveManageEntry.ORIGIN_SYSTEM,
                        },
                    )
                    return

        ArchiveManageEntry.objects.filter(
            archive=self, mark_reason="images_sha1_similarity", mark_user__isnull=True
        ).delete()

    def create_phash_similarity_mark(self, excluded_archives: list[int] | None = None, use_recycled_archives: bool = False, match_similar_tags: bool = False) -> None:
        images_from_archive = self.image_set.all()

        algorithm = "phash"

        image_type = ContentType.objects.get_for_model(Image)

        images_phashes = ItemProperties.objects.filter(
            content_type=image_type, object_id__in=images_from_archive, tag="hash-compare", name=algorithm
        )

        delete_old_marks = True
        delete_old_tags_marks = True

        if images_phashes:
            images_phashes_values = images_phashes.values_list("value", flat=True)

            similar_images = (
                ItemProperties.objects.filter(
                    tag="hash-compare", name=algorithm, content_type=image_type, value__in=images_phashes_values
                )
                .exclude(pk__in=images_phashes)
                .distinct()
            )

            if similar_images:
                similar_images_pk = similar_images.values_list("object_id", flat=True)
                if use_recycled_archives:
                    archives_phash = Archive.objects.filter(image__in=similar_images_pk).distinct()
                else:
                    archives_phash = Archive.objects.filter(image__in=similar_images_pk).exclude(binned=True).distinct()
                if excluded_archives:
                    archives_phash = archives_phash.exclude(pk__in=excluded_archives)
                per_archives_comment = []
                per_tag_archives_comment = []
                if match_similar_tags:
                    current_tags = self.tag_list()
                else:
                    current_tags = []

                mark_priority = 0.0

                for archive_phash in archives_phash:
                    other_images_phash = ItemProperties.objects.filter(
                        tag="hash-compare",
                        name=algorithm,
                        content_type=image_type,
                        object_id__in=archive_phash.image_set.all(),
                    ).values_list("value", flat=True)
                    found_phash = list((Counter(images_phashes_values) & Counter(other_images_phash)).elements())

                    # Special case: if all match images are 0000000000000000 or 8000000000000000 (black or white),
                    # Skip
                    if all([x == IMAGE_PHASH_BLACK for x in found_phash]) or all(
                        [x == IMAGE_PHASH_WHITE for x in found_phash]
                    ):
                        continue

                    if self.filecount:
                        possible_priority = (len(found_phash) / self.filecount) * 3.0
                        if possible_priority > mark_priority:
                            mark_priority = possible_priority
                    per_archives_comment.append(
                        (
                            len(found_phash),
                            "(special-link):({})({}): {} matches, other has {}, (special-link):(compare)({})".format(
                                archive_phash.pk,
                                archive_phash.get_absolute_url(),
                                len(found_phash),
                                archive_phash.filecount,
                                reverse("viewer:compare-archives-viewer")
                                + "?archives={}&archives={}&algos=phash".format(self.pk, archive_phash.pk),
                            ),
                        )
                    )

                    if match_similar_tags:
                        compare_tags = archive_phash.tag_list()
                        tag_comparer = ArchiveTagsComparer(self, archive_phash, current_tags, compare_tags, found_phash)

                        translation_of_raw_anthology = tag_comparer.translation_of_raw_anthology()
                        translation_of_raw_book = tag_comparer.translation_of_raw_book()

                        if translation_of_raw_anthology:
                            per_tag_archives_comment.append(
                                (
                                    archive_phash.pk,
                                    "(special-link):({})({}): Translation of chapter in {}, (special-link):(compare)({})".format(
                                        archive_phash.pk,
                                        archive_phash.get_absolute_url(),
                                        "magazine" if tag_comparer.compare_is_magazine else "tankoubon",
                                        reverse("viewer:compare-archives-viewer")
                                        + "?archives={}&archives={}&algos=phash".format(self.pk, archive_phash.pk),
                                    ),
                                )
                            )

                        if translation_of_raw_book:
                            per_tag_archives_comment.append(
                                (
                                    archive_phash.pk,
                                    "(special-link):({})({}): Translation of {}, (special-link):(compare)({})".format(
                                        archive_phash.pk,
                                        archive_phash.get_absolute_url(),
                                        self.get_es_category(),
                                        reverse("viewer:compare-archives-viewer")
                                        + "?archives={}&archives={}&algos=phash".format(self.pk, archive_phash.pk),
                                    ),
                                )
                            )

                per_archives_comment = sorted(per_archives_comment, key=itemgetter(0), reverse=True)

                if per_archives_comment:

                    mark_comment = "\n".join([x[1] for x in per_archives_comment])

                    manager_entry, _ = ArchiveManageEntry.objects.update_or_create(
                        archive=self,
                        mark_reason="images_phash_similarity",
                        defaults={
                            "mark_comment": mark_comment,
                            "mark_priority": mark_priority,
                            "mark_check": True,
                            "origin": ArchiveManageEntry.ORIGIN_SYSTEM,
                        },
                    )
                    delete_old_marks = False

                per_tag_archives_comment = sorted(per_tag_archives_comment, key=itemgetter(0), reverse=True)

                if per_tag_archives_comment:
                    mark_comment = "\n".join([x[1] for x in per_tag_archives_comment])

                    manager_entry, _ = ArchiveManageEntry.objects.update_or_create(
                        archive=self,
                        mark_reason="phash_tags_similarity",
                        defaults={
                            "mark_comment": mark_comment,
                            "mark_priority": 0.5,
                            "mark_check": True,
                            "origin": ArchiveManageEntry.ORIGIN_SYSTEM,
                        },
                    )
                    delete_old_tags_marks = False

        if delete_old_marks:
            ArchiveManageEntry.objects.filter(
                archive=self, mark_reason="images_phash_similarity", mark_user__isnull=True
            ).delete()

        if delete_old_tags_marks:
            ArchiveManageEntry.objects.filter(
                archive=self, mark_reason="phash_tags_similarity", mark_user__isnull=True
            ).delete()

    def create_wanted_image_similarity_mark(self) -> None:
        from viewer.models.wanted import FoundWantedImageOnArchive, WantedImage
        if (
            image_processing.CAN_USE_IMAGE_MATCH
            and settings.CRAWLER_SETTINGS.auto_match_wanted_images
            and self.thumbnail
        ):

            wanted_images = WantedImage.objects.filter(active=True)

            if wanted_images:
                img_thumbnail, img_gray = image_processing.get_image_thumbnail_and_grayscale(self)

                per_archives_comment = []

                highest_priority = 0.0

                for wanted_image in wanted_images:
                    found_match, n_good_matches, im_result = image_processing.compare_wanted_with_image(
                        img_gray, img_thumbnail, wanted_image
                    )
                    if found_match:
                        if wanted_image.mark_priority > highest_priority:
                            highest_priority = wanted_image.mark_priority

                        found_image, created = FoundWantedImageOnArchive.objects.get_or_create(
                            wanted_image=wanted_image,
                            archive=self,
                            defaults={
                                "comment": str(django_tz.now())
                            },
                        )

                        if im_result is not None:
                            found_image.save_result_image(im_result)

                        per_archives_comment.append(
                            "Image: (popover-img):({})({}) found {} (popover-img):(features)({})".format(
                                wanted_image.image_name,
                                wanted_image.get_image_url(),
                                n_good_matches,
                                found_image.get_image_url(),
                            )
                        )

                if per_archives_comment:
                    mark_comment = "\n".join(per_archives_comment)

                    manager_entry, _ = ArchiveManageEntry.objects.update_or_create(
                        archive=self,
                        mark_reason="wanted_images_found",
                        defaults={
                            "mark_comment": mark_comment,
                            "mark_priority": highest_priority,
                            "mark_check": True,
                            "origin": ArchiveManageEntry.ORIGIN_SYSTEM,
                        },
                    )
                    return

        ArchiveManageEntry.objects.filter(
            archive=self, mark_reason="wanted_images_found", mark_user__isnull=True
        ).delete()

    def get_wanted_images_similarity_mark(self) -> tuple[int, list[tuple["WantedImage", int, bool, bytes | None]]]:
        from viewer.models.wanted import WantedImage

        per_wanted_data = []

        if image_processing.CAN_USE_IMAGE_MATCH and self.thumbnail:

            wanted_images = WantedImage.objects.filter(active=True)

            if wanted_images:
                img_thumbnail, img_gray = image_processing.get_image_thumbnail_and_grayscale(self)

                for wanted_image in wanted_images:
                    found_match, n_good_matches, im_result = image_processing.compare_wanted_with_image(
                        img_gray, img_thumbnail, wanted_image, skip_minimum=True
                    )
                    # if found_match:
                    if im_result is not None:
                        buffered = io.BytesIO()
                        im_result.save(buffered, format="JPEG")
                        img_str: bytes | None = base64.b64encode(buffered.getvalue())
                    else:
                        img_str = None

                    per_wanted_data.append((wanted_image, n_good_matches, found_match, img_str))

                return 0, per_wanted_data
            else:
                return -2, per_wanted_data
        else:
            return -1, per_wanted_data

    def recalc_filesize(self) -> None:
        if os.path.isfile(self.zipped.path):
            self.filesize = get_zip_filesize(self.zipped.path)
            super(Archive, self).save()

    def recalc_fileinfo(self, use_pool: bool = True) -> None:
        if os.path.isfile(self.zipped.path):
            if use_pool:
                try:
                    from core.base.process_pool import recalc_archive_fileinfo_in_pool

                    self.filesize, self.filecount, self.crc32, other_file_datas = recalc_archive_fileinfo_in_pool(
                        self.zipped.path
                    )
                    self.fill_other_file_data(other_file_datas)
                    super(Archive, self).save()
                    return
                except Exception as e:
                    logger.warning(
                        "Process pool recalc_fileinfo failed for archive %s, falling back to in-process: %s",
                        self.pk,
                        e,
                    )
            self.filesize, self.filecount, other_file_datas = get_zip_fileinfo(self.zipped.path, get_extra_data=True)
            self.fill_other_file_data(other_file_datas)
            self.crc32 = calc_crc32(self.zipped.path)
            super(Archive, self).save()

    def test_zip_file(self) -> None:
        if not os.path.exists(self.zipped.path):
            raise FileNotFoundError("Zipped file not found: {}.".format(self.zipped.path))
        my_zip = None
        try:
            my_zip = zipfile.ZipFile(self.zipped.path, "r")
            bad_files = my_zip.testzip()
            if bad_files:
                my_zip.close()
                logger.error(
                    "Zip file CRC check failed on file %s for archive: %s on file: %s",
                    bad_files,
                    self,
                    self.zipped.path,
                )
                raise zipfile.BadZipFile("Bad CRC check on files: {}.".format(bad_files))
        except (zipfile.BadZipFile, NotImplementedError, zlib.error) as e:
            if my_zip:
                my_zip.close()
            logger.error(
                "Error opening or testing zipfile for archive: %s on file: %s: %s",
                self,
                self.zipped.path,
                e,
            )
            if isinstance(e, zipfile.BadZipFile):
                raise
            raise zipfile.BadZipFile("Bad ZIP file: {}. Error: {}".format(self.zipped.path, e))

    def set_original_filename(self) -> None:
        if os.path.isfile(self.zipped.path) and self.zipped.name:
            self.original_filename = os.path.basename(self.zipped.name)
            super(Archive, self).save()

    def generate_thumbnails(self, use_pool: bool = True, skip_zip_check: bool = False) -> bool:
        from viewer.services.archive_thumbnail import ArchiveThumbnailService

        return ArchiveThumbnailService.generate_thumbnails(self, use_pool=use_pool, skip_zip_check=skip_zip_check)

    def get_image_data_from_position(self, position: int) -> bytes | None:
        from viewer.services.archive_thumbnail import ArchiveThumbnailService

        return ArchiveThumbnailService.get_image_data_from_position(self, position)

    def release_gallery(self):

        if self.gallery:
            self.gallery.archive_set.remove(self)

            ArchiveManageEntry.objects.filter(archive=self, mark_reason="wrong_file", mark_user__isnull=True).delete()

    def select_as_match(self, gallery_id: int) -> None:
        try:
            matched_gallery = Gallery.objects.get(pk=gallery_id)
        except Gallery.DoesNotExist:
            return
        self.gallery_id = matched_gallery.id

        self.set_titles_from_gallery(matched_gallery, dont_save=True)

        self.match_type = "manual:user"
        self.possible_matches.clear()
        self.simple_save()
        self.set_tags_from_gallery(matched_gallery)

        self.mark_if_different_size_from_gallery()

        if self.public:
            matched_gallery.public = True
            matched_gallery.save()

    def set_reason(self, reason: str) -> None:
        self.reason = reason

        self.simple_save()

    def mark_if_different_size_from_gallery(self):
        if (
            self.gallery
            and self.gallery.filesize is not None
            and self.gallery.filesize != 0
            and self.filesize != self.gallery.filesize
        ):
            mark_comment = (
                "Torrent downloaded has not the same file as the matched gallery"
                " (different filesize or filecount). This file must be replaced if the correct one"
                " is found."
            )
            manager_entry, _ = ArchiveManageEntry.objects.update_or_create(
                archive=self,
                mark_reason="wrong_file",
                defaults={
                    "mark_comment": mark_comment,
                    "mark_priority": 4.3,
                    "mark_check": True,
                    "origin": ArchiveManageEntry.ORIGIN_SYSTEM,
                },
            )
        else:
            ArchiveManageEntry.objects.filter(archive=self, mark_reason="wrong_file", mark_user__isnull=True).delete()

    def similar_archives(self, num_common_tags: int = 0, **kwargs: typing.Any) -> "QuerySet[Archive]":
        return Archive.objects.filter(tags__in=self.tags.all(), **kwargs)\
            .exclude(pk=self.pk)\
            .annotate(num_common_tags=Count("pk"))\
            .filter(num_common_tags__gt=num_common_tags)\
            .distinct()\
            .order_by("-num_common_tags")

    def set_tags_from_gallery(self, gallery: Gallery, preserve_custom: bool = True, force: bool = False):
        archive_option = ArchiveOption.objects.filter(archive=self).first()
        if force or not archive_option or not archive_option.freeze_tags:
            if preserve_custom:
                current_custom_tags = list(self.custom_tags())
                gallery_tags = list(gallery.tags.all())
                self.tags.set(gallery_tags + current_custom_tags)
            else:
                self.tags.set(gallery.tags.all())

    def set_titles_from_gallery(self, gallery: Gallery, force: bool = False, dont_save: bool = False):
        archive_option = ArchiveOption.objects.filter(archive=self).first()
        if force or not archive_option or not archive_option.freeze_titles:
            self.title = gallery.title
            self.title_jpn = gallery.title_jpn
            if not dont_save:
                self.simple_save()

    def do_freeze_titles(self):
        archive_option, created = ArchiveOption.objects.get_or_create(archive=self, defaults={"freeze_titles": True})
        if not created:
            archive_option.freeze_titles = True
            archive_option.save()

    def do_freeze_tags(self):
        archive_option, created = ArchiveOption.objects.get_or_create(archive=self, defaults={"freeze_tags": True})
        if not created:
            archive_option.freeze_tags = True
            archive_option.save()

    def undo_freeze_titles(self):
        archive_option = ArchiveOption.objects.filter(archive=self).first()
        if not archive_option:
            return
        archive_option.freeze_titles = False
        archive_option.save()

    def undo_freeze_tags(self):
        archive_option = ArchiveOption.objects.filter(archive=self).first()
        if not archive_option:
            return
        archive_option.freeze_tags = False
        archive_option.save()

    def move_gallery_to_alternative(self) -> None:
        """Move the current gallery to alternative_sources and release the association."""
        if self.gallery:
            # Add to alternative_sources if not already there
            if not self.alternative_sources.filter(id=self.gallery.id).exists():
                self.alternative_sources.add(self.gallery)
            # Release the current gallery association
            self.gallery = None
            self.sync_gallery_metadata()
            self.save()

    def create_phash_gallery_similarity_mark(self):
        algorithm = "phash"

        if self.gallery:
            archive_type = ContentType.objects.get_for_model(Archive)
            gallery_type = ContentType.objects.get_for_model(Gallery)

            gallery_hash_object = ItemProperties.objects.filter(
                content_type=gallery_type,
                object_id=self.gallery.pk,
                tag="hash-compare",
                name=algorithm,
            ).first()

            archive_hash_object = ItemProperties.objects.filter(
                content_type=archive_type,
                object_id=self.pk,
                tag="hash-compare",
                name=algorithm,
            ).first()

            if gallery_hash_object and archive_hash_object:
                hamming_value = hamming_distance(archive_hash_object.value, gallery_hash_object.value)

                if hamming_value > 0:
                    mark_priority = (hamming_value / len(archive_hash_object.value)) * 2.0

                    manager_entry, _ = ArchiveManageEntry.objects.update_or_create(
                        archive=self,
                        mark_reason="gallery_phash_similarity",
                        defaults={
                            "mark_comment": "Hamming distance to Gallery (thumbnail to thumbnail) (special-link):({})({}): is greater than 0 (max is 16): {}".format(
                                self.gallery.pk,
                                self.gallery.get_absolute_url(),
                                hamming_value,
                            ),
                            "mark_priority": mark_priority,
                            "mark_check": True,
                            "origin": ArchiveManageEntry.ORIGIN_SYSTEM,
                        },
                    )

        else:
            ArchiveManageEntry.objects.filter(
                archive=self, mark_reason="gallery_phash_similarity", mark_user__isnull=True
            ).delete()
            
    def move_to_recycle_bin(self, reason=""):
        if not self.is_recycled():
            with transaction.atomic():
                ArchiveRecycleEntry.objects.update_or_create(
                    archive=self,
                    defaults={
                        "reason": reason,
                        "origin": ArchiveRecycleEntry.ORIGIN_SYSTEM,
                    },
                )
                self.binned = True
                self.simple_save()
                if self.public:
                    self.set_private()



class ArchiveTag(models.Model):

    ORIGIN_SYSTEM = 1
    ORIGIN_USER = 2

    ORIGIN_CHOICES = (
        (ORIGIN_SYSTEM, "System"),
        (ORIGIN_USER, "User"),
    )

    archive = models.ForeignKey(Archive, on_delete=models.CASCADE)
    tag = models.ForeignKey("viewer.Tag", on_delete=models.CASCADE)
    origin = models.SmallIntegerField(choices=ORIGIN_CHOICES, db_index=True, default=ORIGIN_SYSTEM, blank=True)

    class Meta:
        verbose_name_plural = "Archive Tags"
        ordering = ["-origin"]



class ArchiveManageEntry(models.Model):

    ORIGIN_SYSTEM = 1
    ORIGIN_USER = 2

    ORIGIN_CHOICES = (
        (ORIGIN_SYSTEM, "System"),
        (ORIGIN_USER, "User"),
    )

    class StatusChoices(models.IntegerChoices):
        NORMAL = 1, _("Normal")
        NOT_INDEXED = 5, _("Not Indexed")

    class Meta:
        verbose_name_plural = "Archive manage entries"
        ordering = ["-mark_priority"]

    archive = models.ForeignKey(Archive, on_delete=models.CASCADE, related_name="manage_entries")
    mark_check = models.BooleanField(default=False)
    mark_priority = models.FloatField(blank=True, null=True, default=1.0)
    mark_reason = models.CharField(blank=True, default="", max_length=200)
    mark_user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="archive_entry_mark"
    )
    mark_comment = models.TextField(blank=True, default="")
    mark_extra = models.CharField(blank=True, default="", max_length=200)
    mark_date = models.DateTimeField(default=django_tz.now)
    origin = models.SmallIntegerField(choices=ORIGIN_CHOICES, db_index=True, default=ORIGIN_SYSTEM, blank=True)
    status = models.SmallIntegerField(choices=StatusChoices, db_index=True, default=StatusChoices.NORMAL)

    resolve_check = models.BooleanField(default=False)
    resolve_user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="archive_entry_resolver"
    )
    resolve_comment = models.TextField(blank=True, null=True, default="")

    def is_indexed(self):
        return self.status == self.StatusChoices.NORMAL

    def mark_as_json_string(self) -> str:
        data = {
            "mark_user": self.mark_user.username if self.mark_user else None,
            "mark_reason": self.mark_reason,
            "mark_comment": self.mark_comment,
            "mark_date": self.mark_date.timestamp(),
        }
        return json.dumps(data)


class ArchiveOption(models.Model):

    archive = models.OneToOneField(Archive, on_delete=models.CASCADE)
    freeze_titles = models.BooleanField(default=False, blank=True)
    freeze_tags = models.BooleanField(default=False, blank=True)


class ArchiveRecycleEntry(models.Model):

    ORIGIN_SYSTEM = 1
    ORIGIN_USER = 2

    ORIGIN_CHOICES = (
        (ORIGIN_SYSTEM, "System"),
        (ORIGIN_USER, "User"),
    )

    class Meta:
        verbose_name_plural = "Archive recycle entries"
        ordering = ["-date_deleted"]

    archive = models.OneToOneField(Archive, on_delete=models.CASCADE, primary_key=True, related_name="recycle_entry")
    reason = models.CharField(blank=True, default="", max_length=200)
    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="archive_recycle_entry"
    )
    comment = models.TextField(blank=True, default="")
    date_deleted = models.DateTimeField(default=django_tz.now)
    origin = models.SmallIntegerField(choices=ORIGIN_CHOICES, db_index=True, default=ORIGIN_SYSTEM, blank=True)

    def as_json_string(self) -> str:
        data = {
            "user": self.user.username if self.user else None,
            "reason": self.reason,
            "comment": self.comment,
            "date": self.date_deleted.timestamp(),
        }
        return json.dumps(data)



class ArchiveFileEntry(models.Model):
    archive = models.ForeignKey(Archive, on_delete=models.CASCADE)
    archive_position = models.PositiveIntegerField(default=1)
    position = models.PositiveIntegerField(default=1)
    sha1 = models.CharField(max_length=50, blank=True, null=True)
    file_name = models.CharField(max_length=500, blank=True, default="")
    file_type = models.CharField(max_length=40, blank=True, default="")
    file_size = models.PositiveIntegerField(default=0)
    description = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        verbose_name_plural = "Archive file entries"
        ordering = ["-position"]


class ArchiveStatistics(models.Model):
    archive = models.ForeignKey(Archive, on_delete=models.CASCADE)
    filesize_average = models.FloatField(blank=True, null=True)
    height_mode = models.PositiveIntegerField(blank=True, null=True)
    width_mode = models.PositiveIntegerField(blank=True, null=True)
    height_average = models.FloatField(blank=True, null=True)
    width_average = models.FloatField(blank=True, null=True)
    height_stddev = models.FloatField(blank=True, null=True)
    width_stddev = models.FloatField(blank=True, null=True)
    image_mode_mode = models.CharField(max_length=50, blank=True, null=True)
    file_type_mode = models.CharField(max_length=40, blank=True, null=True)
    file_type_match = models.FloatField(blank=True, null=True)
    is_horizontal_mode = models.BooleanField(default=False)

    class Meta:
        verbose_name_plural = "Archive file statistics"
        # ordering = ["-position"]


def upload_imgpath(instance: "Archive", filename: str) -> str:
    file_name, file_extension = os.path.splitext(filename)
    return "images/extracted/archive_{id}/full/{uuid}{extension}".format(
        uuid=uuid.uuid4(),
        id=instance.id,
        extension=file_extension,
    )


def upload_imgpath_handler(instance: "Image", filename: str) -> str:
    file_name, file_extension = os.path.splitext(filename)
    return "images/extracted/archive_{id}/full/{uuid}{extension}".format(
        uuid=uuid.uuid4(),
        id=instance.archive.id,
        extension=file_extension,
    )


def upload_thumbpath_handler(instance: "Image", filename: str) -> str:
    file_name, file_extension = os.path.splitext(filename)
    return "images/extracted/archive_{id}/thumb/{uuid}{extension}".format(
        uuid=uuid.uuid4(),
        id=instance.archive.id,
        extension=file_extension,
    )


class Image(models.Model):
    image = models.ImageField(
        upload_to=upload_imgpath_handler,
        blank=True,
        null=True,
        max_length=500,
        height_field="image_height",
        width_field="image_width",
    )
    image_name = models.CharField(max_length=500, blank=True, null=True)
    image_height = models.PositiveIntegerField(blank=True, null=True)
    image_width = models.PositiveIntegerField(blank=True, null=True)
    original_height = models.PositiveIntegerField(null=True)
    original_width = models.PositiveIntegerField(null=True)
    image_format = models.CharField(max_length=50, blank=True, null=True)
    image_mode = models.CharField(max_length=50, blank=True, null=True)
    image_size = models.PositiveIntegerField(null=True)
    thumbnail_height = models.PositiveIntegerField(blank=True, null=True)
    thumbnail_width = models.PositiveIntegerField(blank=True, null=True)
    thumbnail = models.ImageField(
        upload_to=upload_thumbpath_handler,
        blank=True,
        null=True,
        max_length=500,
        height_field="thumbnail_height",
        width_field="thumbnail_width",
    )
    archive = models.ForeignKey(Archive, on_delete=models.CASCADE)
    archive_position = models.PositiveIntegerField(default=1)
    position = models.PositiveIntegerField(default=1)
    sha1 = models.CharField(max_length=50, blank=True, null=True)
    extracted = models.BooleanField(default=False)

    class Meta:
        ordering = ["position"]
        indexes = [
            models.Index(fields=["sha1"], name="image_sha1"),
        ]

    def delete_plus_files(self) -> None:
        self.image.delete(save=False)
        self.thumbnail.delete(save=False)
        self.delete()

    def __str__(self) -> str:
        if self.image:
            return self.image.path
        else:
            return str(self.archive.title) + ", position: " + str(self.position)

    def dump_image(self, request: HttpRequest) -> DataDict:
        image_dict: dict[str, typing.Any] = {
            "position": self.position,
            "url": request.build_absolute_uri(self.image.url),
            "is_horizontal": (
                self.image_width / self.image_height > 1 if self.image_width and self.image_height else False
            ),
            "width": self.image_width,
            "height": self.image_height,
        }

        return image_dict

    def fetch_image_data(self, use_original_image=False) -> bytes | None:
        if self.extracted:
            if use_original_image:
                with self.image.open() as fp:
                    image_data = fp.read()
                    return image_data
            else:
                with self.thumbnail.open() as fp:
                    image_data = fp.read()
                    return image_data
        else:
            full_image = self.archive.get_image_data_from_position(self.position)
            if use_original_image:
                return full_image
            else:
                if full_image:
                    im: PImage.Image | ImageFile.ImageFile = PImage.open(io.BytesIO(full_image))
                    if im.mode != "RGB":
                        im = im.convert("RGB")
                    im.thumbnail((250, 362.5), PImage.Resampling.LANCZOS)
                    img_bytes = io.BytesIO()
                    im.save(img_bytes, format="JPEG")
                    return img_bytes.getvalue()
                else:
                    return None

    def create_or_update_thumbnail_hash(self, algorithm: str):
        image_type = ContentType.objects.get_for_model(Image)

        image_data = self.fetch_image_data()

        if image_data:

            hash_result = CompareObjectsService.hash_thumbnail(io.BytesIO(image_data), algorithm)
            if hash_result:
                hash_object, _ = ItemProperties.objects.update_or_create(
                    content_type=image_type,
                    object_id=self.pk,
                    tag="hash-compare",
                    name=algorithm,
                    defaults={"value": hash_result},
                )

    def get_absolute_url(self) -> str:
        return reverse("viewer:image", args=[str(self.id)])

    def get_image_url(self) -> str:
        return self.image.url

    def set_attributes_from_image(
        self, image_object: typing.IO[bytes], image_size: int | None = None, image_name: str | None = None
    ) -> None:
        try:
            im = PImage.open(image_object)
            size = im.size
            self.original_width = size[0]
            self.original_height = size[1]
            self.image_format = im.format
            self.image_mode = im.mode
            self.image_size = image_size
            self.image_name = image_name
        except PImage.UnidentifiedImageError:
            pass


    def save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        if not self.image_height and self.image and os.path.isfile(self.image.path):
            im = PImage.open(self.image.path)
            size = im.size
            self.image_width = size[0]
            self.image_height = size[1]
            im.close()
        super(Image, self).save(*args, **kwargs)

    def simple_save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super(Image, self).save(*args, **kwargs)



