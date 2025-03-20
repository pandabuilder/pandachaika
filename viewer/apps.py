import os

from django.apps import AppConfig
from django.conf import settings


class ViewerConfig(AppConfig):
    name = "viewer"

    def ready(self) -> None:
        from . import handlers

        settings.PROVIDER_CONTEXT.register_providers(settings.PROVIDERS)
        if "PANDA_CONFIG_DIR" in os.environ:
            default_dir = os.environ["PANDA_CONFIG_DIR"]
        else:
            default_dir = None
        settings.CRAWLER_SETTINGS.load_config_from_file(default_dir=default_dir)
        from .models import Archive, Gallery, FoundGallery, WantedGallery, ArchiveManageEntry, DownloadEvent

        settings.CRAWLER_SETTINGS.set_models(
            Gallery, Archive, FoundGallery, WantedGallery, ArchiveManageEntry, DownloadEvent
        )
