import importlib
import inspect
from operator import itemgetter
from typing import List, Callable, Optional, Tuple, Union, Type
import typing

from core.base.types import OptionalLogger, ProviderSettings
from core.base.utilities import GeneralUtils
from core.base.matchers import Matcher
from core.base.parsers import BaseParser
from core.downloaders.handlers import BaseDownloader

if typing.TYPE_CHECKING:
    from viewer import models
    from core.base import setup


AcceptableModules = Union[Type['BaseParser'], Type['Matcher'], Type['BaseDownloader']]


def _get_provider_submodule_api(module_name: str, submodule_name: str) -> List[AcceptableModules]:
    sub_module = "{}.{}".format(module_name, submodule_name)
    try:
        importlib.import_module(module_name, package='__path__')
    except ImportError:
        return []
    if importlib.util.find_spec(sub_module):
        site = importlib.import_module(sub_module, package=module_name)
        if hasattr(site, 'API'):
            return list(getattr(site, 'API'))

    return []


def _get_provider_submodule_method(module_name: str, submodule_name: str, method_name: str) -> Optional[Callable]:
    sub_module = "{}.{}".format(module_name, submodule_name)
    try:
        importlib.import_module(module_name, package='__path__')
    except ImportError:
        return None
    if importlib.util.find_spec(sub_module):
        site = importlib.import_module(sub_module, package=module_name)
        if hasattr(site, method_name):
            obj = getattr(site, method_name)
            if inspect.isfunction(obj):
                return obj
    return None


# We should only create one ProviderContext over the program lifetime,
# to avoid having to search the file system every time it's created.
# This is why this should be outside Settings
class ProviderContext:

    parsers: List[Type['BaseParser']] = []
    matchers: List[Type['Matcher']] = []
    downloaders: List[Type['BaseDownloader']] = []
    resolvers: List[Tuple[str, Callable[['setup.Settings'], ProviderSettings]]] = []
    settings_parsers: List[Tuple[str, Callable]] = []
    wanted_generators: List[Tuple[str, Callable]] = []

    def register_providers(self, module_name_list: List[str]) -> None:
        for module_name in module_name_list:
            self.register_provider(module_name)

    def register_provider(self, module_name: str) -> None:
        # We just split the module and get their last part
        # Only used by resolver since the others define their provider internally
        provider_name = module_name.split(".")[-1]
        for member in _get_provider_submodule_api(module_name, "parsers"):
            if member not in self.parsers and issubclass(member, BaseParser):
                self.register_parser(member)
        for member in _get_provider_submodule_api(module_name, "matchers"):
            if member not in self.matchers and issubclass(member, Matcher):
                self.register_matcher(member)
        for member in _get_provider_submodule_api(module_name, "downloaders"):
            if member not in self.downloaders and issubclass(member, BaseDownloader):
                self.register_downloader(member)
        resolver = _get_provider_submodule_method(module_name, "utilities", "resolve_url")
        if resolver and resolver not in self.resolvers:
            self.register_resolver(provider_name, resolver)
        settings_parser = _get_provider_submodule_method(module_name, "settings", "parse_config")
        if settings_parser and settings_parser not in self.settings_parsers:
            self.register_settings_parser(provider_name, settings_parser)
        wanted_generator = _get_provider_submodule_method(module_name, "wanted", "wanted_generator")
        if wanted_generator and wanted_generator not in self.wanted_generators:
            self.register_wanted_generator(provider_name, wanted_generator)

    def register_parser(self, obj: Type['BaseParser']) -> None:
        if inspect.isclass(obj):
            if obj.name and not obj.ignore:
                self.parsers.append(obj)

    def register_matcher(self, obj: Type['Matcher']) -> None:
        if inspect.isclass(obj):
            if obj.provider and obj.name:
                self.matchers.append(obj)

    def register_downloader(self, obj: Type['BaseDownloader']) -> None:
        if inspect.isclass(obj):
            if obj.provider and obj.type:
                self.downloaders.append(obj)

    def register_resolver(self, provider_name: str, obj: Callable) -> None:
        self.resolvers.append((provider_name, obj))

    def register_settings_parser(self, provider_name: str, obj: Callable) -> None:
        self.settings_parsers.append((provider_name, obj))

    def register_wanted_generator(self, provider_name: str, obj: Callable) -> None:
        self.wanted_generators.append((provider_name, obj))

    def get_parsers(self, settings: 'setup.Settings', logger: OptionalLogger, filter_name: str = None) -> List['BaseParser']:
        parsers_list = list()
        for parser in self.parsers:
            parser_name = getattr(parser, 'name')
            if filter_name:
                if filter_name in parser_name:
                    parser_instance = parser(settings, logger)
                    if parser_name == 'generic':
                        parsers_list.append(parser_instance)
                    else:
                        parsers_list.insert(0, parser_instance)

            else:
                parser_instance = parser(settings, logger)
                if parser_name == 'generic':
                    parsers_list.append(parser_instance)
                else:
                    parsers_list.insert(0, parser_instance)

        return parsers_list

    def get_parsers_classes(self, filter_name: str=None) -> List[Type['BaseParser']]:
        parsers_list = list()
        for parser in self.parsers:
            parser_name = getattr(parser, 'name')
            if filter_name:
                if filter_name in parser_name:
                    if parser_name == 'generic':
                        parsers_list.append(parser)
                    else:
                        parsers_list.insert(0, parser)
            else:
                if parser_name == 'generic':
                    parsers_list.append(parser)
                else:
                    parsers_list.insert(0, parser)

        return parsers_list

    def get_downloaders(
            self, settings: 'setup.Settings', logger: OptionalLogger,
            general_utils: GeneralUtils, filter_name: str = None,
            force: bool = False) -> List[Tuple['BaseDownloader', int]]:
        downloaders = list()
        for downloader in self.downloaders:
            handler_name = str(downloader)
            if filter_name:
                if filter_name in handler_name:
                    if force:
                        downloader_instance = downloader(settings, logger, general_utils)
                        downloaders.append(
                            (downloader_instance, 1)
                        )
                    else:
                        if handler_name in settings.downloaders and settings.downloaders[handler_name] >= 0:
                            downloader_instance = downloader(settings, logger, general_utils)
                            downloaders.append(
                                (downloader_instance, settings.downloaders[handler_name])
                            )
            else:
                if force:
                    downloader_instance = downloader(settings, logger, general_utils)
                    downloaders.append(
                        (downloader_instance, 1)
                    )
                else:
                    if handler_name in settings.downloaders and settings.downloaders[handler_name] >= 0:
                        downloader_instance = downloader(settings, logger, general_utils)
                        downloaders.append(
                            (downloader_instance, settings.downloaders[handler_name])
                        )

        return sorted(downloaders, key=itemgetter(1))

    def get_downloaders_name_priority(self, settings: 'setup.Settings', filter_name: str=None) -> List[Tuple[str, int]]:
        downloaders = list()
        for downloader in self.downloaders:
            handler_name = str(downloader)
            if filter_name:
                if filter_name in handler_name:
                    if handler_name in settings.downloaders:
                        downloaders.append(
                            (handler_name, settings.downloaders[handler_name])
                        )
                    else:
                        downloaders.append(
                            (handler_name, -1)
                        )
            else:
                if handler_name in settings.downloaders:
                    downloaders.append(
                        (handler_name, settings.downloaders[handler_name])
                    )
                else:
                    downloaders.append(
                        (handler_name, -1)
                    )

        return sorted(downloaders, key=itemgetter(1), reverse=True)

    def get_matchers(self, settings: 'setup.Settings', logger: OptionalLogger=None,
                     filter_name: str=None, force: bool=False,
                     matcher_type: str='') -> List[Tuple['Matcher', int]]:
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
                        matcher_instance = matcher(settings, logger)
                        matchers_list.append((matcher_instance, 1))
                    else:
                        if matcher_name in settings.matchers and settings.matchers[matcher_name] >= 0:
                            matcher_instance = matcher(settings, logger)
                            matchers_list.append((matcher_instance, settings.matchers[matcher_name]))
            else:
                if force:
                    matcher_instance = matcher(settings, logger)
                    matchers_list.append((matcher_instance, 1))
                else:
                    if matcher_name in settings.matchers and settings.matchers[matcher_name] >= 0:
                        matcher_instance = matcher(settings, logger)
                        matchers_list.append((matcher_instance, settings.matchers[matcher_name]))

        return sorted(matchers_list, key=itemgetter(1))

    def get_matchers_name_priority(self, settings: 'setup.Settings',
                                   filter_name: str=None, matcher_type: str='') -> List[Tuple[str, int]]:
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
                        matchers_list.append(
                            (matcher_name, settings.matchers[matcher_name])
                        )
                    else:
                        matchers_list.append(
                            (matcher_name, -1)
                        )
            else:
                if matcher_name in settings.matchers:
                    matchers_list.append(
                        (matcher_name, settings.matchers[matcher_name])
                    )
                else:
                    matchers_list.append(
                        (matcher_name, -1)
                    )

        return sorted(matchers_list, key=itemgetter(1), reverse=True)

    def resolve_all_urls(self, gallery: 'models.Gallery') -> str:
        methods = self.get_resolve_methods(gallery.provider)
        for method in methods:
            return method(gallery)
        return "Can't reconstruct URL"

    def get_resolve_methods(self, filter_name: str = None) -> List[Callable]:
        method_list = list()
        for method_tuple in self.resolvers:
            method_name = method_tuple[0]
            if filter_name:
                if filter_name in method_name:
                    method_list.append(method_tuple[1])
            else:
                method_list.append(method_tuple[1])

        return method_list

    def get_wanted_generators(self, filter_name: str = None) -> List[Callable]:
        method_list = list()
        for method_tuple in self.wanted_generators:
            method_name = method_tuple[0]
            if filter_name:
                if method_name in filter_name:
                    method_list.append(method_tuple[1])
            else:
                method_list.append(method_tuple[1])

        return method_list

    # def __init__(self, settings=None, logger=None):
    #     self.settings = settings
    #     self.logger = logger
