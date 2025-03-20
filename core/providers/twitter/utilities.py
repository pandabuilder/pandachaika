import pkgutil
from importlib import import_module

from . import handles


def get_all_handle_modules():
    handle_modules = {}
    found_modules = pkgutil.iter_modules(handles.__path__, handles.__name__ + ".")
    for module in found_modules:
        imported = import_module(module.name)
        if hasattr(imported, "match_tweet_with_wanted_galleries") and hasattr(imported, "HANDLE"):
            handle_modules[imported.HANDLE] = imported
    return handle_modules


HANDLES_MODULES = get_all_handle_modules()
