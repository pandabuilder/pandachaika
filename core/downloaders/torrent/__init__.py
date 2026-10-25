import importlib
import inspect
import os
import pkgutil
from typing import Any

from core.base.types import TorrentClient


def get_torrent_client(torrent_settings: dict[str, Any]) -> TorrentClient | None:
    client = None
    requested_client = str(torrent_settings.get("client", "")).strip().lower()
    if not requested_client:
        return None

    matched_module_name = None
    for module_name in modules_name:
        if module_name.lower() == requested_client:
            matched_module_name = module_name
            break

    if not matched_module_name:
        return None

    full_package_name = f"core.downloaders.torrent.{matched_module_name}"
    try:
        torrent_module = importlib.import_module(full_package_name)
    except ImportError:
        return None

    for _, obj in inspect.getmembers(torrent_module):
        if inspect.isclass(obj) and issubclass(obj, TorrentClient) and obj is not TorrentClient:
            client = obj(
                torrent_settings.get("address", "localhost"),
                torrent_settings.get("port", 8080),
                torrent_settings.get("user", ""),
                torrent_settings.get("pass", ""),
                secure=not torrent_settings.get("no_certificate_check", False),
            )
            break
    return client


modules_name = list()

for importer, package_name, is_pkg in pkgutil.iter_modules([os.path.dirname(__file__)]):
    modules_name.append(package_name)
