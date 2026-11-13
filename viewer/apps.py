import os

from django.apps import AppConfig
from django.conf import settings


class ViewerConfig(AppConfig):
    name = "viewer"

    def ready(self) -> None:
        from . import handlers  # noqa: F401

        settings.PROVIDER_CONTEXT.register_providers(settings.PROVIDERS)
        if "PANDA_CONFIG_DIR" in os.environ:
            default_dir = os.environ["PANDA_CONFIG_DIR"]
        else:
            default_dir = None
        settings.CRAWLER_SETTINGS.load_config_from_file(default_dir=default_dir)
        from .models import Archive, Gallery

        for model in (Gallery, Archive):
            if hasattr(model, "history"):
                hist_model = model.history.model
                if hasattr(hist_model, "prev_record"):
                    orig_fget = hist_model.prev_record.fget

                    def make_getter(orig):
                        def get_prev_record(instance):
                            if hasattr(instance, "cached_prev_record"):
                                return instance.cached_prev_record
                            if hasattr(instance, "_cached_prev_record"):
                                return instance._cached_prev_record
                            val = orig(instance)
                            instance._cached_prev_record = val
                            return val
                        return get_prev_record

                    hist_model.prev_record = property(make_getter(orig_fget))
