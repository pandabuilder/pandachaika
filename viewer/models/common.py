import typing
from PIL import Image as PImage
from PIL import ImageFile
import django.db.models.options as options
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.files.storage import FileSystemStorage
from django.db import models
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models import Lookup
from django.db.models.sql.compiler import SQLCompiler

options.DEFAULT_NAMES += ("es_index_name", "es_mapping")
PImage.MAX_IMAGE_PIXELS = 200000000
EMPTY_TITLE = "No title"
ImageFile.LOAD_TRUNCATED_IMAGES = True

IMAGE_PHASH_BLACK = "0000000000000000"
IMAGE_PHASH_WHITE = "8000000000000000"


class OriginalFilenameFileSystemStorage(FileSystemStorage):

    def get_valid_name(self, name):
        """
        Return a filename, based on the provided filename, that's suitable for
        use in the target storage system.
        """
        return name



fs = OriginalFilenameFileSystemStorage()


class SpacedSearch(Lookup):

    lookup_name = "ss"

    def as_sql(self, compiler: SQLCompiler, connection: BaseDatabaseWrapper) -> tuple[str, typing.Any]:
        lhs, lhs_params = self.process_lhs(compiler, connection)
        rhs, rhs_params = self.process_rhs(compiler, connection)
        params = tuple(lhs_params) + tuple(rhs_params)
        return "%s LIKE %s" % (lhs, rhs), params

    def as_postgresql(self, compiler: SQLCompiler, connection: BaseDatabaseWrapper) -> tuple[str, typing.Any]:
        lhs, lhs_params = self.process_lhs(compiler, connection)
        rhs, rhs_params = self.process_rhs(compiler, connection)
        params = tuple(lhs_params) + tuple(rhs_params)
        return "%s ILIKE %s" % (lhs, rhs), params


models.CharField.register_lookup(SpacedSearch)
models.FileField.register_lookup(SpacedSearch)


class ItemProperties(models.Model):

    class Meta:
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]
        unique_together = ("content_type", "object_id", "name")

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    content_object = GenericForeignKey("content_type", "object_id")
    name = models.CharField(max_length=50)
    tag = models.SlugField()
    value = models.CharField(max_length=100)



