import itertools
import json
import typing

from django.db import models
from django.db.models import QuerySet
from django.urls import reverse
from django.utils.text import slugify

if typing.TYPE_CHECKING:
    from viewer.models.archive import Archive
    from viewer.models.gallery import Gallery


class ArchiveGroup(models.Model):
    title = models.CharField(max_length=500, blank=False, null=False)
    title_slug = models.SlugField(unique=True)
    details = models.TextField(blank=True, null=True, default="")
    archives: models.ManyToManyField = models.ManyToManyField(
        "viewer.Archive",
        related_name="archive_groups",
        blank=True,
        default="",
        through="ArchiveGroupEntry",
        through_fields=("archive_group", "archive"),
    )
    position = models.PositiveIntegerField(default=1)
    public = models.BooleanField(default=False)
    create_date = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True, blank=True, null=True)

    class Meta:
        verbose_name_plural = "Archive groups"
        ordering = ["position"]

    def __str__(self) -> str:
        return self.title

    def get_absolute_url(self) -> str:
        return reverse("viewer:archive-group", args=[str(self.title_slug)])

    def save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        if not self.title_slug:

            slug_candidate = slugify(self.title, allow_unicode=True)
            for i in itertools.count(1):
                if not ArchiveGroup.objects.filter(title_slug=slug_candidate).exists():
                    break
                slug_candidate = slugify("{}-{}".format(self.title, i), allow_unicode=True)

            self.title_slug = slug_candidate
        super().save(*args, **kwargs)

    def delete_text_report(self) -> str:
        data: dict[str, typing.Any] = {}
        if self.title:
            data["title"] = self.title
        if self.details:
            data["details"] = self.details
        if not data:
            return ""
        return json.dumps(data, ensure_ascii=False)



class ArchiveGroupEntryManager(models.Manager["ArchiveGroupEntry"]):
    def get_queryset(self) -> models.QuerySet:
        return models.QuerySet(self.model, using=self._db)

    def filter_by_authenticated_status(self, authenticated: bool, **kwargs: typing.Any) -> models.QuerySet:
        if authenticated:
            return self.get_queryset().filter(**kwargs)
        else:
            return self.get_queryset().filter(archive__public=True, **kwargs)


class ArchiveGroupEntry(models.Model):
    archive_group = models.ForeignKey(ArchiveGroup, on_delete=models.CASCADE)
    archive = models.ForeignKey("viewer.Archive", on_delete=models.CASCADE)
    title = models.CharField(max_length=500, blank=True, default="")
    position = models.PositiveIntegerField(blank=True, null=True)

    objects = ArchiveGroupEntryManager()

    class Meta:
        verbose_name_plural = "Archive group entries"
        ordering = ["position"]

    def save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        if not self.position:
            last_position = (
                ArchiveGroupEntry.objects.filter(archive_group=self.archive_group)
                .exclude(position__isnull=True)
                .order_by("-position")
                .first()
            )

            if last_position is None or last_position.position is None:
                position_candidate = 1
            else:
                position_candidate = last_position.position + 1

            self.position = position_candidate

        self.title = self.title or self.archive.title or self.archive.title or ""

        super().save(*args, **kwargs)



class ArchiveMatches(models.Model):
    archive = models.ForeignKey("viewer.Archive", on_delete=models.CASCADE)
    gallery = models.ForeignKey("viewer.Gallery", on_delete=models.CASCADE)
    match_type = models.CharField("Match type", max_length=40, blank=True, null=True, default="")
    match_accuracy = models.FloatField("Match accuracy", blank=True, null=True, default=0.0)

    class Meta:
        verbose_name_plural = "Archive matches"
        ordering = ["-match_accuracy"]



class GalleryMatchGroup(models.Model):
    title = models.CharField(max_length=500, blank=True, null=False, default="")
    galleries: models.ManyToManyField = models.ManyToManyField(
        "viewer.Gallery", related_name="gallery_group", blank=True, default="",
        through="GalleryMatchGroupEntry",
        through_fields=("gallery_match_group", "gallery"),
    )

    possible_matches: models.ManyToManyField = models.ManyToManyField(
        "viewer.Gallery",
        related_name="possible_match_group_matches",
        blank=True,
        default="",
        through="GalleryGroupPossibleMatches",
        through_fields=("gallery_match_group", "gallery"),
    )

    create_date = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True, blank=True, null=True)

    def save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        if not self.title and self.pk:
            first_post_gallery = self.galleries.all().first()
            if first_post_gallery:
                self.title = first_post_gallery.best_title
        super(GalleryMatchGroup, self).save(*args, **kwargs)

    def __str__(self) -> str:
        return self.title

    def get_absolute_url(self) -> str:
        return reverse("viewer:gallery-match-group", args=[str(self.pk)])

    def delete_text_report(self) -> str:
        data: dict[str, typing.Any] = {}
        if self.title:
            data["title"] = self.title
        if not data:
            return ""
        return json.dumps(data, ensure_ascii=False)

    def process_group(self):
        first_post_gallery = self.galleries.all().first()
        if first_post_gallery:
            other_galleries = self.galleries.exclude(gallerymatchgroupentry__gallery=first_post_gallery)
            for other_gallery in other_galleries:
                for archive in other_gallery.archive_set.all():
                    if archive.gallery != first_post_gallery:
                        archive.gallery = first_post_gallery
                        archive.save()

    def add_gallery(self, gallery: "Gallery") -> None:
        positions = GalleryMatchGroupEntry.objects.filter(gallery_match_group=self).values_list("gallery_position", flat=True)
        highest_position = max(positions)
        match_entry = GalleryMatchGroupEntry(gallery_match_group=self, gallery=gallery, gallery_position=highest_position + 1)
        match_entry.save()




class GalleryMatchGroupEntry(models.Model):
    gallery_match_group = models.ForeignKey(GalleryMatchGroup, on_delete=models.CASCADE)
    gallery = models.OneToOneField("viewer.Gallery", on_delete=models.CASCADE)
    gallery_position = models.PositiveIntegerField(default=1)
    create_date = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True, blank=True, null=True)

    class Meta:
        verbose_name_plural = "Gallery match group entries"
        constraints = [models.UniqueConstraint(fields=["gallery_match_group", "gallery_position"], name="unique_position_in_group")]
        ordering = ["gallery_position"]

    def save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super(GalleryMatchGroupEntry, self).save(*args, **kwargs)
        first_post_gallery = GalleryMatchGroupEntry.objects.filter(gallery_match_group=self.gallery_match_group).first()
        if first_post_gallery:
            for archive in self.gallery.archive_set.all():
                if archive.gallery != first_post_gallery.gallery:
                    archive.gallery = first_post_gallery.gallery
                    archive.save()



class GalleryGroupPossibleMatches(models.Model):
    gallery_match_group = models.ForeignKey(GalleryMatchGroup, on_delete=models.CASCADE)
    gallery = models.ForeignKey("viewer.Gallery", on_delete=models.CASCADE)
    match_type = models.CharField("Match type", max_length=40, blank=True, null=True, default="")
    match_accuracy = models.FloatField("Match accuracy", blank=True, null=True, default=0.0)

    class Meta:
        verbose_name_plural = "Gallery group possible matches"
        ordering = ["-match_accuracy"]

