import os
import sys
import pkgutil
import inspect
from typing import Any, Dict

from core.base.types import TorrentClient


def get_torrent_client(torrent_settings: Dict[str, Any]) -> TorrentClient:
    client = None
    torrent_module = None
    for module_name in modules_name:
        if module_name == torrent_settings['client']:
            if module_name not in sys.modules:
                full_package_name = '%s.%s' % ('core.downloaders.torrent', module_name)
                torrent_module = __import__(full_package_name, fromlist=[module_name])
            else:
                torrent_module = module_name
    if not torrent_module:
        return torrent_module
    for _, obj in inspect.getmembers(torrent_module):
        if inspect.isclass(obj) and hasattr(obj, 'type') and 'torrent_handler' in getattr(obj, 'type'):
            client = obj(
                torrent_settings['address'],
                torrent_settings['port'],
                torrent_settings['user'],
                torrent_settings['pass'],
                secure=not torrent_settings['no_certificate_check'],
            )
    return client


modules_name = list()

for importer, package_name, is_pkg in pkgutil.iter_modules([os.path.dirname(__file__)]):
    modules_name.append(package_name)
