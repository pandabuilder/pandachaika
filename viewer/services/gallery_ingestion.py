import logging
from os.path import basename
from tempfile import NamedTemporaryFile
import typing
from urllib.parse import urlparse

from django.conf import settings
from django.core.files import File
from django.db import transaction
import requests

from core.base.utilities import request_with_retries

if typing.TYPE_CHECKING:
    from viewer.models import Gallery

logger = logging.getLogger(__name__)


class GalleryIngestionService:
    """Service handling remote thumbnail downloads, perceptual hashing, and ingestion tasks for galleries."""

    @classmethod
    def fetch_thumbnail(
        cls,
        gallery: "Gallery",
        force_redownload: bool = False,
        force_provider: str | None = None,
    ) -> bool:
        """Download the gallery's remote thumbnail and compute its perceptual hash outside Model.save()."""
        from viewer.models.gallery import Gallery

        if gallery.thumbnail_url and (not gallery.thumbnail or force_redownload):
            request_dict: dict[str, typing.Any] = {
                "stream": True,
                "headers": settings.CRAWLER_SETTINGS.requests_headers,
                "timeout": settings.CRAWLER_SETTINGS.timeout_timer,
            }

            provider_to_use = force_provider if force_provider else gallery.provider

            if gallery.provider in settings.CRAWLER_SETTINGS.providers and provider_to_use in settings.CRAWLER_SETTINGS.providers:
                if settings.CRAWLER_SETTINGS.providers[provider_to_use].cookies:
                    request_dict["cookies"] = settings.CRAWLER_SETTINGS.providers[provider_to_use].cookies
                if settings.CRAWLER_SETTINGS.providers[provider_to_use].proxies:
                    request_dict["proxies"] = settings.CRAWLER_SETTINGS.providers[provider_to_use].proxies
                if settings.CRAWLER_SETTINGS.providers[provider_to_use].timeout_timer:
                    request_dict["timeout"] = settings.CRAWLER_SETTINGS.providers[provider_to_use].timeout_timer
            response = request_with_retries(
                gallery.thumbnail_url,
                request_dict,
                post=False,
            )
            if response:
                disassembled = urlparse(gallery.thumbnail_url)
                file_name = basename(disassembled.path)
                if not file_name:
                    file_name = "g_thumbnail"
                lf = NamedTemporaryFile()
                if response.status_code == requests.codes.ok:
                    for chunk in response.iter_content(chunk_size=1024):
                        if chunk:  # filter out keep-alive new chunks
                            lf.write(chunk)
                    gallery.thumbnail.save(file_name, File(lf), save=False)
                lf.close()

                super(Gallery, gallery).save(force_update=True)
                gallery.schedule_index_update()

                if settings.CRAWLER_SETTINGS.auto_phash_images:
                    gallery.create_or_update_thumbnail_hash("phash")

                return True

        return False

    @classmethod
    def schedule_thumbnail_fetch(
        cls,
        gallery: "Gallery",
        force_redownload: bool = False,
        force_provider: str | None = None,
    ) -> None:
        """Schedule remote thumbnail download via transaction.on_commit() so DB transactions do not block on HTTP I/O."""
        if not gallery.thumbnail_url or (gallery.thumbnail and not force_redownload):
            return
        if gallery.pk is None:
            return
        conn = transaction.get_connection()
        key = ("gallery_thumb", gallery.pk, force_redownload, force_provider)
        if conn.in_atomic_block and any(
            getattr(func, "_gallery_thumb_key", None) == key for _, func, _ in conn.run_on_commit
        ):
            return

        def _on_commit() -> None:
            cls.fetch_thumbnail(gallery, force_redownload=force_redownload, force_provider=force_provider)

        setattr(_on_commit, "_gallery_thumb_key", key)
        transaction.on_commit(_on_commit)
