﻿import itertools
import os
import re
import shutil
import typing
import uuid
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone, date
from itertools import chain

import elasticsearch
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.files.storage import FileSystemStorage
from django.db.backends.sqlite3.base import DatabaseWrapper
from django.templatetags.static import static
from os.path import join as pjoin, basename
from tempfile import NamedTemporaryFile
from typing import Optional
from urllib.parse import quote, urlparse

import requests
from django.core.exceptions import ValidationError
from django.db.models.signals import post_delete, post_save
from django.db.models.sql.compiler import SQLCompiler
from django.dispatch import receiver
from django.http import HttpRequest
from django.utils.text import slugify
from django.db.models import Value, CharField
from django.db.models.functions import Concat, Replace

from PIL import Image as PImage
import django.db.models.options as options
from django.urls import reverse
from django.contrib.auth.models import User
from django.core.files import File
from django.db import models
from django.db.models import Q, F, Count, QuerySet
import django.utils.timezone as django_tz
from django.db.models import Lookup
from django.utils.translation import gettext_lazy as _
from django.conf import settings


from core.base.comparison import get_list_closer_gallery_titles_from_list
from core.base.utilities import (
    calc_crc32, get_zip_filesize,
    get_zip_fileinfo,
    sha1_from_file_object,
    clean_title,
    request_with_retries, get_images_from_zip)
from core.base.types import GalleryData, DataDict
from core.base.utilities import (
    get_dict_allowed_fields, replace_illegal_name
)
from viewer.utils.tags import sort_tags, sort_tags_str


T = typing.TypeVar('T')

options.DEFAULT_NAMES += 'es_index_name', 'es_mapping'
PImage.MAX_IMAGE_PIXELS = 200000000


class OriginalFilenameFileSystemStorage(FileSystemStorage):

    def get_valid_name(self, name):
        """
        Return a filename, based on the provided filename, that's suitable for
        use in the target storage system.
        """
        return name


fs = OriginalFilenameFileSystemStorage()


class SpacedSearch(Lookup):

    lookup_name = 'ss'

    def as_sql(self, qn: SQLCompiler, connection: DatabaseWrapper) -> tuple[str, typing.Any]:
        lhs, lhs_params = self.process_lhs(qn, connection)
        rhs, rhs_params = self.process_rhs(qn, connection)
        return '%s LIKE %s' % (lhs, rhs), lhs_params + rhs_params

    def as_mysql(self, qn: SQLCompiler, connection: DatabaseWrapper) -> tuple[str, typing.Any]:
        lhs, lhs_params = self.process_lhs(qn, connection)
        rhs, rhs_params = self.process_rhs(qn, connection)
        return '%s LIKE %s' % (lhs, rhs), lhs_params + rhs_params

    def as_postgresql(self, qn: SQLCompiler, connection: DatabaseWrapper) -> tuple[str, typing.Any]:
        lhs, lhs_params = self.process_lhs(qn, connection)
        rhs, rhs_params = self.process_rhs(qn, connection)
        return '%s ILIKE %s' % (lhs, rhs), lhs_params + rhs_params


models.CharField.register_lookup(SpacedSearch)
models.FileField.register_lookup(SpacedSearch)


class TagQuerySet(models.QuerySet):
    def are_custom(self) -> QuerySet:
        return self.filter(source='user')

    def not_custom(self) -> QuerySet:
        return self.exclude(source='user')

    def first_artist_tag(self) -> Optional['Tag']:
        return self.filter(scope__exact='artist').first()


class GalleryQuerySet(models.QuerySet):
    def several_archives(self) -> QuerySet:
        return self.annotate(
            num_archives=Count('archive')
        ).filter(num_archives__gt=1).order_by('-id').prefetch_related('archive_set')

    def different_filesize_archive(self, **kwargs: typing.Any) -> QuerySet:
        return self.filter(
            ~Q(filesize=F('archive__filesize')),
            ~Q(filesize=0),
            **kwargs,
        ).prefetch_related('archive_set').order_by('provider', '-create_date')

    def non_used_galleries(self, **kwargs: typing.Any) -> QuerySet:
        return self.filter(
            Q(status=Gallery.NORMAL),
            ~Q(dl_type__contains='skipped'),
            Q(archive__isnull=True),
            Q(gallery_container__archive__isnull=True),
            Q(magazine__archive__isnull=True),
            Q(alternative_sources__isnull=True),
            **kwargs
        ).order_by('-create_date')

    def submitted_galleries(self, *args: typing.Any, **kwargs: typing.Any) -> QuerySet:
        return self.filter(
            Q(origin=Gallery.ORIGIN_SUBMITTED),
            ~Q(status=Gallery.DELETED),
            Q(archive__isnull=True),
            Q(gallery_container__archive__isnull=True),
            Q(magazine__archive__isnull=True),
            *args,
            **kwargs
        ).order_by('-create_date')

    def eligible_for_use(self, **kwargs: typing.Any) -> QuerySet:
        return self.filter(
            Q(status=Gallery.NORMAL),
            **kwargs
        )


class GalleryManager(models.Manager):
    def get_queryset(self) -> GalleryQuerySet:
        return GalleryQuerySet(self.model, using=self._db)

    def exists_by_gid_provider(self, gid: str, provider: str) -> bool:
        return bool(self.filter(gid=gid, provider=provider))

    def filter_order_created(self, **kwargs: typing.Any) -> QuerySet:
        return self.filter(**kwargs).order_by('-create_date')

    def filter_order_modified(self, **kwargs: typing.Any) -> QuerySet:
        return self.filter(**kwargs).order_by('-last_modified')

    def after_posted_date(self, date: datetime) -> QuerySet:
        return self.filter(posted__gte=date).order_by('posted')

    def different_filesize_archive(self, **kwargs: typing.Any) -> QuerySet:
        return self.get_queryset().different_filesize_archive(**kwargs)

    def filter_non_existent(self) -> QuerySet:
        return self.get_queryset().several_archives()

    def non_used_galleries(self, **kwargs: typing.Any) -> QuerySet:
        return self.get_queryset().non_used_galleries(**kwargs)

    def submitted_galleries(self, *args: typing.Any, **kwargs: typing.Any) -> QuerySet:
        return self.get_queryset().submitted_galleries(*args, **kwargs)

    def eligible_for_use(self, **kwargs: typing.Any) -> QuerySet:
        return self.get_queryset().eligible_for_use(**kwargs)

    # # This is commented because it didn't work in MySQL
    # def need_new_archive(self, **kwargs):
    #
    #     return self.annotate(
    #         num_archives=Count('archive')
    #     ).filter(
    #         (
    #             (
    #                 Q(num_archives=1) &
    #                 ~Q(filesize=F('archive__filesize')) &
    #                 ~Q(filesize=0)
    #             ) |
    #             Q(archive__isnull=True)
    #         ),
    #         **kwargs
    #     ).prefetch_related('archive_set').order_by('-create_date')

    def filter_dl_type(self, dl_type_filter: str, **kwargs: typing.Any) -> QuerySet:
        return self.filter(dl_type__icontains=dl_type_filter, **kwargs)

    def filter_dl_type_and_posted_dates(self, dl_type_filter: str, start_date: datetime, end_date: datetime) -> QuerySet:
        return self.filter(posted__gte=start_date,
                           posted__lte=end_date,
                           dl_type__icontains=dl_type_filter)

    def filter_first(self, **kwargs: typing.Any) -> Optional['Gallery']:
        return self.filter(**kwargs).first()

    def update_by_gid_provider(self, gallery_data: GalleryData) -> bool:
        gallery = self.filter(gid=gallery_data.gid, provider=gallery_data.provider).first()
        if gallery:
            if gallery_data.tags is not None:
                for tag in gallery_data.tags:
                    if tag == "":
                        continue
                    scope_name = tag.split(":", maxsplit=1)
                    if len(scope_name) > 1:
                        tag_object, _ = Tag.objects.get_or_create(
                            scope=scope_name[0],
                            name=scope_name[1])
                    else:
                        scope = ''
                        tag_object, _ = Tag.objects.get_or_create(
                            name=tag, scope=scope)
                    gallery.tags.add(tag_object)
            values = get_dict_allowed_fields(gallery_data)
            if 'gallery_container_gid' in values:
                gallery_container = Gallery.objects.filter(
                    gid=values['gallery_container_gid'], provider=gallery_data.provider
                ).first()
                if gallery_container:
                    values['gallery_container'] = gallery_container
                del values['gallery_container_gid']
            if 'magazine_gid' in values:
                magazine = Gallery.objects.filter(
                    gid=values['magazine_gid'], provider=gallery_data.provider
                ).first()
                if magazine:
                    values['magazine'] = magazine
                del values['magazine_gid']
            for key, value in values.items():
                setattr(gallery, key, value)
            gallery.save()

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
    def add_from_values(gallery_data: GalleryData) -> 'Gallery':
        tags = gallery_data.tags

        values = get_dict_allowed_fields(gallery_data)
        if 'gallery_container_gid' in values:
            gallery_container = Gallery.objects.filter(
                gid=values['gallery_container_gid'], provider=values['provider']
            ).first()
            if gallery_container:
                values['gallery_container'] = gallery_container
            del values['gallery_container_gid']
        if 'magazine_gid' in values:
            magazine = Gallery.objects.filter(
                gid=values['magazine_gid'], provider=values['provider']
            ).first()
            if magazine:
                values['magazine'] = magazine
            del values['magazine_gid']

        gallery = Gallery(**values)
        gallery.save()

        if gallery_data.magazine_chapters_gids:
            chapters = Gallery.objects.filter(
                gid__in=gallery_data.magazine_chapters_gids, provider=values['provider']
            )
            chapters.update(magazine=gallery.pk)

        if gallery_data.gallery_contains_gids:
            contained = Gallery.objects.filter(
                gid__in=gallery_data.gallery_contains_gids, provider=values['provider']
            )
            contained.update(gallery_container=gallery.pk)

        if tags:
            gallery.tags.clear()
            for tag in tags:
                if tag == "":
                    continue
                scope_name = tag.split(":", maxsplit=1)
                if len(scope_name) > 1:
                    tag_object, _ = Tag.objects.get_or_create(
                        scope=scope_name[0],
                        name=scope_name[1])
                else:
                    scope = ''
                    tag_object, _ = Tag.objects.get_or_create(
                        name=tag, scope=scope)
                gallery.tags.add(tag_object)

        return gallery

    def update_or_create_from_values(self, gallery_data: GalleryData) -> 'Gallery':
        tags = gallery_data.tags

        values = get_dict_allowed_fields(gallery_data)
        if 'gallery_container_gid' in values:
            gallery_container = Gallery.objects.filter(
                gid=values['gallery_container_gid'], provider=values['provider']
            ).first()
            if gallery_container:
                values['gallery_container'] = gallery_container
            del values['gallery_container_gid']

        if 'magazine_gid' in values:
            magazine = Gallery.objects.filter(
                gid=values['magazine_gid'], provider=values['provider']
            ).first()
            if magazine:
                values['magazine'] = magazine
            del values['magazine_gid']

        gallery, _ = self.update_or_create(defaults=values, gid=values['gid'], provider=values['provider'])

        if gallery_data.magazine_chapters_gids:
            chapters = Gallery.objects.filter(
                gid__in=gallery_data.magazine_chapters_gids, provider=values['provider']
            )
            chapters.update(magazine=gallery.pk)

        if gallery_data.gallery_contains_gids:
            contained = Gallery.objects.filter(
                gid__in=gallery_data.gallery_contains_gids, provider=values['provider']
            )
            contained.update(gallery_container=gallery.pk)

        if tags:
            gallery.tags.clear()
            for tag in tags:
                if tag == "":
                    continue
                scope_name = tag.split(":", maxsplit=1)
                if len(scope_name) > 1:
                    tag_object, _ = Tag.objects.get_or_create(
                        scope=scope_name[0],
                        name=scope_name[1])
                else:
                    scope = ''
                    tag_object, _ = Tag.objects.get_or_create(
                        name=tag, scope=scope)
                gallery.tags.add(tag_object)

        return gallery

    # This method is mainly used to update own fields, no related fields need to be checked
    def update_by_dl_type(self, values: DataDict, gallery_id: str, dl_type: str) -> typing.Optional['Gallery']:

        instance = self.filter(id=gallery_id, dl_type__contains=dl_type).first()
        if instance:
            for key, value in values.items():
                setattr(instance, key, value)
            instance.save()
            return instance
        else:
            return None


class ArchiveQuerySet(models.QuerySet):
    def filter_non_existent(self, root: str, **kwargs: typing.Any) -> list['Archive']:
        archives = self.filter(**kwargs).order_by('-id')

        return [archive for archive in archives if not os.path.isfile(os.path.join(root, archive.zipped.path))]


class ArchiveManager(models.Manager):
    def get_queryset(self) -> ArchiveQuerySet:
        return ArchiveQuerySet(self.model, using=self._db)

    def filter_and_order_by_posted(self, **kwargs: typing.Any) -> ArchiveQuerySet:
        return self.get_queryset().filter(**kwargs).order_by('gallery__posted')

    def filter_non_existent(self, root: str, **kwargs: typing.Any) -> list['Archive']:
        return self.get_queryset().filter_non_existent(root, **kwargs)

    def filter_by_dl_remote(self) -> ArchiveQuerySet:
        return self.get_queryset().filter(
            Q(crc32='')
            & (
                Q(match_type__startswith='torrent') | Q(match_type__startswith='hath')
            )
        )

    def filter_by_missing_file_info(self) -> ArchiveQuerySet:
        return self.get_queryset().filter(crc32='')

    def filter_by_authenticated_status(self, authenticated: bool, **kwargs: typing.Any) -> ArchiveQuerySet:
        if authenticated:
            return self.get_queryset().filter(**kwargs)
        else:
            return self.get_queryset().filter(public=True, **kwargs)

    def filter_matching_gallery_filesize(self, gid: str, provider: str) -> Optional['Archive']:
        return self.filter(Q(gallery__gid=gid), Q(gallery__provider=provider),
                           Q(filesize=F('gallery__filesize'))).first()

    def update_or_create_by_values_and_gid(self, values: DataDict, gid_provider: Optional[tuple[str, str]], **kwargs: typing.Any) -> 'Archive':
        archive, _ = self.update_or_create(defaults=values, **kwargs)
        if gid_provider:
            gallery, _ = Gallery.objects.get_or_create(gid=gid_provider[0], provider=gid_provider[1])
            archive.gallery = gallery
            archive.save()
            archive.tags.set(gallery.tags.all())

        return archive

    @staticmethod
    def create_by_values_and_gid(values: DataDict, gid_provider: Optional[tuple[str, str]]) -> 'Archive':
        archive = Archive(**values)
        archive.simple_save()
        if gid_provider:
            gallery, _ = Gallery.objects.get_or_create(gid=gid_provider[0], provider=gid_provider[1])
            archive.gallery = gallery
            archive.tags.set(gallery.tags.all())

        if archive.title:
            base_file_name = replace_illegal_name(archive.title)
        elif archive.title_jpn:
            base_file_name = replace_illegal_name(archive.title_jpn)
        else:
            base_file_name = str(archive.id)
        archive.zipped = os.path.join(
            "galleries/archives/{id}/{file}".format(
                id=archive.id,
                file=base_file_name + '.zip'
            ),
        )
        os.makedirs(
            os.path.join(
                settings.MEDIA_ROOT,
                "galleries/archives/{id}".format(id=archive.id)),
            exist_ok=True)
        archive.save()

        return archive

    def add_or_update_from_values(self, values: DataDict, **kwargs: typing.Any) -> 'Archive':

        archive, _ = self.update_or_create(defaults=values, **kwargs)
        return archive

    def delete_by_filter(self, **kwargs: typing.Any) -> bool:
        t = self.filter(**kwargs)
        if t.exists():
            t.delete()
            return True
        else:
            return False


class Tag(models.Model):
    name = models.CharField(max_length=200)
    scope = models.CharField(max_length=200, default='', blank=True)
    source = models.CharField(
        'Source', max_length=50, blank=True, null=True, default='web')
    create_date = models.DateTimeField(auto_now_add=True)

    objects = TagQuerySet.as_manager()

    class Meta:
        ordering = ['-id']
        unique_together = [['scope', 'name']]

    def natural_key(self):
        return self.scope, self.name

    def __str__(self) -> str:
        if self.scope != '':
            return self.scope + ":" + self.name
        else:
            return self.name


def gallery_thumb_path_handler(instance: 'Gallery', filename: str) -> str:
    return "images/gallery_thumbs/{id}/{file}".format(id=instance.id,
                                                      file=filename)


class Gallery(models.Model):
    NORMAL = 1
    # Denied status is intended for submitted galleries that were not accepted by a moderator. Different from deleted.
    # To remove denied galleries from other lists, an admin should mark as DELETED.
    DENIED = 4
    # The deleted status hides the gallery from some user facing interfaces, as match galleries, gallery list, etc.
    # And makes that trying to parse it again results in it being skipped.
    DELETED = 5
    # This status is meant for galleries that are only kept as a way to mark already seen galleries, that don't live
    # With metadata
    NO_METADATA = 6

    ORIGIN_NORMAL = 1
    ORIGIN_SUBMITTED = 2

    STATUS_CHOICES = (
        (NORMAL, 'Normal'),
        (DENIED, 'Denied'),
        (DELETED, 'Deleted'),
        (NO_METADATA, 'No metadata'),
    )

    ORIGIN_CHOICES = (
        (ORIGIN_NORMAL, 'Normal'),
        (ORIGIN_SUBMITTED, 'Submitted'),
    )

    gid = models.CharField(max_length=200)
    token = models.CharField(max_length=50, blank=True, null=True)
    title = models.CharField(max_length=500, blank=True, null=True, default='')
    title_jpn = models.CharField(
        max_length=500, blank=True, null=True, default='')
    tags = models.ManyToManyField(Tag, blank=True, default='')
    gallery_container = models.ForeignKey(
        'self', blank=True, null=True, on_delete=models.SET_NULL,
        related_name='gallery_contains'
    )
    magazine = models.ForeignKey(
        'self', blank=True, null=True, on_delete=models.SET_NULL,
        related_name='magazine_chapters'
    )
    category = models.CharField(
        max_length=20, blank=True, null=True, default='')
    uploader = models.CharField(
        max_length=50, blank=True, null=True, default='')
    comment = models.TextField(blank=True, default='')
    posted = models.DateTimeField('Date posted', blank=True, null=True)
    filecount = models.IntegerField(
        'File count', blank=True, null=True, default=0)
    filesize = models.BigIntegerField('Size', blank=True, null=True, default=0)
    expunged = models.BooleanField(default=False)
    rating = models.CharField(max_length=10, blank=True, null=True, default='')
    hidden = models.BooleanField(default=False)
    fjord = models.BooleanField(default=False)
    public = models.BooleanField(default=False)
    provider = models.CharField('Provider', max_length=50, default='generic')
    dl_type = models.CharField(max_length=100, default='')
    reason = models.CharField(max_length=200, blank=True, null=True, default='backup')
    create_date = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True, blank=True, null=True)
    thumbnail_url = models.URLField(
        max_length=2000, blank=True, null=True, default='')
    thumbnail_height = models.PositiveIntegerField(blank=True, null=True)
    thumbnail_width = models.PositiveIntegerField(blank=True, null=True)
    thumbnail = models.ImageField(
        blank=True,
        upload_to=gallery_thumb_path_handler, default='', max_length=500,
        height_field='thumbnail_height',
        width_field='thumbnail_width')
    status = models.SmallIntegerField(
        choices=STATUS_CHOICES, db_index=True, default=NORMAL
    )
    origin = models.SmallIntegerField(
        choices=ORIGIN_CHOICES, db_index=True, default=ORIGIN_NORMAL
    )

    objects = GalleryManager()

    class Meta:
        es_index_name = settings.ES_GALLERY_INDEX_NAME
        es_mapping = {
            'properties': {
                'title': {'type': 'text'},
                'title_jpn': {'type': 'text'},
                'title_complete': {
                    'type': 'completion',
                    'analyzer': 'simple',
                    'preserve_separators': True,
                    'preserve_position_increments': True,
                    'max_input_length': 50,
                },
                'title_suggest': {
                    'type': 'text',
                    'analyzer': 'edge_ngram_analyzer',
                    'search_analyzer': 'standard',
                },
                'tags': {
                    'type': 'object',
                    'properties': {
                        'scope': {'type': 'keyword', 'store': True},
                        'name': {'type': 'keyword', 'store': True},
                        'full': {'type': 'keyword'},
                    }
                },
                'size': {'type': 'long'},
                'image_count': {'type': 'integer'},
                'create_date': {'type': 'date'},
                'posted_date': {'type': 'date'},
                'provider': {'type': 'keyword'},
                'reason': {'type': 'keyword'},
                'public': {'type': 'boolean'},
                'category': {'type': 'keyword'},
                'thumbnail': {'type': 'text', 'index': False}
            }
        }
        verbose_name_plural = "galleries"
        permissions = (
            ("publish_gallery", "Can publish available galleries"),
            ("approve_gallery", "Can approve submitted galleries"),
            ("wanted_gallery_found", "Can be notified of new wanted gallery matches"),
            ("crawler_adder", "Can add links to the crawler with more options"),
        )
        constraints = [
            models.UniqueConstraint(fields=['gid', 'provider'], name='unique_gallery')
        ]

    def es_repr(self) -> DataDict:
        data = {}
        mapping = self._meta.es_mapping  # type: ignore
        data['_id'] = self.pk
        for field_name in mapping['properties'].keys():
            data[field_name] = self.field_es_repr(field_name)
        return data

    def field_es_repr(self, field_name: str) -> T:
        config = self._meta.es_mapping['properties'][field_name]  # type: ignore
        if hasattr(self, 'get_es_%s' % field_name):
            field_es_value = getattr(self, 'get_es_%s' % field_name)()
        else:
            if config['type'] == 'object':
                related_object = getattr(self, field_name)
                if related_object:
                    field_es_value = {'_id': related_object.pk}
                    for prop in config['properties'].keys():
                        field_es_value[prop] = getattr(related_object, prop)
                else:
                    field_es_value = None
            else:
                field_es_value = getattr(self, field_name)
        return field_es_value

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

    def get_es_size(self) -> Optional[int]:
        return self.filesize

    def get_es_image_count(self) -> Optional[int]:
        return self.filecount

    def get_es_posted_date(self) -> typing.Optional[str]:
        data = None
        if self.posted:
            data = self.posted.replace(tzinfo=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S%z')
        return data

    def get_es_create_date(self) -> typing.Optional[str]:
        if self.create_date:
            return self.create_date.replace(tzinfo=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S%z')
        else:
            return None

    def get_es_tags(self) -> list[DataDict]:
        data: list[DataDict] = []
        if self.tags.exists():
            data += [{'scope': c.scope, 'name': c.name, 'full': str(c)} for c in self.tags.all()]
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

    def get_es_thumbnail(self) -> typing.Optional[str]:
        if self.thumbnail:
            return self.thumbnail.url
        else:
            return None

    def __str__(self) -> str:
        return self.title or self.title_jpn or ''

    @property
    def best_title(self) -> str:
        if self.title:
            return self.title
        elif self.title_jpn:
            return self.title_jpn
        return ""

    def tags_str(self) -> str:
        lst = [str(x) for x in self.tags.all()]
        return ', '.join(lst)

    def tag_list(self) -> list[str]:
        lst = [str(x) for x in self.tags.all()]
        return lst

    def tag_list_sorted(self) -> list[str]:
        return sort_tags_str(self.tags.all())

    def tag_lists(self) -> list[tuple[str, list['Tag']]]:
        return sort_tags(self.tags.all())

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
        return self.status == self.DELETED

    # Kinda not optimal
    def is_submitted(self) -> bool:
        return self.origin == self.ORIGIN_SUBMITTED and self.status != self.DENIED and not self.archive_set.all()

    def get_link(self) -> str:
        return settings.PROVIDER_CONTEXT.resolve_all_urls(self)

    def get_absolute_url(self) -> str:
        return reverse('viewer:gallery', args=[str(self.id)])

    # Should be used by small changes.
    def simple_save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super(Gallery, self).save(*args, **kwargs)

    def save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        is_new = self.pk
        super(Gallery, self).save(*args, **kwargs)
        if settings.ES_CLIENT and settings.ES_AUTOREFRESH_GALLERY:
            if (settings.ES_ONLY_INDEX_PUBLIC and self.public) or not settings.ES_ONLY_INDEX_PUBLIC:
                payload = self.es_repr()
                if is_new is not None:
                    del payload['_id']
                    try:
                        settings.ES_CLIENT.update(
                            index=self._meta.es_index_name,  # type: ignore
                            id=self.pk,
                            refresh=True,
                            body={
                                'doc': payload
                            },
                            request_timeout=30
                        )
                    except elasticsearch.exceptions.NotFoundError:
                        settings.ES_CLIENT.create(
                            index=self._meta.es_index_name,  # type: ignore
                            id=self.pk,
                            refresh=True,
                            body={
                                'doc': payload
                            },
                            request_timeout=30
                        )

                else:
                    settings.ES_CLIENT.create(
                        index=self._meta.es_index_name,  # type: ignore
                        id=self.pk,
                        refresh=True,
                        body={
                            'doc': payload
                        },
                        request_timeout=30
                    )
        if self.thumbnail_url and not self.thumbnail:
            request_dict = {
                'stream': True,
                'headers': settings.CRAWLER_SETTINGS.requests_headers,
                'timeout': settings.CRAWLER_SETTINGS.timeout_timer,
            }
            if self.provider in settings.CRAWLER_SETTINGS.providers:
                if settings.CRAWLER_SETTINGS.providers[self.provider].cookies:
                    request_dict['cookies'] = settings.CRAWLER_SETTINGS.providers[self.provider].cookies
                if settings.CRAWLER_SETTINGS.providers[self.provider].proxies:
                    request_dict['proxies'] = settings.CRAWLER_SETTINGS.providers[self.provider].proxies
                if settings.CRAWLER_SETTINGS.providers[self.provider].timeout_timer:
                    request_dict['timeout'] = settings.CRAWLER_SETTINGS.providers[self.provider].timeout_timer
            response = request_with_retries(
                self.thumbnail_url,
                request_dict,
                post=False,
            )
            if response:
                disassembled = urlparse(self.thumbnail_url)
                file_name = basename(disassembled.path)
                lf = NamedTemporaryFile()
                if response.status_code == requests.codes.ok:
                    for chunk in response.iter_content(chunk_size=1024):
                        if chunk:  # filter out keep-alive new chunks
                            lf.write(chunk)
                    self.thumbnail.save(file_name, File(lf), save=False)
                lf.close()

            super(Gallery, self).save(force_update=True)

    def delete(self, *args: typing.Any, **kwargs: typing.Any) -> tuple[int, dict[str, int]]:
        self.thumbnail.delete(save=False)
        self.remove_wanted_relations()
        prev_pk = self.pk
        deleted = super(Gallery, self).delete(*args, **kwargs)
        if settings.ES_CLIENT and settings.ES_AUTOREFRESH_GALLERY:
            try:
                settings.ES_CLIENT.delete(
                    index=self._meta.es_index_name,  # type: ignore
                    id=prev_pk,
                    refresh=True,
                    request_timeout=30
                )
            except elasticsearch.exceptions.NotFoundError:
                pass
        return deleted

    def remove_wanted_relations(self) -> None:
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
        self.status = self.DELETED
        self.save()

    def mark_as_denied(self) -> None:
        self.remove_wanted_relations()
        self.status = self.DENIED
        self.save()

    def match_against_wanted_galleries(self, wanted_filters: Optional['QuerySet[WantedGallery]'] = None, skip_already_found: bool = True) -> 'list[WantedGallery]':

        found_wanted_galleries = []

        if not wanted_filters:
            wanted_filters = WantedGallery.objects.all()

        if skip_already_found:
            wanted_filters = wanted_filters.filter(~Q(foundgallery__gallery=self))

        if self.title or self.title_jpn:
            q_objects = Q()
            q_objects_unwanted = Q()
            q_objects_regexp = Q()
            q_objects_unwanted_regexp = Q()
            if self.title:
                wanted_filters = wanted_filters.annotate(g_title=Value(self.title, output_field=CharField()))

                q_objects.add(Q(g_title__ss=Concat(Value('%'), Replace(F('search_title'), Value(' '), Value('%')), Value('%'))), Q.OR)
                q_objects_unwanted.add(~Q(g_title__ss=Concat(Value('%'), Replace(F('unwanted_title'), Value(' '), Value('%')), Value('%'))), Q.AND)

                q_objects_regexp.add(Q(g_title__regex=F('search_title')), Q.OR)
                q_objects_unwanted_regexp.add(~Q(g_title__regex=F('unwanted_title')), Q.AND)

            if self.title_jpn:
                wanted_filters = wanted_filters.annotate(g_title_jpn=Value(self.title_jpn, output_field=CharField()))
                q_objects.add(Q(g_title_jpn__ss=Concat(Value('%'), Replace(F('search_title'), Value(' '), Value('%')), Value('%'))), Q.OR)
                q_objects_unwanted.add(~Q(g_title_jpn__ss=Concat(Value('%'), Replace(F('unwanted_title'), Value(' '), Value('%')), Value('%'))), Q.AND)

                q_objects_regexp.add(Q(g_title_jpn__regex=F('search_title')), Q.OR)
                q_objects_unwanted_regexp.add(~Q(g_title_jpn__regex=F('unwanted_title')), Q.AND)

            filtered_wanted = wanted_filters.filter(
                Q(search_title__isnull=True)
                | Q(search_title='')
                | Q(Q(regexp_search_title=False), q_objects)
                | Q(Q(regexp_search_title=True), q_objects_regexp)
            ).filter(
                Q(unwanted_title__isnull=True)
                | Q(unwanted_title='')
                | Q(Q(regexp_unwanted_title=False), q_objects_unwanted)
                | Q(Q(regexp_unwanted_title=True), q_objects_unwanted_regexp)
            )

        else:
            filtered_wanted = wanted_filters.filter(
                Q(search_title__isnull=True) | Q(search_title='')
            ).filter(
                Q(unwanted_title__isnull=True) | Q(unwanted_title='')
            )

        if self.category:
            filtered_wanted = filtered_wanted.filter(Q(category='') | Q(category__iexact=self.category))

        if self.filecount:
            filtered_wanted = filtered_wanted.filter(
                Q(wanted_page_count_upper=0) | Q(wanted_page_count_upper__gt=self.filecount))
            filtered_wanted = filtered_wanted.filter(
                Q(wanted_page_count_lower=0) | Q(wanted_page_count_lower__lt=self.filecount))

        if self.provider:
            filtered_wanted = filtered_wanted.filter(Q(provider='') | Q(provider__iexact=self.provider))
            filtered_wanted = filtered_wanted.filter(Q(wanted_providers=None) | Q(wanted_providers__slug=self.provider))
            filtered_wanted = filtered_wanted.filter(Q(unwanted_providers=None) | ~Q(unwanted_providers__slug=self.provider))

        for wanted_filter in filtered_wanted:
            # if wanted_filter.wanted_providers.count():
            #     if not wanted_filter.wanted_providers.filter(slug=self.provider).first():
            #         continue
            accepted = True
            if bool(wanted_filter.wanted_tags.all()):
                if not set(wanted_filter.wanted_tags_list()).issubset(set(self.tag_list())):
                    accepted = False
                # Do not accept galleries that have more than 1 tag in the same wanted tag scope.
                if accepted & wanted_filter.wanted_tags_exclusive_scope:
                    accepted_tags = set(wanted_filter.wanted_tags_list()).intersection(set(self.tag_list()))
                    gallery_tags_scopes = [x.split(":", maxsplit=1)[0] for x in self.tag_list() if len(x) > 1]
                    wanted_gallery_tags_scopes = [x.split(":", maxsplit=1)[0] for x in accepted_tags if len(x) > 1]
                    scope_count: dict[str, int] = {}
                    for scope_name in gallery_tags_scopes:
                        if scope_name in wanted_gallery_tags_scopes:
                            if scope_name not in scope_count:
                                scope_count[scope_name] = 1
                            else:
                                scope_count[scope_name] += 1
                    for scope, count in scope_count.items():
                        if count > 1:
                            accepted = False
            if accepted & bool(wanted_filter.unwanted_tags.all()):
                if set(wanted_filter.unwanted_tags_list()).issubset(set(self.tag_list())):
                    accepted = False

            if accepted:
                found_wanted_galleries.append(wanted_filter)

        return found_wanted_galleries


class GallerySubmitEntryQuerySet(models.QuerySet):
    def to_be_resolved(self, *args: typing.Any, **kwargs: typing.Any) -> QuerySet:
        return self.filter(
            Q(resolved_status=GallerySubmitEntry.RESOLVED_SUBMITTED),
            **kwargs
        )


class GallerySubmitEntryManager(models.Manager):
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
        (RESOLVED_SUBMITTED, 'Submitted'),
        (RESOLVED_APPROVED, 'Approved'),
        (RESOLVED_DENIED, 'Denied'),
        (RESOLVED_ALREADY_PRESENT, 'Already present'),
    )

    class Meta:
        verbose_name_plural = "Gallery submit entries"

    gallery = models.ForeignKey(Gallery, blank=True, null=True, on_delete=models.SET_NULL)
    submit_url = models.TextField(blank=True, null=True, default='')
    submit_reason = models.TextField(blank=True, null=True, default='')
    submit_extra = models.TextField(blank=True, null=True, default='')
    submit_result = models.CharField(blank=True, null=True, default='', max_length=200)
    submit_date = models.DateTimeField(blank=True, default=django_tz.now)
    create_date = models.DateTimeField(auto_now_add=True)
    resolved_date = models.DateTimeField(blank=True, null=True)
    resolved_status = models.SmallIntegerField(
        choices=RESOLVED_STATUS_CHOICES, db_index=True, default=RESOLVED_SUBMITTED
    )
    resolved_reason = models.CharField('Reason', max_length=200, blank=True, null=True, default='backup')
    resolved_comment = models.TextField(blank=True, null=True, default='')

    def mark_as_denied(self, reason='', comment='') -> None:
        self.resolved_status = self.RESOLVED_DENIED
        if reason:
            self.resolved_reason = reason
        if comment:
            self.resolved_comment = comment
        self.resolved_date = django_tz.now()
        self.save()

    def mark_as_approved(self, reason='', comment='') -> None:
        self.resolved_status = self.RESOLVED_APPROVED
        if reason:
            self.resolved_reason = reason
        if comment:
            self.resolved_comment = comment
        self.resolved_date = django_tz.now()
        self.save()

    def __str__(self) -> str:
        return self.submit_url or 'Empty url'


@receiver(post_delete, sender=Gallery)
def thumbnail_post_delete_handler(sender: typing.Any, **kwargs: typing.Any) -> None:
    gallery = kwargs['instance']
    gallery.thumbnail.delete(save=False)


def archive_path_handler(instance: 'Archive', filename: str) -> str:
    return "galleries/archive_uploads/{file}".format(file=filename)


def thumb_path_handler(instance: 'Archive', filename: str) -> str:
    return "images/thumbs/archive_{id}/{file}".format(id=instance.id,
                                                      file=filename)


class Archive(models.Model):
    gallery = models.ForeignKey(Gallery, blank=True, null=True, on_delete=models.SET_NULL)
    title = models.CharField(max_length=500, blank=True, null=True)
    title_jpn = models.CharField(max_length=500, blank=True, null=True, default='')
    zipped = models.FileField(verbose_name='File', upload_to=archive_path_handler, max_length=500, storage=fs)
    original_filename = models.CharField('Original Filename', max_length=500, blank=True, null=True)
    custom_tags = models.ManyToManyField(Tag, blank=True, default='')
    crc32 = models.CharField('CRC32', max_length=10, blank=True)
    match_type = models.CharField(
        'Match type', max_length=40, blank=True, null=True, default='')
    filesize = models.BigIntegerField('Size', blank=True, null=True)
    filecount = models.IntegerField('File count', blank=True, null=True)
    public_date = models.DateTimeField(blank=True, null=True)
    create_date = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True, blank=True, null=True)
    user = models.ForeignKey(User, default=1, on_delete=models.SET_NULL, null=True)
    source_type = models.CharField(
        'Source type', max_length=50, blank=True, null=True, default='')
    reason = models.CharField(
        'Reason', max_length=200, blank=True, null=True, default='backup')
    public = models.BooleanField(default=False)
    thumbnail_height = models.PositiveIntegerField(blank=True, null=True)
    thumbnail_width = models.PositiveIntegerField(blank=True, null=True)
    thumbnail = models.ImageField(
        blank=True,
        upload_to=thumb_path_handler, default='', max_length=500,
        height_field='thumbnail_height',
        width_field='thumbnail_width')
    possible_matches = models.ManyToManyField(
        Gallery, related_name="possible_matches",
        blank=True, default='',
        through='ArchiveMatches', through_fields=('archive', 'gallery'))
    extracted = models.BooleanField(default=False)
    tags = models.ManyToManyField(Tag, related_name='gallery_tags', blank=True, default='')
    alternative_sources = models.ManyToManyField(
        Gallery, related_name="alternative_sources",
        blank=True, default='')
    details = models.TextField(
        blank=True, null=True, default='')

    objects = ArchiveManager()

    class Meta:
        es_index_name = settings.ES_INDEX_NAME
        es_mapping = {
            'properties': {
                'title': {'type': 'text'},
                'title_jpn': {'type': 'text'},
                'title_complete': {
                    'type': 'completion',
                    'analyzer': 'simple',
                    'preserve_separators': True,
                    'preserve_position_increments': True,
                    'max_input_length': 50,
                },
                'title_suggest': {
                    'type': 'text',
                    'analyzer': 'edge_ngram_analyzer',
                    'search_analyzer': 'standard',
                },
                'tags': {
                    'type': 'object',
                    'properties': {
                        'scope': {'type': 'keyword', 'store': True},
                        'name': {'type': 'keyword', 'store': True},
                        'full': {'type': 'keyword'},
                    }
                },
                'size': {'type': 'long'},
                'image_count': {'type': 'integer'},
                'create_date': {'type': 'date'},
                'public_date': {'type': 'date'},
                'original_date': {'type': 'date'},
                'source_type': {'type': 'keyword'},
                'reason': {'type': 'keyword'},
                'public': {'type': 'boolean'},
                'category': {'type': 'keyword'},
                'thumbnail': {'type': 'text', 'index': False},
            }
        }
        permissions = (
            ("publish_archive", "Can publish available archives"),
            ("manage_archive", "Can manage available archives"),
            ("match_archive", "Can match archives"),
            ("update_metadata", "Can update metadata"),
            ("upload_with_metadata_archive", "Can upload a file with an associated metadata source"),
        )

    def es_repr(self) -> DataDict:
        data = {}
        mapping = self._meta.es_mapping  # type: ignore
        data['_id'] = self.pk
        for field_name in mapping['properties'].keys():
            data[field_name] = self.field_es_repr(field_name)
        return data

    def field_es_repr(self, field_name: str) -> T:
        config = self._meta.es_mapping['properties'][field_name]  # type: ignore
        if hasattr(self, 'get_es_%s' % field_name):
            field_es_value = getattr(self, 'get_es_%s' % field_name)()
        else:
            if config['type'] == 'object':
                related_object = getattr(self, field_name)
                if related_object:
                    field_es_value = {'_id': related_object.pk}
                    for prop in config['properties'].keys():
                        field_es_value[prop] = getattr(related_object, prop)
                else:
                    field_es_value = None
            else:
                field_es_value = getattr(self, field_name)
        return field_es_value

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

    def get_es_size(self) -> Optional[int]:
        return self.filesize

    def get_es_category(self) -> Optional[str]:
        data = None
        if self.gallery:
            data = self.gallery.category
        return data

    def get_es_image_count(self) -> Optional[int]:
        return self.filecount

    def get_es_original_date(self) -> typing.Optional[str]:
        data = None
        if self.gallery and self.gallery.posted:
            data = self.gallery.posted.replace(tzinfo=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S%z')
        return data

    def get_es_public_date(self) -> typing.Optional[str]:
        if self.public_date:
            return self.public_date.replace(tzinfo=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S%z')
        else:
            return None

    def get_es_create_date(self) -> typing.Optional[str]:
        if self.create_date:
            return self.create_date.replace(tzinfo=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S%z')
        else:
            return None

    def get_es_tags(self) -> list[DataDict]:
        data: list[DataDict] = []
        if self.tags.exists():
            data += [{'scope': c.scope, 'name': c.name, 'full': str(c)} for c in self.tags.all()]
        if self.custom_tags.exists():
            data += [{'scope': c.scope, 'name': c.name, 'full': str(c)} for c in self.custom_tags.all()]
        return data

    def get_es_tag_names(self) -> list[str]:
        data: list[str] = []
        if self.tags.exists():
            data += [c.name for c in self.tags.all()]
        if self.custom_tags.exists():
            data += [c.name for c in self.custom_tags.all()]
        return data

    def get_es_tag_scopes(self) -> list[str]:
        data: list[str] = []
        if self.tags.exists():
            data += [c.scope for c in self.tags.all()]
        if self.custom_tags.exists():
            data += [c.scope for c in self.custom_tags.all()]
        return data

    def get_es_thumbnail(self) -> typing.Optional[str]:
        if self.thumbnail:
            return self.thumbnail.url
        else:
            return None

    def __str__(self) -> str:
        return self.title or self.title_jpn or ''

    def get_absolute_url(self) -> str:
        return reverse('viewer:archive', args=[str(self.id)])

    @property
    def pretty_name(self) -> str:
        return "{0}{1}".format(
            quote(replace_illegal_name(self.title or self.title_jpn or self.zipped.name)),
            os.path.splitext(self.zipped.name)[1]
        )

    @property
    def best_title(self) -> str:
        if self.title:
            return self.title
        elif self.title_jpn:
            return self.title_jpn
        return ""

    def filename(self) -> str:
        return os.path.basename(self.zipped.name)

    def tags_str(self) -> str:
        lst = [str(x) for x in self.tags.all()] + [str(x) for x in self.custom_tags.all()]
        return ', '.join(lst)

    def tag_list(self) -> list[str]:
        lst = [str(x) for x in self.tags.all()] + [str(x) for x in self.custom_tags.all()]
        return lst

    def tag_list_sorted(self) -> list[str]:
        return sort_tags_str(list(chain(self.tags.all(), self.custom_tags.all())))

    def tag_lists(self) -> list[tuple[str, list[Tag]]]:
        return sort_tags(self.tags.all())

    def custom_tag_lists(self) -> list[tuple[str, list[Tag]]]:
        return sort_tags(self.custom_tags.all())

    def images(self) -> str:
        lst = [x.image.name for x in self.image_set.all()]
        lst = ["<a href='/media/images/extracted/archive_" + str(self.id)
               + "/full/%s'>%s</a>" % (x, x.split('/')[-1]) for x in lst]
        return ', '.join(lst)

    def public_toggle(self) -> None:

        self.public = not self.public
        if self.public and os.path.isfile(self.zipped.path) and self.crc32:
            self.generate_image_set()
            self.generate_thumbnails()
            self.calculate_sha1_for_images()
            self.public_date = django_tz.now()
            self.simple_save()
        elif not self.public:
            self.simple_save()

    def set_public(self, reason: str = '') -> None:

        if not os.path.isfile(self.zipped.path) or not self.crc32:
            return
        self.public = True
        self.generate_image_set()
        self.generate_thumbnails()
        self.calculate_sha1_for_images()
        self.public_date = django_tz.now()
        if reason:
            self.reason = reason
        self.simple_save()
        self.simple_save()  # TODO: Check why the first simple_save isn't adding to the index.
        if self.gallery:
            if reason:
                self.gallery.reason = reason
            self.gallery.public = True
            self.gallery.save()

    def set_private(self, reason: str = '') -> None:

        if not os.path.isfile(self.zipped.path) or not self.crc32:
            return
        self.public = False
        if reason:
            self.reason = reason
        self.simple_save()
        if self.gallery:
            if reason:
                self.gallery.reason = reason
            self.gallery.public = False
            self.gallery.save()

    def delete_all_files(self) -> None:

        results = self.image_set.filter(extracted=True)

        if results:
            for img in results:
                img.image.delete(save=False)
                img.thumbnail.delete(save=False)
        self.thumbnail.delete(save=False)
        self.zipped.delete(save=False)
        self.image_set.all().delete()
        self.extracted = False
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

    def get_available_thumbnail_plus_size(self) -> tuple[str, Optional[int], Optional[int]]:
        if self.thumbnail.name and self.thumbnail.url:
            return self.thumbnail.url, self.thumbnail_height, self.thumbnail_width
        elif self.gallery and self.gallery.thumbnail.name and self.gallery.thumbnail.url:
            return self.gallery.thumbnail.url, self.gallery.thumbnail_height, self.gallery.thumbnail_width
        else:
            return static("imgs/no_cover.png"), 290, 196

    def calculate_sha1_for_images(self) -> bool:

        image_set = self.image_set.all()

        try:
            my_zip = zipfile.ZipFile(
                self.zipped.path, 'r')
        except (zipfile.BadZipFile, NotImplementedError):
            return False

        if my_zip.testzip():
            return False

        filtered_files = get_images_from_zip(my_zip)

        for count, filename_tuple in enumerate(filtered_files, start=1):
            image = image_set.get(archive_position=count)
            if image.extracted:
                with open(image.image.path, 'rb') as current_img:
                    image.sha1 = sha1_from_file_object(current_img)
            else:
                if filename_tuple[1] is None:
                    with my_zip.open(filename_tuple[0]) as current_zip_img:
                        image.sha1 = sha1_from_file_object(current_zip_img)
                else:
                    with my_zip.open(filename_tuple[1]) as current_zip:
                        with zipfile.ZipFile(current_zip) as my_nested_zip:
                            with my_nested_zip.open(filename_tuple[0]) as current_zip_img:
                                image.sha1 = sha1_from_file_object(current_zip_img)
            image.save()

        my_zip.close()
        return True

    def extract_delete(self) -> None:

        extracted_images = self.image_set.filter(extracted=True)

        if extracted_images:
            for img in extracted_images:
                img.image.delete(save=False)
                img.image = None
                img.thumbnail.delete(save=False)
                img.thumbnail = None
                img.extracted = False
                img.save()

        self.extracted = False
        self.simple_save()

    def extract_toggle(self) -> bool:

        extracted_images = self.image_set.filter(extracted=True)

        if extracted_images:
            for img in extracted_images:
                img.image.delete(save=False)
                img.image = None
                img.thumbnail.delete(save=False)
                img.thumbnail = None
                img.extracted = False
                img.save()
        else:
            try:
                my_zip = zipfile.ZipFile(
                    self.zipped.path, 'r')
            except (zipfile.BadZipFile, NotImplementedError):
                return False

            if my_zip.testzip():
                my_zip.close()
                return False

            os.makedirs(pjoin(settings.MEDIA_ROOT, "images/extracted/archive_{id}/full/".format(id=self.pk)), exist_ok=True)
            os.makedirs(pjoin(settings.MEDIA_ROOT, "images/extracted/archive_{id}/thumb/".format(id=self.pk)), exist_ok=True)

            non_extracted_images = self.image_set.filter(extracted=False)
            if not non_extracted_images:
                self.generate_image_set()
                non_extracted_images = self.image_set.filter(extracted=False)
            non_extracted_positions = non_extracted_images.values_list('archive_position', flat=True)

            filtered_files = get_images_from_zip(my_zip)

            for count, filename_tuple in enumerate(filtered_files, start=1):
                if count not in non_extracted_positions:
                    continue
                try:
                    image = non_extracted_images.get(archive_position=count)
                except Image.DoesNotExist:
                    self.generate_image_set()
                    image = non_extracted_images.get(archive_position=count)
                image_name = os.path.split(filename_tuple[2].replace('\\', os.sep))[1]

                # Image
                full_img_name = pjoin(settings.MEDIA_ROOT, upload_imgpath(self, image_name))
                thumb_img_name = upload_thumbpath_handler(image, image_name)

                with open(full_img_name, "wb") as current_new_img, my_zip.open(filename_tuple[1] or filename_tuple[0]) as current_file:
                    if filename_tuple[1]:
                        with zipfile.ZipFile(current_file) as my_nested_zip:
                            with my_nested_zip.open(filename_tuple[0]) as current_img:
                                shutil.copyfileobj(current_img, current_new_img)  # type: ignore
                    else:
                        shutil.copyfileobj(current_file, current_new_img)  # type: ignore
                image.image.name = upload_imgpath(self, image_name)

                # Thumbnail
                im = PImage.open(full_img_name)
                if im.mode != 'RGB':
                    im = im.convert('RGB')

                im.thumbnail((200, 290), PImage.ANTIALIAS)
                im.save(pjoin(settings.MEDIA_ROOT, thumb_img_name), "JPEG")
                image.thumbnail.name = thumb_img_name

                image.extracted = True
                image.save()

            my_zip.close()

        self.extracted = not self.extracted
        self.simple_save()
        return True

    def generate_image_set(self, force: bool = False) -> None:

        if not os.path.isfile(self.zipped.path):
            return
        image_set_present = bool(self.image_set.all())
        # large thumbnail and image set
        if not image_set_present or force:
            try:
                my_zip = zipfile.ZipFile(
                    self.zipped.path, 'r')
            except (zipfile.BadZipFile, NotImplementedError):
                return
            if my_zip.testzip():
                my_zip.close()
                return

            filtered_files = get_images_from_zip(my_zip)

            for img in self.image_set.all():
                if img.extracted:
                    img.image.delete(save=False)
                    img.thumbnail.delete(save=False)
                img.delete()
            for count, filename in enumerate(filtered_files, start=1):
                image = Image(archive=self, archive_position=count, position=count)
                image.image = None
                image.save()

            my_zip.close()

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
        if settings.ES_CLIENT and settings.ES_AUTOREFRESH:
            try:
                settings.ES_CLIENT.delete(
                    index=self._meta.es_index_name,  # type: ignore
                    id=prev_pk,
                    refresh=True,
                    request_timeout=30
                )
            except elasticsearch.exceptions.NotFoundError:
                pass
        return deleted

    def simple_save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        is_new = self.pk
        super(Archive, self).save(*args, **kwargs)
        if settings.ES_CLIENT and settings.ES_AUTOREFRESH:
            if (settings.ES_ONLY_INDEX_PUBLIC and self.public) or not settings.ES_ONLY_INDEX_PUBLIC:
                payload = self.es_repr()
                if is_new is not None:
                    del payload['_id']
                    try:
                        settings.ES_CLIENT.update(
                            index=self._meta.es_index_name,  # type: ignore
                            id=self.pk,
                            refresh=True,
                            body={
                                'doc': payload
                            },
                            request_timeout=30
                        )
                    except elasticsearch.exceptions.NotFoundError:
                        settings.ES_CLIENT.create(
                            index=self._meta.es_index_name,  # type: ignore
                            id=self.pk,
                            refresh=True,
                            body={
                                'doc': payload
                            },
                            request_timeout=30
                        )

                else:
                    settings.ES_CLIENT.create(
                        index=self._meta.es_index_name,  # type: ignore
                        id=self.pk,
                        refresh=True,
                        body={
                            'doc': payload
                        },
                        request_timeout=30
                    )

    def save(self, *args: typing.Any, **kwargs: typing.Any) -> None:

        self.simple_save(*args, **kwargs)

        if not self.zipped or not os.path.isfile(self.zipped.path):
            return
        image_set_present = bool(self.image_set.all())
        # large thumbnail and image set
        if not self.thumbnail or not image_set_present:
            try:
                my_zip = zipfile.ZipFile(
                    self.zipped.path, 'r')
            except (zipfile.BadZipFile, NotImplementedError):
                return
            if my_zip.testzip():
                my_zip.close()
                return
            filtered_files = get_images_from_zip(my_zip)

            if not image_set_present:
                for count, filename_tuple in enumerate(filtered_files, start=1):
                    # image_name = os.path.split(filename.replace('\\', os.sep))[1]
                    image = Image(archive=self, archive_position=count, position=count)
                    # image.image.name = upload_imgpath(self, image_name)
                    image.image = None
                    image.save()

            if not self.thumbnail and filtered_files:

                if image_set_present:
                    first_file = filtered_files[self.image_set.all()[0].archive_position - 1]
                else:
                    first_file = filtered_files[0]

                if first_file[1] is None:
                    with my_zip.open(first_file[0]) as current_img:
                        self.create_thumbnail_from_io_image(current_img)
                else:
                    with my_zip.open(first_file[1]) as current_zip:
                        with zipfile.ZipFile(current_zip) as my_nested_zip:
                            with my_nested_zip.open(first_file[0]) as current_img:
                                self.create_thumbnail_from_io_image(current_img)

            my_zip.close()

        # title
        if self.gallery and self.gallery.title:
            self.title = self.gallery.title
            self.possible_matches.clear()
        elif self.title is None:
            self.title = re.sub(
                '[_]',
                ' ',
                os.path.splitext(os.path.basename(self.zipped.name))[0])

        # tags
        if self.gallery and self.gallery.tags.all():
            self.tags.set(self.gallery.tags.all())

        # title_jpn
        if self.gallery and self.gallery.title_jpn:
            self.title_jpn = self.gallery.title_jpn

        # crc32
        if self.crc32 is None or self.crc32 == '':
            self.crc32 = calc_crc32(self.zipped.path)

        # size
        if self.filesize is None or self.filecount is None:
            self.filesize, self.filecount = get_zip_fileinfo(
                self.zipped.path)

        # original_filename
        if self.original_filename is None or self.original_filename == '':
            self.original_filename = os.path.basename(self.zipped.name)

        self.simple_save(force_update=True)

    def create_thumbnail_from_io_image(self, current_img):
        im = PImage.open(current_img)
        if im.mode != 'RGB':
            im = im.convert('RGB')
        im.thumbnail((200, 290), PImage.ANTIALIAS)
        thumb_name = thumb_path_handler(self, "thumb2.jpg")
        os.makedirs(os.path.dirname(pjoin(settings.MEDIA_ROOT, thumb_name)), exist_ok=True)
        im.save(pjoin(settings.MEDIA_ROOT, thumb_name), "JPEG")
        self.thumbnail.name = thumb_name

    def rename_zipped_tail(self, new_file_name: str) -> bool:
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

    def generate_possible_matches(self, cutoff: float = 0.4, max_matches: int = 20, clear_title: bool = False, provider_filter: str = '') -> None:
        if not self.match_type == 'non-match':
            return

        galleries_title_id = []

        if provider_filter:
            galleries = Gallery.objects.eligible_for_use(provider__contains=provider_filter)
        else:
            galleries = Gallery.objects.eligible_for_use()
        for gallery in galleries:
            if gallery.title:
                galleries_title_id.append(
                    (replace_illegal_name(gallery.title), gallery.pk))
            if gallery.title_jpn:
                galleries_title_id.append(
                    (replace_illegal_name(gallery.title_jpn), gallery.pk))

        adj_title = replace_illegal_name(
            os.path.basename(self.zipped.name)).replace(".zip", "")

        if clear_title:
            adj_title = clean_title(adj_title)

        similar_list = get_list_closer_gallery_titles_from_list(
            adj_title, galleries_title_id, cutoff, max_matches)

        if similar_list is not None:

            self.possible_matches.clear()

            for similar in similar_list:
                gallery = Gallery.objects.get(pk=similar[1])

                ArchiveMatches.objects.create(archive=self,
                                              gallery=gallery,
                                              match_type='title',
                                              match_accuracy=similar[2])

        if self.filesize is None or self.filesize <= 0:
            return
        galleries_same_size = Gallery.objects.filter(filesize=self.filesize)
        if galleries_same_size.exists():

            for similar_gallery in galleries_same_size:
                gallery = Gallery.objects.get(pk=similar_gallery.pk)

                ArchiveMatches.objects.create(archive=self,
                                              gallery=gallery,
                                              match_type='size',
                                              match_accuracy=1)

    def recalc_filesize(self) -> None:
        if os.path.isfile(self.zipped.path):
            self.filesize = get_zip_filesize(
                self.zipped.path)
            super(Archive, self).save()

    def recalc_fileinfo(self) -> None:
        if os.path.isfile(self.zipped.path):
            self.filesize, self.filecount = get_zip_fileinfo(
                self.zipped.path)
            self.crc32 = calc_crc32(self.zipped.path)
            super(Archive, self).save()

    def set_original_filename(self) -> None:
        if os.path.isfile(self.zipped.path):
            self.original_filename = os.path.basename(self.zipped.name)
            super(Archive, self).save()

    def generate_thumbnails(self) -> bool:

        if not os.path.exists(self.zipped.path):
            return False
        try:
            my_zip = zipfile.ZipFile(self.zipped.path, 'r')
        except (zipfile.BadZipFile, NotImplementedError):
            return False

        if my_zip.testzip():
            my_zip.close()
            return False

        self.thumbnail.delete(save=False)

        filtered_files = get_images_from_zip(my_zip)

        if not filtered_files:
            my_zip.close()
            return False

        images_from_archive = self.image_set.all()

        if images_from_archive:
            first_file = filtered_files[images_from_archive[0].archive_position - 1]
        else:
            first_file = filtered_files[0]

        if first_file[1] is None:
            with my_zip.open(first_file[0]) as current_img:
                self.create_thumbnail_from_io_image(current_img)
        else:
            with my_zip.open(first_file[1]) as current_zip:
                with zipfile.ZipFile(current_zip) as my_nested_zip:
                    with my_nested_zip.open(first_file[0]) as current_img:
                        self.create_thumbnail_from_io_image(current_img)

        my_zip.close()
        super(Archive, self).save()
        return True

    def select_as_match(self, gallery_id: int) -> None:
        try:
            matched_gallery = Gallery.objects.get(pk=gallery_id)
        except Gallery.DoesNotExist:
            return
        self.gallery_id = matched_gallery.id
        self.title = matched_gallery.title
        self.title_jpn = matched_gallery.title_jpn
        self.match_type = "manual:user"
        self.possible_matches.clear()
        self.simple_save()
        self.tags.set(matched_gallery.tags.all())
        if self.public:
            matched_gallery.public = True
            matched_gallery.save()

    def similar_archives(self, num_common_tags: int = 0, **kwargs: typing.Any) -> 'QuerySet[Archive]':
        return Archive.objects.filter(tags__in=self.tags.all(), **kwargs).exclude(pk=self.pk).\
            annotate(num_common_tags=Count('pk')).filter(num_common_tags__gt=num_common_tags).distinct().\
            order_by('-num_common_tags')

# Admin


class ArchiveGroup(models.Model):
    title = models.CharField(max_length=500, blank=False, null=False)
    title_slug = models.SlugField(unique=True)
    details = models.TextField(
        blank=True, null=True, default='')
    archives = models.ManyToManyField(
        Archive, related_name="archive_groups",
        blank=True, default='',
        through='ArchiveGroupEntry', through_fields=('archive_group', 'archive'))
    position = models.PositiveIntegerField(default=1)
    public = models.BooleanField(default=False)
    create_date = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True, blank=True, null=True)

    class Meta:
        verbose_name_plural = "Archive groups"
        ordering = ['position']

    def __str__(self) -> str:
        return self.title

    def get_absolute_url(self) -> str:
        return reverse('viewer:archive-group', args=[str(self.title_slug)])

    def save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        if not self.title_slug:

            slug_candidate = slugify(self.title, allow_unicode=True)
            for i in itertools.count(1):
                if not ArchiveGroup.objects.filter(title_slug=slug_candidate).exists():
                    break
                slug_candidate = slugify('{}-{}'.format(self.title, i), allow_unicode=True)

            self.title_slug = slug_candidate
        super().save(*args, **kwargs)


class ArchiveGroupEntry(models.Model):
    archive_group = models.ForeignKey(ArchiveGroup, on_delete=models.CASCADE)
    archive = models.ForeignKey(Archive, on_delete=models.CASCADE)
    title = models.CharField(max_length=500, blank=True, default='')
    position = models.PositiveIntegerField(blank=True, null=True)

    class Meta:
        verbose_name_plural = "Archive group entries"
        ordering = ['position']

    def save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        if not self.position:
            last_position = ArchiveGroupEntry.objects.filter(
                archive_group=self.archive_group
            ).exclude(position__isnull=True).order_by('-position').first()

            if last_position is None or last_position.position is None:
                position_candidate = 1
            else:
                position_candidate = last_position.position + 1

            self.position = position_candidate

        self.title = self.title or self.archive.title or self.archive.title or ''

        super().save(*args, **kwargs)


class ArchiveMatches(models.Model):
    archive = models.ForeignKey(Archive, on_delete=models.CASCADE)
    gallery = models.ForeignKey(Gallery, on_delete=models.CASCADE)
    match_type = models.CharField(
        'Match type', max_length=40, blank=True, null=True, default='')
    match_accuracy = models.FloatField(
        'Match accuracy', blank=True, null=True, default=0.0)

    class Meta:
        verbose_name_plural = "Archive matches"
        ordering = ['-match_accuracy']


def upload_imgpath(instance: 'Archive', filename: str) -> str:
    return "images/extracted/archive_{id}/full/{file}".format(
        id=instance.id,
        file=filename)


def upload_imgpath_handler(instance: 'Image', filename: str) -> str:
    return "images/extracted/archive_{id}/full/{file}".format(
        id=instance.archive.id,
        file=filename)


def upload_thumbpath_handler(instance: 'Image', filename: str) -> str:
    return "images/extracted/archive_{id}/thumb/{file}".format(
        id=instance.archive.id,
        file=filename)


class Image(models.Model):
    image = models.ImageField(upload_to=upload_imgpath_handler, blank=True,
                              null=True, max_length=500,
                              height_field='image_height',
                              width_field='image_width')
    image_height = models.PositiveIntegerField(null=True)
    image_width = models.PositiveIntegerField(null=True)
    thumbnail_height = models.PositiveIntegerField(blank=True, null=True)
    thumbnail_width = models.PositiveIntegerField(blank=True, null=True)
    thumbnail = models.ImageField(
        upload_to=upload_thumbpath_handler, blank=True,
        null=True, max_length=500,
        height_field='thumbnail_height',
        width_field='thumbnail_width')
    archive = models.ForeignKey(Archive, on_delete=models.CASCADE)
    archive_position = models.PositiveIntegerField(default=1)
    position = models.PositiveIntegerField(default=1)
    sha1 = models.CharField(max_length=50, blank=True, null=True)
    extracted = models.BooleanField(default=False)

    class Meta:
        ordering = ['position']

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
            'position': self.position,
            'url': request.build_absolute_uri(self.image.url),
            'is_horizontal': self.image_width / self.image_height > 1 if self.image_width and self.image_height else False,
            'width': self.image_width,
            'height': self.image_height
        }

        return image_dict

    def get_absolute_url(self) -> str:
        return reverse('viewer:image', args=[str(self.id)])

    def get_image_url(self) -> str:
        return self.image.url

    def save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        if not self.image_height and self.image and os.path.isfile(self.image.path):
            im = PImage.open(self.image.path)
            size = im.size
            self.image_width = size[0]
            self.image_height = size[1]
        super(Image, self).save(*args, **kwargs)


class UserArchivePrefs(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    archive = models.ForeignKey(Archive, on_delete=models.CASCADE)
    favorite_group = models.IntegerField('Favorite Group', default=1)
    # TODO: Add unique_together for user, archive


class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    bio = models.TextField(max_length=500, blank=True, default='')
    notify_new_submissions = models.BooleanField(default=False, blank=True)
    notify_new_private_archive = models.BooleanField(default=False, blank=True)
    notify_wanted_gallery_found = models.BooleanField(default=False, blank=True)


@receiver(post_save, sender=User)
def create_favorites(sender, instance, created, **kwargs):
    if created:
        Profile.objects.create(user=instance)


def users_with_perm(app: str, perm_name: str, *args: typing.Any, **kwargs: typing.Any):
    return User.objects.filter(
        Q(is_superuser=True)
        | Q(user_permissions__codename=perm_name, user_permissions__content_type__app_label=app)
        | Q(groups__permissions__codename=perm_name, groups__permissions__content_type__app_label=app)
    ).filter(is_active=True).filter(*args, **kwargs).distinct()


def upload_mention_handler(instance: 'Mention', filename: str) -> str:
    return "mentions/{id}/fn_{file}{ext}".format(
        id=instance.id,
        file=uuid.uuid4(),
        ext=filename)


def upload_mention_thumb_handler(instance: 'Mention', filename: str) -> str:
    return "mentions/{id}/tn_{file}{ext}".format(
        id=instance.id,
        file=uuid.uuid4(),
        ext=filename)


class Mention(models.Model):
    mention_date = models.DateTimeField('Mention date', blank=True, null=True)
    release_date = models.DateTimeField('Release date', blank=True, null=True)
    type = models.CharField(
        max_length=50, blank=True, null=True, default='')
    source = models.CharField(
        max_length=50, blank=True, null=True, default='')
    comment = models.CharField(
        max_length=100, blank=True, null=True, default='')
    image = models.ImageField(upload_to=upload_mention_handler, blank=True,
                              null=True, max_length=500,
                              height_field='image_height',
                              width_field='image_width')
    image_height = models.PositiveIntegerField(blank=True, null=True)
    image_width = models.PositiveIntegerField(blank=True, null=True)
    thumbnail_height = models.PositiveIntegerField(blank=True, null=True)
    thumbnail_width = models.PositiveIntegerField(blank=True, null=True)
    thumbnail = models.ImageField(
        upload_to=upload_mention_thumb_handler, blank=True,
        null=True, max_length=500,
        height_field='thumbnail_height',
        width_field='thumbnail_width')

    def __str__(self) -> str:
        return str(self.mention_date)

    def save_img(self, img_link: str) -> None:
        tf2 = NamedTemporaryFile()

        request_dict = {
            'stream': True,
            'headers': settings.CRAWLER_SETTINGS.requests_headers,
            'timeout': settings.CRAWLER_SETTINGS.timeout_timer,
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
        if not self.image:
            return
        im = PImage.open(self.image.path)
        if im.mode != 'RGB':
            im = im.convert('RGB')

        # large thumbnail
        im.thumbnail((200, 290), PImage.ANTIALIAS)
        thumb_relative_path = upload_mention_thumb_handler(self, os.path.splitext(self.image.name)[1])
        thumb_fn = pjoin(settings.MEDIA_ROOT, thumb_relative_path)
        os.makedirs(os.path.dirname(thumb_fn), exist_ok=True)
        im.save(thumb_fn, "JPEG")
        self.thumbnail.name = thumb_relative_path

        self.save()


class Artist(models.Model):
    name = models.CharField(
        max_length=50, blank=True, null=True, default='')
    name_jpn = models.CharField(
        max_length=50, blank=True, null=True, default='')
    twitter_handle = models.CharField(
        max_length=50, blank=True, null=True, default='')

    def __str__(self) -> str:
        return self.name or self.name_jpn or self.twitter_handle or ''


class WantedGalleryManager(models.Manager):
    def not_found(self) -> QuerySet:
        return self.filter(found=False)

    def eligible_to_search(self) -> QuerySet:
        return self.filter(
            Q(Q(Q(release_date__lte=django_tz.now()) | Q(release_date__isnull=True)), should_search=True)
            & (
                Q(found=False)
                | Q(found=True, keep_searching=True)
            )
        )


class WantedGallery(models.Model):
    objects = WantedGalleryManager()

    title = models.CharField(max_length=500, blank=True, null=True, default='')
    title_jpn = models.CharField(
        max_length=500, blank=True, null=True, default='')
    book_type = models.CharField(
        'Book type', max_length=20, blank=True, null=True, default='')
    publisher = models.CharField(
        'Publisher', max_length=20, blank=True, null=True, default='')
    public = models.BooleanField(default=False)
    release_date = models.DateTimeField('Release date', blank=True, null=True, default=django_tz.now)
    mentions = models.ManyToManyField(Mention, blank=True)
    artists = models.ManyToManyField(Artist, blank=True)
    cover_artist = models.ForeignKey(
        Artist, blank=True, null=True, related_name="cover_artist", on_delete=models.SET_NULL)
    look_for_duration = models.DurationField('Look for duration', default=timedelta(days=30))
    should_search = models.BooleanField('Should search', blank=True, default=False)
    keep_searching = models.BooleanField('Keep searching', blank=True, default=False)
    add_as_hidden = models.BooleanField('Add as hidden', blank=True, default=False)
    notify_when_found = models.BooleanField('Notify when found', default=True)
    reason = models.CharField(
        'Reason', max_length=200, blank=True, null=True, default='backup')
    search_title = models.CharField(max_length=500, blank=True, default='')
    regexp_search_title = models.BooleanField('Regexp search title', blank=True, default=False)
    unwanted_title = models.CharField(max_length=500, blank=True, default='')
    regexp_unwanted_title = models.BooleanField('Regexp unwanted title', blank=True, default=False)
    wanted_page_count_lower = models.IntegerField(blank=True, default=0)
    wanted_page_count_upper = models.IntegerField(blank=True, default=0)
    wanted_tags = models.ManyToManyField(Tag, blank=True)
    wanted_tags_exclusive_scope = models.BooleanField(blank=True, default=False)
    unwanted_tags = models.ManyToManyField(Tag, blank=True, related_name="unwanted_tags")
    category = models.CharField(
        max_length=20, blank=True, null=True, default='')
    # TODO: migrate all instances to use wanted_providers instead, and then delete this field (redundant).
    provider = models.CharField(
        'Provider', max_length=50, blank=True, null=True, default='')
    wanted_providers = models.ManyToManyField('Provider', blank=True)
    unwanted_providers = models.ManyToManyField('Provider', blank=True, related_name="unwanted_providers")
    found_galleries = models.ManyToManyField(
        Gallery, related_name="found_galleries",
        blank=True,
        through='FoundGallery', through_fields=('wanted_gallery', 'gallery'))

    possible_matches = models.ManyToManyField(
        Gallery, related_name="gallery_matches",
        blank=True,
        through='GalleryMatch', through_fields=('wanted_gallery', 'gallery'))
    found = models.BooleanField('Found', blank=True, default=False)
    date_found = models.DateTimeField('Date found', blank=True, null=True)

    page_count = models.IntegerField(
        'Page count', blank=True, null=True, default=0)
    create_date = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True, blank=True, null=True)

    class Meta:
        verbose_name_plural = "Wanted galleries"
        ordering = ['-release_date']
        permissions = (
            ("edit_search_filter_wanted_gallery", "Can edit wanted galleries search filter parameters"),
            ("edit_search_dates_wanted_gallery", "Can edit wanted galleries search date parameters"),
            ("edit_search_notify_wanted_gallery", "Can edit wanted galleries search notify parameter"),
        )

    def __str__(self) -> str:
        return self.title or self.title_jpn or ''

    def mentions_str(self) -> str:
        lst = [str(x) for x in self.mentions.all()]
        return ', '.join(lst)

    def mentions_list(self) -> list[str]:
        lst = [str(x) for x in self.mentions.all()]
        return lst

    def wanted_tags_list(self) -> list[str]:
        lst = [str(x) for x in self.wanted_tags.all()]
        return lst

    def unwanted_tags_list(self) -> list[str]:
        lst = [str(x) for x in self.unwanted_tags.all()]
        return lst

    def calculate_nearest_release_date(self) -> None:
        if not self.mentions:
            return
        mention_dates_objs = self.mentions.exclude(release_date=None).values_list('release_date', flat=True)
        if not mention_dates_objs:
            return
        mention_dates = [x.date() for x in mention_dates_objs if x is not None]
        date_count: typing.Counter[date] = Counter(mention_dates)
        most_occurring_date = date_count.most_common(1)[0][0]
        self.release_date = datetime.combine(most_occurring_date, datetime.min.time())
        self.save()

    def search_gallery_title_internal_matches(self, provider_filter: str = '', cutoff: float = 0.4, max_matches: int = 20) -> None:
        galleries_title_id = []

        if provider_filter:
            galleries = Gallery.objects.eligible_for_use(provider__contains=provider_filter)
        else:
            galleries = Gallery.objects.eligible_for_use()
        galleries = galleries.filter(~Q(foundgallery__wanted_gallery=self))
        for gallery in galleries:
            if gallery.title:
                galleries_title_id.append(
                    (clean_title(gallery.title), gallery.pk))
            if gallery.title_jpn:
                galleries_title_id.append(
                    (clean_title(gallery.title_jpn), gallery.pk))

        similar_list = get_list_closer_gallery_titles_from_list(
            self.search_title, galleries_title_id, cutoff, max_matches)

        if similar_list:

            for similar in similar_list:
                GalleryMatch.objects.get_or_create(
                    wanted_gallery=self,
                    gallery_id=similar[1],
                    defaults={'match_accuracy': similar[2]})

    def match_against_galleries(self) -> None:

        matched_galleries = self.get_matching_galleries()

        for matched_gallery in matched_galleries:
            self.found = True
            self.date_found = django_tz.now()
            self.save()
            FoundGallery.objects.get_or_create(
                wanted_gallery=self,
                gallery=matched_gallery
            )

    def get_matching_galleries(self) -> list['Gallery']:

        matching_galleries: list['Gallery'] = []

        galleries = Gallery.objects.eligible_for_use().filter(~Q(foundgallery__wanted_gallery=self))

        has_wanted_tags = bool(self.wanted_tags.all())
        has_unwanted_tags = bool(self.unwanted_tags.all())
        wanted_providers_count = self.wanted_providers.count()
        unwanted_providers_count = self.unwanted_providers.count()

        if self.search_title or self.unwanted_title:
            q_objects_search_title = Q()
            q_objects_unwanted_title = Q()
            if self.search_title:
                galleries = galleries.annotate(g_search_title=Value(self.search_title, output_field=CharField()))
                if self.regexp_search_title:
                    q_objects_search_title.add(
                        Q(title__regex=self.search_title),
                        Q.OR
                    )
                    q_objects_search_title.add(
                        Q(title_jpn__regex=self.search_title),
                        Q.OR
                    )
                else:
                    q_objects_search_title.add(
                        Q(g_search_title__ss=Concat(Value('%'), Replace(F('title'), Value(' '), Value('%')), Value('%'))),
                        Q.OR
                    )
                    q_objects_search_title.add(
                        Q(g_search_title__ss=Concat(Value('%'), Replace(F('title_jpn'), Value(' '), Value('%')),
                                                    Value('%'))),
                        Q.OR
                    )

            if self.unwanted_title:
                galleries = galleries.annotate(g_unwanted_title=Value(self.unwanted_title, output_field=CharField()))
                if self.regexp_unwanted_title:
                    q_objects_unwanted_title.add(
                        ~Q(title__regex=self.unwanted_title),
                        Q.AND
                    )
                    q_objects_unwanted_title.add(
                        ~Q(title_jpn__regex=self.unwanted_title),
                        Q.AND
                    )
                else:
                    q_objects_unwanted_title.add(
                        ~Q(g_unwanted_title__ss=Concat(Value('%'), Replace(F('title'), Value(' '), Value('%')),
                                                       Value('%'))),
                        Q.AND
                    )
                    q_objects_unwanted_title.add(
                        ~Q(g_unwanted_title__ss=Concat(Value('%'), Replace(F('title_jpn'), Value(' '), Value('%')),
                                                       Value('%'))),
                        Q.AND
                    )

            galleries = galleries.filter(
                q_objects_search_title
            ).filter(
                q_objects_unwanted_title
            )

        # if has_wanted_tags:
        #     galleries = galleries.filter(tags__in=self.wanted_tags.all())
        #
        # if has_unwanted_tags:
        #     galleries = galleries.filter(~Q(tags__in=self.unwanted_tags.all()))
        #
        # if self.category:
        #     galleries = galleries.filter(category__iexact=self.category)
        #
        # if self.wanted_page_count_upper:
        #     galleries = galleries.filter(filecount__le=self.wanted_page_count_upper)
        #
        # if self.wanted_page_count_lower:
        #     galleries = galleries.filter(filecount__ge=self.wanted_page_count_lower)
        #
        # if self.provider:
        #     galleries = galleries.filter(provider__iexact=self.provider)

        for gallery in galleries:
            if wanted_providers_count:
                if not self.wanted_providers.filter(slug=gallery.provider).first():
                    continue
            if unwanted_providers_count:
                if self.unwanted_providers.filter(slug=gallery.provider).first():
                    continue
            accepted = True
            if has_wanted_tags:
                if not set(self.wanted_tags_list()).issubset(set(gallery.tag_list())):
                    accepted = False
                # Do not accept galleries that have more than 1 tag in the same wanted tag scope.
                if accepted & self.wanted_tags_exclusive_scope:
                    accepted_tags = set(self.wanted_tags_list()).intersection(set(gallery.tag_list()))
                    gallery_tags_scopes = [x.split(":", maxsplit=1)[0] for x in gallery.tag_list() if len(x) > 1]
                    wanted_gallery_tags_scopes = [x.split(":", maxsplit=1)[0] for x in accepted_tags if len(x) > 1]
                    scope_count: dict[str, int] = {}
                    for scope_name in gallery_tags_scopes:
                        if scope_name in wanted_gallery_tags_scopes:
                            if scope_name not in scope_count:
                                scope_count[scope_name] = 1
                            else:
                                scope_count[scope_name] += 1
                    for scope, count in scope_count.items():
                        if count > 1:
                            accepted = False
            if accepted & has_unwanted_tags:
                if set(self.unwanted_tags_list()).issubset(set(gallery.tag_list())):
                    accepted = False

            if accepted:
                matching_galleries.append(gallery)

        return matching_galleries

    def public_toggle(self) -> None:

        self.public = not self.public
        self.save()

    def set_public(self) -> None:

        self.public = True
        self.save()

    def set_private(self) -> None:

        self.public = False
        self.save()

    def get_absolute_url(self) -> str:
        return reverse('viewer:wanted-gallery', args=[str(self.id)])

    def save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super(WantedGallery, self).save(*args, **kwargs)


class FoundGallery(models.Model):
    wanted_gallery = models.ForeignKey(WantedGallery, on_delete=models.CASCADE)
    gallery = models.ForeignKey(Gallery, on_delete=models.CASCADE)
    match_accuracy = models.FloatField(
        'Match accuracy', blank=True, null=True, default=0.0)
    source = models.CharField(
        'Source', max_length=50, blank=True, null=True)
    create_date = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "Found galleries"
        ordering = ['-create_date']


class GalleryMatch(models.Model):
    wanted_gallery = models.ForeignKey(WantedGallery, on_delete=models.CASCADE)
    gallery = models.ForeignKey(Gallery, on_delete=models.CASCADE)
    match_accuracy = models.FloatField(
        'Match accuracy', blank=True, null=True, default=0.0)

    class Meta:
        verbose_name_plural = "Gallery matches"
        ordering = ['-match_accuracy']


class TweetPost(models.Model):
    tweet_id = models.BigIntegerField(blank=True, null=True)
    text = models.CharField(
        max_length=200, blank=True, null=True, default='')
    user = models.CharField(
        max_length=200, blank=True, null=True, default='')
    posted_date = models.DateTimeField('Posted date', blank=True, null=True, default=django_tz.now)
    media_url = models.CharField(
        max_length=200, blank=True, null=True, default='')


class Scheduler(models.Model):
    name = models.CharField(
        max_length=50)
    description = models.CharField(
        max_length=200, default='', blank=True)
    # module_location = models.CharField(max_length=500, default='')
    # class_name = models.CharField(max_length=50, default='')
    # enabled = models.BooleanField(default=False)
    # auto_start = models.BooleanField(default=False)
    # uses_web_queue = models.BooleanField(default=False)
    last_run = models.DateTimeField('last run', blank=True, null=True, default=django_tz.now)
    # timer = models.FloatField('Timer')
    # create_date = models.DateTimeField(auto_now_add=True)
    # last_modified = models.DateTimeField(auto_now=True, blank=True, null=True)

    def __str__(self) -> str:
        return self.name


class Provider(models.Model):
    name = models.CharField(max_length=100, help_text=_("User friendly name"))
    slug = models.SlugField(unique=True)
    home_page = models.URLField(blank=True, default='')
    description = models.CharField(max_length=500, blank=True, default='')
    information = models.TextField(blank=True, default='')

    create_date = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True, blank=True, null=True)

    def __str__(self) -> str:
        return self.name


class AttributeQuerySet(models.QuerySet):
    def fetch_value(self, name: str) -> typing.Optional[T]:
        attr = self.filter(name=name).first()

        if attr:
            return attr.value
        else:
            return None


class AttributeManager(models.Manager):
    def get_queryset(self) -> AttributeQuerySet:
        return AttributeQuerySet(self.model, using=self._db)

    def fetch_value(self, name: str) -> Optional[typing.Union[str, float, int, datetime, timedelta, bool]]:
        return self.get_queryset().fetch_value(name)


class Attribute(models.Model):
    objects = AttributeManager()

    class Meta:
        unique_together = ('name', 'provider')
        verbose_name_plural = "Provider attributes"

    TYPE_TEXT = 'text'
    TYPE_FLOAT = 'float'
    TYPE_INT = 'int'
    TYPE_DATE = 'date'
    TYPE_DURATION = 'duration'
    TYPE_BOOLEAN = 'bool'

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

    def _get_value(self) -> T:
        return getattr(self, 'value_%s' % self.data_type)

    def _set_value(self, new_value: str) -> None:
        setattr(self, 'value_%s' % self.data_type, new_value)

    def clean(self) -> None:
        # Only allowed types
        if self.data_type not in self.DATA_TYPES:
            raise ValidationError('{} must be one of: {}'.format(self.data_type, self.DATA_TYPES))
        # Don't allow empty string for all but text.
        if self.value == '' and self.data_type is not self.TYPE_TEXT:
            raise ValidationError('value_{0} cannot be blank when data type is {0}'.format(self.data_type))

    def save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        self.full_clean()
        super(Attribute, self).save(*args, **kwargs)

    value = property(_get_value, _set_value)


class EventLog(models.Model):
    content_type = models.ForeignKey(ContentType, null=True, on_delete=models.SET_NULL)
    object_id = models.PositiveIntegerField(null=True)
    content_object = GenericForeignKey('content_type', 'object_id')
    action = models.CharField(max_length=50, db_index=True)
    reason = models.CharField(max_length=200, blank=True, null=True, default='')
    data = models.CharField(max_length=200, blank=True, null=True, default='')
    result = models.CharField(max_length=200, blank=True, null=True, default='')
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    create_date = models.DateTimeField(default=django_tz.now, db_index=True)

    class Meta:
        verbose_name_plural = "Event logs"
        ordering = ['-create_date']
        permissions = (
            ("read_all_logs", "Can view a general log from all users"),
        )
