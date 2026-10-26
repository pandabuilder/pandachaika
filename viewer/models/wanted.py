import io
import itertools
import logging
import os
import shutil
import typing
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime
from os.path import join as pjoin
from tempfile import NamedTemporaryFile
from urllib.parse import quote

import elasticsearch
from PIL import Image as PImage
from PIL import ImageFile, ImageOps
import requests
from django.conf import settings
from django.core.files import File
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q, QuerySet
from django.urls import reverse
import django.utils.timezone as django_tz

from core.base.comparison import get_list_closer_text_from_list
from core.base.image_ops import img_to_thumbnail
from core.base.types import GalleryData
from core.base.utilities import (
    clean_title,
    file_matches_any_filter,
    hamming_distance,
    request_with_retries,
    sha1_from_file_object,
)
from viewer.models.common import IMAGE_PHASH_BLACK, IMAGE_PHASH_WHITE, fs
from viewer.models.tag import SortedTagList, Tag
from viewer.utils import image_processing
from viewer.utils.elasticsearch import (
    add_gallery_data_to_match_index,
    match_expression_to_wanted_index,
    remove_gallery_from_match_index,
)
from viewer.utils.tags import sort_tags

if typing.TYPE_CHECKING:
    from viewer.models.gallery import Gallery

logger = logging.getLogger(__name__)


class WantedGalleryManager(models.Manager["WantedGallery"]):
    def not_found(self) -> QuerySet:
        return self.filter(found=False)

    def eligible_to_search(self) -> QuerySet:
        return self.filter(
            Q(Q(Q(release_date__lte=django_tz.now()) | Q(release_date__isnull=True)), should_search=True)
            & (Q(found=False) | Q(found=True, keep_searching=True))
            & Q(restricted_to_links=False)
        )



class WantedGallery(models.Model):
    objects = WantedGalleryManager()

    title = models.CharField(max_length=500, blank=True, null=True, default="")
    title_jpn = models.CharField(max_length=500, blank=True, null=True, default="")
    book_type = models.CharField("Book type", max_length=20, blank=True, null=True, default="")
    publisher = models.CharField("Publisher", max_length=20, blank=True, null=True, default="")
    public = models.BooleanField(default=False)
    release_date = models.DateTimeField("Release date", blank=True, null=True, default=django_tz.now)
    mentions = models.ManyToManyField("viewer.Mention", blank=True)
    artists = models.ManyToManyField("viewer.Artist", blank=True)
    cover_artist = models.ForeignKey(
        "viewer.Artist", blank=True, null=True, related_name="cover_artist", on_delete=models.SET_NULL
    )
    should_search = models.BooleanField("Should search", blank=True, default=False)
    keep_searching = models.BooleanField("Keep searching", blank=True, default=False)
    notify_when_found = models.BooleanField("Notify when found", default=True)
    reason = models.CharField("Reason", max_length=200, blank=True, null=True, default="backup")
    search_title = models.CharField(max_length=500, blank=True, default="")
    regexp_search_title = models.BooleanField("Regexp search title", blank=True, default=False)
    regexp_search_title_icase = models.BooleanField("Regexp search title case-insensitive", blank=True, default=False)
    unwanted_title = models.CharField(max_length=500, blank=True, default="")
    regexp_unwanted_title = models.BooleanField("Regexp unwanted title", blank=True, default=False)
    regexp_unwanted_title_icase = models.BooleanField(
        "Regexp unwanted title case-insensitive", blank=True, default=False
    )
    match_expression = models.CharField(max_length=2000, blank=True, null=True)
    wanted_page_count_lower = models.IntegerField(blank=True, default=0)
    wanted_page_count_upper = models.IntegerField(blank=True, default=0)
    wanted_tags: models.ManyToManyField = models.ManyToManyField("viewer.Tag", blank=True)
    wanted_tags_exclusive_scope = models.BooleanField(blank=True, default=False)
    exclusive_scope_name = models.CharField(max_length=200, blank=True, default="")
    wanted_tags_accept_if_none_scope = models.CharField(max_length=200, blank=True, default="")
    unwanted_tags: models.ManyToManyField = models.ManyToManyField("viewer.Tag", blank=True, related_name="unwanted_tags")
    category = models.CharField(max_length=20, blank=True, null=True, default="")
    categories: models.ManyToManyField = models.ManyToManyField("viewer.Category", blank=True)
    wanted_providers: models.ManyToManyField = models.ManyToManyField("viewer.Provider", blank=True)
    unwanted_providers: models.ManyToManyField = models.ManyToManyField(
        "viewer.Provider", blank=True, related_name="unwanted_providers"
    )
    wait_for_time = models.DurationField("Wait for time", blank=True, null=True)
    backlog_url_query = models.CharField(max_length=2000, blank=True, null=True)
    found_galleries: models.ManyToManyField = models.ManyToManyField(
        "viewer.Gallery",
        related_name="found_galleries",
        blank=True,
        through="FoundGallery",
        through_fields=("wanted_gallery", "gallery"),
    )

    possible_matches: models.ManyToManyField = models.ManyToManyField(
        "viewer.Gallery",
        related_name="gallery_matches",
        blank=True,
        through="GalleryMatch",
        through_fields=("wanted_gallery", "gallery"),
    )
    found = models.BooleanField("Found", blank=True, default=False)
    date_found = models.DateTimeField("Date found", blank=True, null=True)

    page_count = models.IntegerField("Page count", blank=True, null=True, default=0)
    create_date = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True, blank=True, null=True)

    add_to_archive_group = models.ForeignKey("viewer.ArchiveGroup", blank=True, null=True, on_delete=models.SET_NULL)

    restricted_to_links = models.BooleanField("Restricted to MonitoredLinks", blank=True, default=False)

    class Meta:
        verbose_name_plural = "Wanted galleries"
        ordering = ["-release_date"]
        permissions = (
            ("edit_search_filter_wanted_gallery", "Can edit wanted galleries search filter parameters"),
            ("edit_search_dates_wanted_gallery", "Can edit wanted galleries search date parameters"),
            ("edit_search_notify_wanted_gallery", "Can edit wanted galleries search notify parameter"),
        )

    def __str__(self) -> str:
        return self.title or self.title_jpn or ""

    def mentions_str(self) -> str:
        lst = [str(x) for x in self.mentions.all()]
        return ", ".join(lst)

    def mentions_list(self) -> list[str]:
        lst = [str(x) for x in self.mentions.all()]
        return lst

    def wanted_tags_list(self) -> list[str]:
        lst = [str(x) for x in self.wanted_tags.all()]
        return lst

    def sorted_wanted_tags_list(self) -> SortedTagList:
        return sort_tags(self.wanted_tags.all())

    def sorted_unwanted_tags_list(self) -> SortedTagList:
        return sort_tags(self.unwanted_tags.all())

    def categories_list(self) -> list[str]:
        lst = [str(x) for x in self.categories.all()]
        return lst

    def unwanted_tags_list(self) -> list[str]:
        lst = [str(x) for x in self.unwanted_tags.all()]
        return lst

    def calculate_nearest_release_date(self) -> None:
        if not self.mentions:
            return
        mention_dates_objs = self.mentions.exclude(release_date=None).values_list("release_date", flat=True)
        if not mention_dates_objs:
            return
        mention_dates = [x.date() for x in mention_dates_objs if x is not None]
        date_count: typing.Counter[date] = Counter(mention_dates)
        most_occurring_date = date_count.most_common(1)[0][0]
        self.release_date = datetime.combine(most_occurring_date, datetime.min.time())
        self.save()

    def create_gallery_matches_internally(self, provider_filter: str = "") -> None:

        matched_galleries = self.get_matching_galleries(provider_filter)

        for matched_gallery in matched_galleries:
            GalleryMatch.objects.get_or_create(
                wanted_gallery=self, gallery=matched_gallery, defaults={"match_accuracy": 1}
            )

    def create_found_galleries_internally(self) -> None:

        matched_galleries = self.get_matching_galleries()

        for matched_gallery in matched_galleries:
            self.found = True
            self.date_found = django_tz.now()
            self.save()
            FoundGallery.objects.get_or_create(wanted_gallery=self, gallery=matched_gallery)

    def get_matching_galleries(self, provider_filter: str = "") -> list["Gallery"]:
        from viewer.models.gallery import Gallery

        matching_galleries: list["Gallery"] = []

        galleries = Gallery.objects.eligible_for_use().filter(~Q(foundgallery__wanted_gallery=self))

        if provider_filter:
            galleries = galleries.filter(provider=provider_filter)

        has_wanted_tags = bool(self.wanted_tags.all())
        has_unwanted_tags = bool(self.unwanted_tags.all())
        wanted_providers_count = self.wanted_providers.count()
        unwanted_providers_count = self.unwanted_providers.count()
        categories_count = self.categories.count()

        if self.search_title or self.unwanted_title:
            q_objects_search_title = Q()
            q_objects_unwanted_title = Q()
            if self.search_title:
                if self.regexp_search_title:
                    if self.regexp_search_title_icase:
                        q_objects_search_title.add(Q(title__iregex=self.search_title), Q.OR)
                        q_objects_search_title.add(Q(title_jpn__iregex=self.search_title), Q.OR)
                    else:
                        q_objects_search_title.add(Q(title__regex=self.search_title), Q.OR)
                        q_objects_search_title.add(Q(title_jpn__regex=self.search_title), Q.OR)
                else:
                    q_formatted = "%" + self.search_title.replace(" ", "%") + "%"
                    q_objects_search_title.add(
                        Q(Q(title__ss=q_formatted), ~Q(title__exact=""), Q(title__isnull=False)), Q.OR
                    )
                    q_objects_search_title.add(
                        Q(Q(title_jpn__ss=q_formatted), ~Q(title_jpn__exact=""), Q(title_jpn__isnull=False)), Q.OR
                    )

            if self.unwanted_title:
                if self.regexp_unwanted_title:
                    if self.regexp_unwanted_title_icase:
                        q_objects_unwanted_title.add(~Q(title__iregex=self.unwanted_title), Q.AND)
                        q_objects_unwanted_title.add(~Q(title_jpn__iregex=self.unwanted_title), Q.AND)
                    else:
                        q_objects_unwanted_title.add(~Q(title__regex=self.unwanted_title), Q.AND)
                        q_objects_unwanted_title.add(~Q(title_jpn__regex=self.unwanted_title), Q.AND)
                else:
                    q_formatted = "%" + self.unwanted_title.replace(" ", "%") + "%"
                    q_objects_unwanted_title.add(~Q(title__ss=q_formatted), Q.AND)
                    q_objects_unwanted_title.add(~Q(title_jpn__ss=q_formatted), Q.AND)

            galleries = galleries.filter(q_objects_search_title).filter(q_objects_unwanted_title)

        if self.wait_for_time:
            galleries = galleries.filter(Q(posted__isnull=True) | Q(posted__lte=django_tz.now() - self.wait_for_time))

        # if has_wanted_tags:
        #     galleries = galleries.filter(tags__in=self.wanted_tags.all())
        #
        # if has_unwanted_tags:
        #     galleries = galleries.filter(~Q(tags__in=self.unwanted_tags.all()))
        #
        if self.category:
            galleries = galleries.filter(category__iexact=self.category)

        if self.wanted_page_count_upper:
            galleries = galleries.filter(filecount__lte=self.wanted_page_count_upper)

        if self.wanted_page_count_lower:
            galleries = galleries.filter(filecount__gte=self.wanted_page_count_lower)

        # Disabled because wanted_providers superseeds provider
        # if self.provider:
        #     galleries = galleries.filter(provider__iexact=self.provider)
        if categories_count:
            galleries = galleries.filter(category__in=self.categories.all().values_list("name", flat=True))

        if wanted_providers_count:
            galleries = galleries.filter(provider__in=[x.slug for x in self.wanted_providers.all()])

        if unwanted_providers_count:
            galleries = galleries.exclude(provider__in=[x.slug for x in self.unwanted_providers.all()])

        if has_wanted_tags:
            for wanted_tag in self.wanted_tags.all():
                galleries = galleries.filter(tags__id__exact=wanted_tag.id)

        if has_unwanted_tags:
            for unwanted_tag in self.unwanted_tags.all():
                galleries = galleries.exclude(tags__id__exact=unwanted_tag.id)

        for gallery in galleries:
            accepted = True
            if has_wanted_tags:
                if not set(self.wanted_tags_list()).issubset(set(gallery.tag_list())):
                    accepted = False
                # Do not accept galleries that have more than 1 tag in the same wanted tag scope.
                if accepted & self.wanted_tags_exclusive_scope:
                    accepted_tags = set(self.wanted_tags_list()).intersection(set(gallery.tag_list()))
                    gallery_tags_scopes = [x.split(":", maxsplit=1)[0] for x in gallery.tag_list() if len(x) > 1]
                    wanted_gallery_tags_scopes = [x.split(":", maxsplit=1)[0] for x in accepted_tags if len(x) > 1]
                    scope_count: dict[str, int] = defaultdict(int)
                    for scope_name in gallery_tags_scopes:
                        if scope_name in wanted_gallery_tags_scopes:
                            if self.exclusive_scope_name:
                                if self.exclusive_scope_name == scope_name:
                                    scope_count[scope_name] += 1
                            else:
                                scope_count[scope_name] += 1
                    for scope, count in scope_count.items():
                        if count > 1:
                            accepted = False
                # Review based on 'accept if none' scope.
                if not accepted and self.wanted_tags_accept_if_none_scope:
                    missing_tags = set(self.wanted_tags_list()).difference(set(gallery.tag_list()))
                    # If all the missing tags start with the parameter,
                    # and no other tag is in gallery with this parameter, mark as accepted
                    scope_formatted = self.wanted_tags_accept_if_none_scope + ":"
                    if all(x.startswith(scope_formatted) for x in missing_tags) and not any(
                        x.startswith(scope_formatted) for x in gallery.tag_list()
                    ):
                        accepted = True
            if accepted & has_unwanted_tags:
                if any(item in gallery.tag_list() for item in self.unwanted_tags_list()):
                    accepted = False

            if self.match_expression:
                expression_matched = self.evaluate_match_expression(gallery)
                if not expression_matched:
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
        return reverse("viewer:wanted-gallery", args=[str(self.id)])

    def get_col_absolute_url(self) -> str:
        return reverse("viewer:col-wanted-gallery", args=[str(self.id)])

    def save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super(WantedGallery, self).save(*args, **kwargs)

    def evaluate_match_expression(self, gallery: "GalleryData | Gallery") -> bool:
        if not self.match_expression:
            return False
        index_uuid = add_gallery_data_to_match_index(gallery)
        if not index_uuid:
            return False
        match_result = match_expression_to_wanted_index(self.match_expression, index_uuid)

        remove_gallery_from_match_index(index_uuid)

        if not match_result:
            return False
        return True



class FoundGallery(models.Model):
    wanted_gallery = models.ForeignKey(WantedGallery, on_delete=models.CASCADE)
    gallery = models.ForeignKey("viewer.Gallery", on_delete=models.CASCADE)
    match_accuracy = models.FloatField("Match accuracy", blank=True, null=True, default=0.0)
    source = models.CharField("Source", max_length=50, blank=True, null=True)
    create_date = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "Found galleries"
        ordering = ["-create_date"]



class GalleryMatch(models.Model):
    wanted_gallery = models.ForeignKey(WantedGallery, on_delete=models.CASCADE)
    gallery = models.ForeignKey("viewer.Gallery", on_delete=models.CASCADE)
    match_accuracy = models.FloatField("Match accuracy", blank=True, null=True, default=0.0)

    class Meta:
        verbose_name_plural = "Gallery matches"
        ordering = ["-match_accuracy"]



def upload_wanted_image_handler(instance: "WantedImage", filename: str) -> str:
    return "images/wanted_images/{uuid}_{file}".format(
        file=filename,
        uuid=uuid.uuid4(),
    )


def upload_wanted_image_thumbnail_handler(instance: "WantedImage", filename: str) -> str:
    return "images/wanted_images/{id}/tn/{file}".format(id=instance.id, file=filename)


class WantedImage(models.Model):

    image = models.ImageField(
        upload_to=upload_wanted_image_handler, max_length=500, height_field="image_height", width_field="image_width"
    )
    active = models.BooleanField(default=True)
    image_name = models.CharField(max_length=500, blank=True, null=True)
    image_height = models.PositiveIntegerField(null=True)
    image_width = models.PositiveIntegerField(null=True)
    image_format = models.CharField(max_length=50, blank=True, null=True)
    image_mode = models.CharField(max_length=50, blank=True, null=True)
    image_size = models.PositiveIntegerField(null=True)
    thumbnail_height = models.PositiveIntegerField(blank=True, null=True)
    thumbnail_width = models.PositiveIntegerField(blank=True, null=True)
    thumbnail = models.ImageField(
        upload_to=upload_wanted_image_thumbnail_handler,
        blank=True,
        null=True,
        max_length=500,
        height_field="thumbnail_height",
        width_field="thumbnail_width",
    )
    sha1 = models.CharField(max_length=50, blank=True, null=True)
    match_threshold = models.FloatField(default=0.8, validators=[MaxValueValidator(1.0), MinValueValidator(0.0)])

    minimum_features = models.PositiveIntegerField(
        default=0,
    )

    restrict_by_homogeneity = models.BooleanField(default=False)

    match_height = models.PositiveIntegerField(blank=True, null=True)
    match_width = models.PositiveIntegerField(blank=True, null=True)
    mark_priority = models.FloatField(blank=True, default=1.0)

    # def delete_plus_files(self) -> None:
    #     self.image.delete(save=False)
    #     self.thumbnail.delete(save=False)
    #     self.delete()

    def __str__(self) -> str:
        if self.image_name:
            return self.image_name
        elif self.image.name:
            return self.image.name
        else:
            return ''

    # def get_absolute_url(self) -> str:
    #     return reverse('viewer:wanted-image', args=[str(self.id)])

    def get_image_url(self) -> str:
        return self.image.url

    def delete(self, *args: typing.Any, **kwargs: typing.Any) -> tuple[int, dict[str, int]]:
        self.image.delete(save=False)
        self.thumbnail.delete(save=False)
        return super(WantedImage, self).delete(*args, **kwargs)

    def save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super(WantedImage, self).save(*args, **kwargs)
        if self.image and os.path.isfile(self.image.path):
            im = PImage.open(self.image.path)
            self.image_format = im.format
            self.image_mode = im.mode
            self.image_size = self.image.size
            if not self.image_name:
                self.image_name = self.image.name
            self.sha1 = sha1_from_file_object(self.image.path)
            super(WantedImage, self).save(*args, **kwargs)
        if not self.thumbnail:
            self.regen_thumbnail()

    def regen_thumbnail(self):
        if self.image and os.path.isfile(self.image.path):
            im = PImage.open(self.image.path)
            size = im.size
            thumb_img_name = upload_wanted_image_thumbnail_handler(self, os.path.basename(self.image.name))
            if not self.match_width or not self.match_height:
                to_use_width = size[0]
                to_use_height = size[1]
            else:
                to_use_width = self.match_width
                to_use_height = self.match_height
            im = img_to_thumbnail(im, to_use_width, to_use_height)
            thumb_fn = pjoin(settings.MEDIA_ROOT, thumb_img_name)
            os.makedirs(os.path.dirname(thumb_fn), exist_ok=True)
            im.save(thumb_fn, "JPEG")
            im_size = im.size
            im.close()
            self.thumbnail = thumb_img_name
            if im_size:
                self.thumbnail_width, self.thumbnail_height = im_size
            super(WantedImage, self).save()

    def simple_save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super(WantedImage, self).save(*args, **kwargs)


def upload_found_wanted_image_thumbnail_handler(instance: "FoundWantedImageOnArchive", filename: str) -> str:
    return "images/wanted_images_result/{id}/{filename}".format(
        id=instance.id,
        filename=filename,
    )



class FoundWantedImageOnArchive(models.Model):

    wanted_image = models.ForeignKey(WantedImage, on_delete=models.CASCADE)
    archive = models.ForeignKey("viewer.Archive", on_delete=models.CASCADE)

    comment = models.CharField(max_length=250, blank=True, null=True)

    result_image_height = models.PositiveIntegerField(blank=True, null=True)
    result_image_width = models.PositiveIntegerField(blank=True, null=True)
    result_image = models.ImageField(
        upload_to=upload_found_wanted_image_thumbnail_handler,
        blank=True,
        null=True,
        max_length=500,
        height_field="result_image_height",
        width_field="result_image_width",
    )

    def __str__(self) -> str:
        if self.result_image.name:
            return self.result_image.name
        else:
            return ''

    def get_image_url(self) -> str:
        return self.result_image.url

    def delete(self, *args: typing.Any, **kwargs: typing.Any) -> tuple[int, dict[str, int]]:
        self.result_image.delete(save=False)
        return super(FoundWantedImageOnArchive, self).delete(*args, **kwargs)

    def save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super(FoundWantedImageOnArchive, self).save(*args, **kwargs)

    def save_result_image(self, result_image: PImage.Image) -> None:
        size = result_image.size
        img_name = upload_found_wanted_image_thumbnail_handler(self, "result_image.jpg")
        to_use_width = size[0]
        to_use_height = size[1]
        im = img_to_thumbnail(result_image, to_use_width, to_use_height)
        thumb_fn = pjoin(settings.MEDIA_ROOT, img_name)
        os.makedirs(os.path.dirname(thumb_fn), exist_ok=True)
        im.save(thumb_fn, "JPEG")
        self.result_image.name = img_name
        super(FoundWantedImageOnArchive, self).save()

    def simple_save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super(FoundWantedImageOnArchive, self).save(*args, **kwargs)



