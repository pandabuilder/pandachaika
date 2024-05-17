from django.apps import AppConfig
from django.conf import settings


class ViewerConfig(AppConfig):
    name = 'viewer'

    def ready(self) -> None:
        from . import handlers
        settings.PROVIDER_CONTEXT.register_providers(settings.PROVIDERS)
        settings.CRAWLER_SETTINGS.load_config_from_file()
        from .models import Archive, Gallery, FoundGallery, WantedGallery, ArchiveManageEntry, DownloadEvent

        settings.CRAWLER_SETTINGS.set_models(Gallery, Archive, FoundGallery, WantedGallery, ArchiveManageEntry, DownloadEvent)
