import copy
import logging
from typing import Any, Optional, cast

from core.base.setup import Settings

logger = logging.getLogger(__name__)

# Attributes that are sensitive credentials, runtime environment/paths,
# or internal process state and should not be serialized into task options.
EXCLUDED_SETTINGS_ATTRIBUTES = {
    # Credentials & security
    "config",
    "database",
    "django_secret_key",
    "django_debug_mode",
    "ftps",
    "ftp_configs",
    "torrent",
    "remote_site",
    # System / environment / paths
    "db_engine",
    "MEDIA_ROOT",
    "STATIC_ROOT",
    "default_dir",
    "log_location",
    "log_level",
    "disable_sql_log",
    # Runtime & worker process control
    "is_worker_process",
    "fatal",
    "cherrypy_auto_restart",
    "timed_downloader_startup",
    "download_progress_checker_startup",
    "webserver",
    # Class attributes / internal contexts
    "provider_context",
    "workers",
    "gallery_model",
    "archive_model",
    "found_gallery_model",
    "wanted_gallery_model",
    "archive_manage_entry_model",
    "download_event_model",
}


def _to_serializable_value(val: Any) -> Any:
    """Recursively convert values, including objects with __slots__ or __dict__, into JSON-serializable types."""
    if val is None or isinstance(val, (int, float, str, bool)):
        return val
    if isinstance(val, (list, tuple, set)):
        return [_to_serializable_value(x) for x in val]
    if isinstance(val, dict):
        return {str(k): _to_serializable_value(v) for k, v in val.items()}
    if hasattr(val, "is_authenticated") and hasattr(val, "pk"):
        return val.pk if val.is_authenticated else None
    if hasattr(val, "__slots__"):
        return {k: _to_serializable_value(getattr(val, k)) for k in val.__slots__ if hasattr(val, k)}
    if hasattr(val, "__dict__"):
        return {k: _to_serializable_value(v) for k, v in vars(val).items() if k not in EXCLUDED_SETTINGS_ATTRIBUTES}
    return str(val)


def _serialize_provider_settings(prov_settings: Any) -> dict[str, Any]:
    """Serialize provider settings object or dict into a dictionary."""
    if prov_settings is None:
        return {}
    if isinstance(prov_settings, dict):
        return {str(k): _to_serializable_value(v) for k, v in prov_settings.items()}
    prov_dict: dict[str, Any] = {}
    if hasattr(prov_settings, "__slots__"):
        for k in prov_settings.__slots__:
            if hasattr(prov_settings, k):
                prov_dict[k] = _to_serializable_value(getattr(prov_settings, k))
    elif hasattr(prov_settings, "__dict__"):
        for k, v in vars(prov_settings).items():
            prov_dict[k] = _to_serializable_value(v)
    return prov_dict


def serialize_settings_options(options: Any) -> dict[str, Any]:
    """Serialize Settings, sub-settings, or dict into a JSON-serializable dictionary."""
    if options is None:
        return {}

    if isinstance(options, dict):
        clean_dict: dict[str, Any] = {}
        for k, v in options.items():
            if k == "archive_user" and v and getattr(v, "is_authenticated", False) and getattr(v, "pk", None):
                clean_dict["user_id"] = v.pk
            else:
                clean_dict[str(k)] = _to_serializable_value(v)
        return clean_dict

    if hasattr(options, "__dict__") or hasattr(options, "__slots__"):
        clean_dict = {}

        if hasattr(options, "__dict__"):
            raw_items = vars(options).items()
        elif hasattr(options, "__slots__"):
            raw_items = [(k, getattr(options, k)) for k in options.__slots__ if hasattr(options, k)]
        else:
            raw_items = []

        for key, val in raw_items:
            if key in EXCLUDED_SETTINGS_ATTRIBUTES:
                continue

            if key == "archive_user":
                if val and getattr(val, "is_authenticated", False) and getattr(val, "pk", None):
                    clean_dict["user_id"] = val.pk
                continue

            if key == "providers" and isinstance(val, dict):
                providers_data: dict[str, Any] = {}
                for prov_name, prov_settings in val.items():
                    prov_serialized = _serialize_provider_settings(prov_settings)
                    if prov_serialized:
                        providers_data[prov_name] = prov_serialized
                if providers_data:
                    clean_dict["providers"] = providers_data
                continue

            clean_dict[key] = _to_serializable_value(val)

        return clean_dict

    return {}


def deserialize_settings_options(base_settings: Optional[Settings], options: Optional[dict[str, Any]]) -> Settings:
    """Create a new Settings instance and apply serialized options."""
    if base_settings is not None:
        try:
            new_settings = copy.deepcopy(base_settings)
        except Exception:
            config = getattr(base_settings, "config", None)
            if config:
                new_settings = Settings(load_from_config=config)
            else:
                new_settings = Settings(load_from_disk=False)
    else:
        new_settings = Settings(load_from_disk=False)

    if base_settings is not None:
        if not new_settings.providers and hasattr(base_settings, "providers"):
            new_settings.providers = cast(Any, dict(base_settings.providers))
        if not new_settings.downloaders and hasattr(base_settings, "downloaders"):
            new_settings.downloaders = dict(base_settings.downloaders)
        if not new_settings.custom_downloaders and hasattr(base_settings, "custom_downloaders"):
            new_settings.custom_downloaders = dict(base_settings.custom_downloaders)
        if not new_settings.custom_downloader_providers and hasattr(base_settings, "custom_downloader_providers"):
            new_settings.custom_downloader_providers = dict(base_settings.custom_downloader_providers)

    if not options or not isinstance(options, dict):
        return new_settings

    if "user_id" in options and options["user_id"] is not None:
        try:
            from django.contrib.auth import get_user_model
            User = get_user_model()
            user = User.objects.filter(pk=options["user_id"]).first()
            if user:
                new_settings.archive_user = user
        except Exception:
            pass

    for k, v in options.items():
        if k in ("user_id", "archive_user"):
            continue

        if k == "replace_metadata":
            new_settings.replace_metadata = bool(v)
            if hasattr(new_settings, "config") and isinstance(new_settings.config, dict):
                new_settings.config.setdefault("allowed", {})["replace_metadata"] = "yes" if v else "no"
            continue

        if k == "downloaders" and isinstance(v, dict):
            for dl_name, priority in v.items():
                try:
                    new_settings.downloaders[dl_name] = int(priority)
                except (ValueError, TypeError):
                    new_settings.downloaders[dl_name] = priority
            continue

        if k == "matchers" and isinstance(v, dict):
            for m_name, priority in v.items():
                try:
                    new_settings.matchers[m_name] = int(priority)
                except (ValueError, TypeError):
                    new_settings.matchers[m_name] = priority
            continue

        if k == "providers" and isinstance(v, dict):
            for prov_name, prov_data in v.items():
                if prov_name in new_settings.providers and isinstance(prov_data, dict):
                    prov_obj = new_settings.providers[prov_name]
                    for p_attr, p_val in prov_data.items():
                        if hasattr(prov_obj, p_attr):
                            setattr(prov_obj, p_attr, p_val)
            continue

        target_obj = getattr(new_settings, k, None)
        if target_obj is not None and (hasattr(target_obj, "__slots__") or hasattr(target_obj, "__dict__")) and isinstance(v, dict):
            for sub_k, sub_v in v.items():
                if hasattr(target_obj, sub_k):
                    setattr(target_obj, sub_k, sub_v)
            continue

        if isinstance(v, list):
            setattr(new_settings, k, list(v))
        elif isinstance(v, dict):
            setattr(new_settings, k, dict(v))
        else:
            setattr(new_settings, k, v)

    return new_settings
