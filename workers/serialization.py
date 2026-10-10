from typing import Any, Optional, cast

from core.base.setup import Settings


def serialize_settings_options(options: Any) -> dict[str, Any]:
    """Serialize Settings or dict into a JSON-serializable dictionary."""
    if options is None:
        return {}
    if isinstance(options, dict):
        # Already a dict, make sure it's JSON serializable
        clean_dict = {}
        for k, v in options.items():
            if isinstance(v, (str, int, float, bool, list, dict, type(None))):
                clean_dict[k] = v
            else:
                clean_dict[k] = str(v)
        return clean_dict

    if isinstance(options, Settings):
        data: dict[str, Any] = {
            "redownload": getattr(options, "redownload", False),
            "archive_reason": getattr(options, "archive_reason", ""),
            "gallery_reason": getattr(options, "gallery_reason", ""),
            "archive_origin": getattr(options, "archive_origin", None),
            "silent_processing": getattr(options, "silent_processing", False),
            "replace_metadata": getattr(options, "replace_metadata", False),
            "keep_dl_type": getattr(options, "keep_dl_type", False),
            "retry_failed": getattr(options, "retry_failed", False),
            "event_action": getattr(options, "event_action", ""),
            "submit_group_uuid": getattr(options, "submit_group_uuid", None),
            "preserve_user_favorites": getattr(options, "preserve_user_favorites", []),
            "preserve_extracted": getattr(options, "preserve_extracted", False),
            "archive_source": getattr(options, "archive_source", ""),
            "internal_matches_for_non_matches": getattr(options, "internal_matches_for_non_matches", False),
        }
        archive_user = getattr(options, "archive_user", None)
        if archive_user and getattr(archive_user, "is_authenticated", False) and getattr(archive_user, "pk", None):
            data["user_id"] = archive_user.pk

        if hasattr(options, "matchers") and isinstance(options.matchers, dict):
            data["matchers"] = dict(options.matchers)

        if hasattr(options, "downloaders") and isinstance(options.downloaders, dict):
            data["downloaders"] = dict(options.downloaders)

        providers_data = {}
        if hasattr(options, "providers") and isinstance(options.providers, dict):
            for prov_name, prov_settings in options.providers.items():
                prov_dict: dict[str, Any] = {}
                if getattr(prov_settings, "proxy", None):
                    prov_dict["proxy"] = prov_settings.proxy
                if getattr(prov_settings, "stop_page_number", None) is not None:
                    prov_dict["stop_page_number"] = prov_settings.stop_page_number
                if prov_dict:
                    providers_data[prov_name] = prov_dict
        if providers_data:
            data["providers"] = providers_data

        return data

    return {}


def deserialize_settings_options(base_settings: Settings, options: Optional[dict[str, Any]]) -> Settings:
    """Create a new Settings instance and apply serialized options."""
    config = getattr(base_settings, "config", None)
    if config:
        new_settings = Settings(load_from_config=config)
    else:
        new_settings = Settings(load_from_disk=False)

    if not new_settings.providers and hasattr(base_settings, "providers"):
        new_settings.providers = cast(Any, dict(base_settings.providers))
    if not new_settings.downloaders and hasattr(base_settings, "downloaders"):
        new_settings.downloaders = dict(base_settings.downloaders)

    if not options or not isinstance(options, dict):
        return new_settings

    if "redownload" in options:
        new_settings.redownload = bool(options["redownload"])
    if "archive_reason" in options:
        new_settings.archive_reason = str(options["archive_reason"])
    if "gallery_reason" in options:
        new_settings.gallery_reason = str(options["gallery_reason"])
    if "archive_origin" in options and options["archive_origin"] is not None:
        new_settings.archive_origin = int(options["archive_origin"])
    if "user_id" in options and options["user_id"] is not None:
        try:
            from django.contrib.auth import get_user_model
            User = get_user_model()
            new_settings.archive_user = User.objects.filter(pk=options["user_id"]).first()
        except Exception:
            pass
    if "silent_processing" in options:
        new_settings.silent_processing = bool(options["silent_processing"])
    if "replace_metadata" in options:
        new_settings.replace_metadata = bool(options["replace_metadata"])
        if new_settings.replace_metadata:
            if hasattr(new_settings, "config") and isinstance(new_settings.config, dict):
                new_settings.config.setdefault("allowed", {})["replace_metadata"] = "yes"
    if "keep_dl_type" in options:
        new_settings.keep_dl_type = bool(options["keep_dl_type"])
    if "retry_failed" in options:
        new_settings.retry_failed = bool(options["retry_failed"])
    if "event_action" in options:
        new_settings.event_action = str(options["event_action"])
    if "submit_group_uuid" in options and options["submit_group_uuid"] is not None:
        new_settings.submit_group_uuid = str(options["submit_group_uuid"])
    if "preserve_user_favorites" in options and isinstance(options["preserve_user_favorites"], list):
        new_settings.preserve_user_favorites = list(options["preserve_user_favorites"])
    if "preserve_extracted" in options:
        new_settings.preserve_extracted = bool(options["preserve_extracted"])
    if "archive_source" in options:
        new_settings.archive_source = str(options["archive_source"])
    if "internal_matches_for_non_matches" in options:
        new_settings.internal_matches_for_non_matches = bool(options["internal_matches_for_non_matches"])
    if "matchers" in options and isinstance(options["matchers"], dict):
        new_settings.matchers = dict(options["matchers"])

    if "downloaders" in options and isinstance(options["downloaders"], dict):
        for dl_name, priority in options["downloaders"].items():
            new_settings.downloaders[dl_name] = priority

    if "providers" in options and isinstance(options["providers"], dict):
        for prov_name, prov_data in options["providers"].items():
            if prov_name in new_settings.providers:
                if "proxy" in prov_data:
                    new_settings.providers[prov_name].proxy = prov_data["proxy"]
                if "stop_page_number" in prov_data:
                    new_settings.providers[prov_name].stop_page_number = prov_data["stop_page_number"]

    return new_settings
