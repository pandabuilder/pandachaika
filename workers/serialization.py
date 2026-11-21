import copy
from collections.abc import Iterable
from datetime import timedelta
import logging
from typing import Any, cast

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
    # System / environment / paths
    "db_engine",
    "MEDIA_ROOT",
    "STATIC_ROOT",
    "default_dir",
    "log_location",
    "log_level",
    "sql_log_level",
    "disable_sql_log",
    # Runtime & worker process control
    "is_worker_process",
    "current_task_type",
    "current_task_id",
    "current_task_priority",
    "fatal",
    "cherrypy_auto_restart",
    "download_progress_checker_startup",
    "archive_download_queue_startup",
    "webserver",
    # Class attributes / internal contexts
    "provider_context",
    "workers",
    "activity_callback",
    "cancel_event",
    "is_cancelled",
    "crawl_batch_collector",
    "crawl_batch",
    "crawl_batch_stats",
    "crawl_batch_summary_logged",
    # Section models backing the top-level Settings attributes; those attributes are serialized by name
    # instead (see Settings.attribute_items), so credentials such as general_model.django_secret_key stay out.
    "general_model",
    "allowed_model",
    "locations_model",
    "match_params_model",
    "_base_settings",
}

# Keys written by older versions, when Settings also kept the section models next to dict copies. Their content
# is duplicated by the top-level keys, so they are ignored when deserializing queued tasks.
LEGACY_SECTION_MODEL_KEYS = {
    "general_model",
    "allowed_model",
    "locations_model",
    "match_params_model",
    "database_model",
    "torrent_model",
    "ftps_model",
    "remote_site_model",
}


def _to_serializable_value(val: Any) -> Any:
    """Recursively convert values, including objects with __slots__ or __dict__, into JSON-serializable types."""
    if val is None or isinstance(val, (int, float, str, bool)):
        return val
    if isinstance(val, timedelta):
        return val.total_seconds()
    if isinstance(val, (list, tuple, set)):
        return [_to_serializable_value(x) for x in val]
    if isinstance(val, dict):
        return {str(k): _to_serializable_value(v) for k, v in val.items()}
    if hasattr(val, "is_authenticated") and hasattr(val, "pk"):
        return val.pk if val.is_authenticated else None
    if hasattr(val, "model_dump") and callable(val.model_dump):
        return _to_serializable_value(val.model_dump())
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
                if k == "monitored_link":
                    ml_val = getattr(prov_settings, k)
                    if ml_val and hasattr(ml_val, "pk"):
                        prov_dict["monitored_link_id"] = ml_val.pk
                else:
                    prov_dict[k] = _to_serializable_value(getattr(prov_settings, k))
    elif hasattr(prov_settings, "__dict__"):
        for k, v in vars(prov_settings).items():
            if k == "monitored_link":
                if v and hasattr(v, "pk"):
                    prov_dict["monitored_link_id"] = v.pk
            elif k == "_proxy":
                prov_dict["proxy"] = _to_serializable_value(v)
            else:
                prov_dict[k] = _to_serializable_value(v)
    if "proxy" not in prov_dict and hasattr(prov_settings, "proxy"):
        prov_dict["proxy"] = _to_serializable_value(getattr(prov_settings, "proxy"))
    if hasattr(prov_settings, "proxies") and getattr(prov_settings, "proxies"):
        prov_dict["proxies"] = _to_serializable_value(getattr(prov_settings, "proxies"))
    return prov_dict


def _serialize_settings_object(options: Any) -> dict[str, Any]:
    clean_dict: dict[str, Any] = {}

    raw_items: Iterable[tuple[str, Any]]
    if isinstance(options, Settings):
        raw_items = options.attribute_items()
    elif hasattr(options, "__dict__"):
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

        if key == "monitored_link":
            if val and getattr(val, "pk", None):
                clean_dict["monitored_link_id"] = val.pk
            continue

        if key == "_proxy":
            clean_dict["proxy"] = _to_serializable_value(val)
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

    opt_proxy = getattr(cast(Any, options), "proxy", None)
    if "proxy" not in clean_dict and opt_proxy:
        clean_dict["proxy"] = _to_serializable_value(opt_proxy)
    opt_proxies = getattr(cast(Any, options), "proxies", None)
    if "proxies" not in clean_dict and opt_proxies:
        clean_dict["proxies"] = _to_serializable_value(opt_proxies)

    return clean_dict


def _diff_serialized_settings(full: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """Keep only keys (and nested provider/downloader/matcher entries) that differ from ``base``."""
    diff: dict[str, Any] = {}
    for k, v in full.items():
        if k in ("user_id", "monitored_link_id"):
            diff[k] = v
            continue
        if k not in base:
            diff[k] = v
            continue
        base_v = base[k]
        if v == base_v:
            continue
        if k == "providers" and isinstance(v, dict) and isinstance(base_v, dict):
            prov_diff: dict[str, Any] = {}
            for prov_name, prov_dict in v.items():
                base_prov = base_v.get(prov_name)
                if isinstance(prov_dict, dict) and isinstance(base_prov, dict):
                    attr_diff = {
                        pk: pv
                        for pk, pv in prov_dict.items()
                        if pk not in base_prov or base_prov[pk] != pv
                    }
                    if attr_diff:
                        prov_diff[prov_name] = attr_diff
                elif prov_dict != base_prov:
                    prov_diff[prov_name] = prov_dict
            if prov_diff:
                diff["providers"] = prov_diff
        elif k in ("downloaders", "matchers") and isinstance(v, dict) and isinstance(base_v, dict):
            entry_diff = {dk: dv for dk, dv in v.items() if dk not in base_v or base_v[dk] != dv}
            if entry_diff:
                diff[k] = entry_diff
        else:
            diff[k] = v
    return diff


def serialize_settings_options(options: Any, base_settings: Settings | None = None) -> dict[str, Any]:
    """Serialize Settings, sub-settings, or dict into a JSON-serializable dictionary.

    When ``base_settings`` is supplied for a ``Settings`` object, only keys differing from
    ``base_settings`` (plus per-task context like ``user_id``/``monitored_link_id``) are emitted,
    avoiding repeating the full static configuration into ``TaskQueue.options``.
    """
    if options is None:
        return {}

    if isinstance(options, dict):
        clean_dict: dict[str, Any] = {}
        for k, v in options.items():
            if k == "archive_user" and v and getattr(v, "is_authenticated", False) and getattr(v, "pk", None):
                clean_dict["user_id"] = v.pk
            elif k == "monitored_link" and v and getattr(v, "pk", None):
                clean_dict["monitored_link_id"] = v.pk
            elif k == "_proxy":
                clean_dict["proxy"] = _to_serializable_value(v)
            else:
                clean_dict[str(k)] = _to_serializable_value(v)
        if "proxy" not in clean_dict and options.get("proxy"):
            clean_dict["proxy"] = _to_serializable_value(options["proxy"])
        if "proxies" not in clean_dict and options.get("proxies"):
            clean_dict["proxies"] = _to_serializable_value(options["proxies"])
        return clean_dict

    if hasattr(options, "__dict__") or hasattr(options, "__slots__"):
        if base_settings is not None and isinstance(options, Settings):
            if options is base_settings:
                minimal: dict[str, Any] = {}
                arch_user = getattr(options, "archive_user", None)
                if arch_user and getattr(arch_user, "is_authenticated", False) and getattr(arch_user, "pk", None):
                    minimal["user_id"] = arch_user.pk
                monitored_link = getattr(options, "monitored_link", None)
                if monitored_link and getattr(monitored_link, "pk", None):
                    minimal["monitored_link_id"] = monitored_link.pk
                return minimal
            full = _serialize_settings_object(options)
            base = _serialize_settings_object(base_settings)
            return _diff_serialized_settings(full, base)

        return _serialize_settings_object(options)

    return {}


def deserialize_settings_options(base_settings: Settings | None, options: dict[str, Any] | None) -> Settings:
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
        setattr(new_settings, "_base_settings", base_settings)
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

    if "monitored_link_id" in options and options["monitored_link_id"] is not None:
        try:
            from viewer.models import MonitoredLink
            ml = MonitoredLink.objects.filter(pk=options["monitored_link_id"]).first()
            if ml:
                new_settings.monitored_link = ml
                if ml.proxy:
                    new_settings.proxy = ml.proxy
                for prov_obj in new_settings.providers.values():
                    prov_obj.monitored_link = ml
                    if ml.proxy and not getattr(prov_obj, "proxy", ""):
                        prov_obj.proxy = ml.proxy
        except Exception:
            pass

    for k, v in options.items():
        if k in ("user_id", "archive_user", "monitored_link_id", "monitored_link") or k in LEGACY_SECTION_MODEL_KEYS:
            continue

        if k in ("proxy", "_proxy") and v:
            new_settings.proxy = str(v)
            for prov_obj in new_settings.providers.values():
                if not getattr(prov_obj, "proxy", ""):
                    prov_obj.proxy = str(v)
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
                        if p_attr == "monitored_link_id" and p_val is not None:
                            try:
                                from viewer.models import MonitoredLink
                                ml = MonitoredLink.objects.filter(pk=p_val).first()
                                if ml:
                                    prov_obj.monitored_link = ml
                            except Exception:
                                pass
                            continue
                        if p_attr == "how_long_after_already_seen" and isinstance(p_val, (int, float)):
                            p_val = timedelta(seconds=p_val)
                        if p_attr in ("proxy", "_proxy") and p_val is not None:
                            prov_obj.proxy = str(p_val)
                            continue
                        if p_attr == "proxies" and isinstance(p_val, dict) and p_val:
                            prov_obj.proxies = dict(p_val)
                            continue
                        if hasattr(prov_obj, p_attr):
                            setattr(prov_obj, p_attr, p_val)
            continue

        if k == "how_long_after_already_seen" and isinstance(v, (int, float)):
            v = timedelta(seconds=v)

        target_obj = getattr(new_settings, k, None)
        if target_obj is not None and isinstance(v, dict):
            if hasattr(target_obj, "update_from_dict") and callable(target_obj.update_from_dict):
                target_obj.update_from_dict(v)
                continue
            if hasattr(target_obj, "__slots__") or hasattr(target_obj, "__dict__"):
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

    if getattr(new_settings, "proxy", ""):
        for prov_obj in new_settings.providers.values():
            if not getattr(prov_obj, "proxy", ""):
                prov_obj.proxy = new_settings.proxy

    return new_settings
