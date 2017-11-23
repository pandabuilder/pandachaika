from django.apps import AppConfig
from django.conf import settings


class ViewerConfig(AppConfig):
    name = 'viewer'

    def ready(self):
        settings.PROVIDER_CONTEXT.register_providers(settings.PROVIDERS)
        settings.CRAWLER_SETTINGS.load_config_from_file()
