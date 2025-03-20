import importlib
import inspect
import logging
from collections.abc import Callable
from operator import itemgetter
from types import ModuleType
from typing import Optional, Union
import typing

from core.base.types import ProviderSettings
from core.base.utilities import GeneralUtils
from core.base.matchers import Matcher
from core.base.parsers import BaseParser
from core.downloaders.handlers import BaseDownloader

if typing.TYPE_CHECKING:
    from viewer import models
    from core.base import setup


AcceptableModules = Union[type["BaseParser"], type["Matcher"], type["BaseDownloader"]]
logger = logging.getLogger(__name__)


def _get_provider_submodule_api(module_name: str, submodule_name: str) -> list[AcceptableModules]:
    sub_module = "{}.{}".format(module_name, submodule_name)
    try:
        importlib.import_module(module_name, package="__path__")
    except ImportError:
        return []
    if importlib.util.find_spec(sub_module):  # type: ignore
        site = importlib.import_module(sub_module, package=module_name)
        if hasattr(site, "API"):
            return list(getattr(site, "API"))

    return []


def _get_provider_submodule_method(module_name: str, submodule_name: str, method_name: str) -> Optional[Callable]:
    sub_module = "{}.{}".format(module_name, submodule_name)
    try:
        importlib.import_module(module_name, package="__path__")
    except ImportError:
        return None
    if importlib.util.find_spec(sub_module):  # type: ignore
        site = importlib.import_module(sub_module, package=module_name)
        if hasattr(site, method_name):
            obj = getattr(site, method_name)
            if inspect.isfunction(obj):
                return obj
    return None


def _get_provider_submodule(module_name: str, submodule_name: str) -> Optional[ModuleType]:
    sub_module = "{}.{}".format(module_name, submodule_name)
    try:
        importlib.import_module(module_name, package="__path__")
    except ImportError:
        return None
    if importlib.util.find_spec(sub_module):  # type: ignore
        site = importlib.import_module(sub_module, package=module_name)
        return site
    return None


# We should only create one ProviderContext over the program lifetime,
# to avoid having to search the file system every time it's created.
# This is why this should be outside Settings
class ProviderContext:

    parsers: list[type["BaseParser"]] = []
    matchers: list[type["Matcher"]] = []
    downloaders: list[type["BaseDownloader"]] = []
    resolvers: list[tuple[str, Callable[["setup.Settings"], ProviderSettings]]] = []
    settings_parsers: list[tuple[str, Callable]] = []
    wanted_generators: list[tuple[str, Callable]] = []
    constants: list[tuple[str, ModuleType]] = []

    def register_providers(self, module_name_list: list[str]) -> None:
        for module_name in module_name_list:
            self.register_provider(module_name)

    def register_provider(self, module_name: str) -> None:
        # We just split the module and get their last part
        # Only used by resolver since the others define their provider internally
        provider_name = module_name.split(".")[-1]
        for member in _get_provider_submodule_api(module_name, "parsers"):
            if member not in self.parsers and issubclass(member, BaseParser):
                # logger.debug("For provider: {}, registering parser: {}".format(provider_name, member))
                self.register_parser(member)
        for member in _get_provider_submodule_api(module_name, "matchers"):
            if member not in self.matchers and issubclass(member, Matcher):
                # logger.debug("For provider: {}, registering matcher: {}".format(provider_name, member))
                self.register_matcher(member)
        for member in _get_provider_submodule_api(module_name, "downloaders"):
            if member not in self.downloaders and issubclass(member, BaseDownloader):
                # logger.debug("For provider: {}, registering downloader: {}".format(provider_name, member))
                self.register_downloader(member)
        resolver = _get_provider_submodule_method(module_name, "utilities", "resolve_url")
        if resolver and (provider_name, resolver) not in self.resolvers:
            # logger.debug("For provider: {}, registering resolver".format(provider_name))
            self.register_resolver(provider_name, resolver)
        settings_parser = _get_provider_submodule_method(module_name, "settings", "parse_config")
        if settings_parser and (provider_name, settings_parser) not in self.settings_parsers:
            # logger.debug("For provider: {}, registering settings parser".format(provider_name))
            self.register_settings_parser(provider_name, settings_parser)
        wanted_generator = _get_provider_submodule_method(module_name, "wanted", "wanted_generator")
        if wanted_generator and (provider_name, wanted_generator) not in self.wanted_generators:
            # logger.debug("For provider: {}, registering wanted generator".format(provider_name))
            self.register_wanted_generator(provider_name, wanted_generator)
        constants_module = _get_provider_submodule(module_name, "constants")
        if constants_module and (provider_name, constants_module) not in self.constants:
            # logger.debug("For provider: {}, registering constants".format(provider_name))
            self.register_constants(provider_name, constants_module)

    def register_parser(self, obj: type["BaseParser"]) -> None:
        if inspect.isclass(obj):
            if obj.name and not obj.ignore:
                self.parsers.append(obj)

    def register_matcher(self, obj: type["Matcher"]) -> None:
        if inspect.isclass(obj):
            if obj.provider and obj.name:
                self.matchers.append(obj)

    def register_downloader(self, obj: type["BaseDownloader"]) -> None:
        if inspect.isclass(obj):
            if obj.provider and obj.type:
                self.downloaders.append(obj)

    def register_resolver(self, provider_name: str, obj: Callable) -> None:
        self.resolvers.append((provider_name, obj))

    def register_settings_parser(self, provider_name: str, obj: Callable) -> None:
        self.settings_parsers.append((provider_name, obj))

    def register_wanted_generator(self, provider_name: str, obj: Callable) -> None:
        self.wanted_generators.append((provider_name, obj))

    def register_constants(self, provider_name: str, obj: ModuleType) -> None:
        self.constants.append((provider_name, obj))

    def get_parsers(
        self, settings: "setup.Settings", filter_name: Optional[str] = None, filter_names: Optional[list[str]] = None
    ) -> list["BaseParser"]:
        parsers_list = list()
        for parser in self.parsers:
            parser_name = getattr(parser, "name")
            if filter_name:
                if filter_name in parser_name:
                    parser_instance = parser(settings)
                    if parser_name == "generic":
                        parsers_list.append(parser_instance)
                    else:
                        parsers_list.insert(0, parser_instance)
            elif filter_names:
                for current_filter_name in filter_names:
                    if current_filter_name in parser_name:
                        parser_instance = parser(settings)
                        if parser_name == "generic":
                            parsers_list.append(parser_instance)
                        else:
                            parsers_list.insert(0, parser_instance)
            else:
                parser_instance = parser(settings)
                if parser_name == "generic":
                    parsers_list.append(parser_instance)
                else:
                    parsers_list.insert(0, parser_instance)

        return parsers_list

    def get_parsers_classes(self, filter_name: Optional[str] = None) -> list[type["BaseParser"]]:
        parsers_list = list()
        for parser in self.parsers:
            parser_name = getattr(parser, "name")
            if filter_name:
                if filter_name in parser_name:
                    if parser_name == "generic":
                        parsers_list.append(parser)
                    else:
                        parsers_list.insert(0, parser)
            else:
                if parser_name == "generic":
                    parsers_list.append(parser)
                else:
                    parsers_list.insert(0, parser)

        return parsers_list

    def get_downloaders(
        self,
        settings: "setup.Settings",
        general_utils: GeneralUtils,
        filter_name: Optional[str] = None,
        force: bool = False,
        priorities: Optional[dict[str, int]] = None,
    ) -> list[tuple["BaseDownloader", int]]:

        if priorities:
            priorities_to_use = priorities
        else:
            priorities_to_use = settings.downloaders

        downloaders = list()
        for downloader in self.downloaders:
            handler_name = str(downloader)
            if filter_name:
                if filter_name in handler_name:
                    if force:
                        downloader_instance = downloader(settings, general_utils)
                        downloaders.append((downloader_instance, 1))
                    else:
                        if handler_name in priorities_to_use and priorities_to_use[handler_name] >= 0:
                            downloader_instance = downloader(settings, general_utils)
                            downloaders.append((downloader_instance, priorities_to_use[handler_name]))
            else:
                if force:
                    downloader_instance = downloader(settings, general_utils)
                    downloaders.append((downloader_instance, 1))
                else:
                    if handler_name in priorities_to_use and priorities_to_use[handler_name] >= 0:
                        downloader_instance = downloader(settings, general_utils)
                        downloaders.append((downloader_instance, priorities_to_use[handler_name]))

        return sorted(downloaders, key=itemgetter(1))

    def get_downloaders_name_priority(
        self, settings: "setup.Settings", filter_name: Optional[str] = None
    ) -> list[tuple[str, int]]:
        downloaders = list()
        for downloader in self.downloaders:
            handler_name = str(downloader)
            if filter_name:
                if filter_name in handler_name:
                    if handler_name in settings.downloaders:
                        downloaders.append((handler_name, settings.downloaders[handler_name]))
                    else:
                        downloaders.append((handler_name, -1))
            else:
                if handler_name in settings.downloaders:
                    downloaders.append((handler_name, settings.downloaders[handler_name]))
                else:
                    downloaders.append((handler_name, -1))

        return sorted(downloaders, key=itemgetter(1), reverse=True)

    def get_matchers(
        self, settings: "setup.Settings", filter_name: Optional[str] = None, force: bool = False, matcher_type: str = ""
    ) -> list[tuple["Matcher", int]]:
        matchers_list = list()
        if matcher_type:
            packages_filtered = [x for x in self.matchers if x.type == matcher_type]
        else:
            packages_filtered = self.matchers
        for matcher in packages_filtered:
            matcher_name = str(matcher)
            if filter_name:
                if filter_name in matcher_name:
                    if force:
                        matcher_instance = matcher(settings)
                        matchers_list.append((matcher_instance, 1))
                    else:
                        if matcher_name in settings.matchers and settings.matchers[matcher_name] >= 0:
                            matcher_instance = matcher(settings)
                            matchers_list.append((matcher_instance, settings.matchers[matcher_name]))
            else:
                if force:
                    matcher_instance = matcher(settings)
                    matchers_list.append((matcher_instance, 1))
                else:
                    if matcher_name in settings.matchers and settings.matchers[matcher_name] >= 0:
                        matcher_instance = matcher(settings)
                        matchers_list.append((matcher_instance, settings.matchers[matcher_name]))

        return sorted(matchers_list, key=itemgetter(1))

    def get_matchers_name_priority(
        self, settings: "setup.Settings", filter_name: Optional[str] = None, matcher_type: str = ""
    ) -> list[tuple[str, int]]:
        matchers_list = list()
        if matcher_type:
            packages_filtered = [x for x in self.matchers if x.type == matcher_type]
        else:
            packages_filtered = self.matchers
        for matcher in packages_filtered:
            matcher_name = str(matcher)
            if filter_name:
                if filter_name in matcher_name:
                    if matcher_name in settings.matchers:
                        matchers_list.append((matcher_name, settings.matchers[matcher_name]))
                    else:
                        matchers_list.append((matcher_name, -1))
            else:
                if matcher_name in settings.matchers:
                    matchers_list.append((matcher_name, settings.matchers[matcher_name]))
                else:
                    matchers_list.append((matcher_name, -1))

        return sorted(matchers_list, key=itemgetter(1), reverse=True)

    def resolve_all_urls(self, gallery: "models.Gallery") -> str:
        methods = self.get_resolve_methods(gallery.provider)
        for method in methods:
            return method(gallery)
        return "Can't reconstruct URL"

    def get_resolve_methods(self, filter_name: Optional[str] = None) -> list[Callable]:
        method_list = list()
        for method_tuple in self.resolvers:
            method_name = method_tuple[0]
            if filter_name:
                if filter_name in method_name:
                    method_list.append(method_tuple[1])
            else:
                method_list.append(method_tuple[1])

        return method_list

    def get_wanted_generators(self, filter_name: Optional[str] = None) -> list[Callable]:
        method_list = list()
        for method_tuple in self.wanted_generators:
            method_name = method_tuple[0]
            if filter_name:
                if method_name in filter_name:
                    method_list.append(method_tuple[1])
            else:
                method_list.append(method_tuple[1])

        return method_list

    def get_constants(self, filter_name: Optional[str] = None) -> list[ModuleType]:
        constants_list = list()
        for module_tuple in self.constants:
            module_name = module_tuple[0]
            if filter_name:
                if module_name == filter_name:
                    constants_list.append(module_tuple[1])
            else:
                constants_list.append(module_tuple[1])

        return constants_list
