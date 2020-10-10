# -*- coding: utf-8 -*-
import configparser
import os
import shutil
import typing
# Reads all providers, to parse each provider specific options into the general settings object.
# Main concern with this first approach is that a provider could read settings from another provider (cookies, etc).
# Another option is to have each provider construct it's own settings object, inheriting from this,
# is that it would need to copy the original settings object each time a provider specific setting is needed.
from typing import Dict, Any, Optional, Tuple, List

from django.db.models.base import ModelBase

from core.base.providers import ProviderContext
from core.base.types import DataDict
from core.workers.holder import WorkerContext

if typing.TYPE_CHECKING:
    from viewer.models import Gallery, Archive, WantedGallery, FoundGallery
    from django.contrib.auth.models import User


class GlobalInfo:

    worker_threads = [
        ('match_unmatched_worker', 'Matches unmatched galleries', 'processor'),
        ('web_search_worker', 'Matches unmatched galleries with web providers', 'processor'),
        ('wanted_local_search_worker', 'Matches unmatched wanted galleries with the internal database', 'processor'),
        ('thumbnails_worker', 'Generates thumbnails for every gallery', 'processor'),
        ('fileinfo_worker', 'Generates file information for every gallery', 'processor'),
        ('web_queue', 'Queue that processes gallery links, one at a time', 'queue'),
        ('webcrawler', 'Processes gallery links, can coexist with web_queue', 'processor'),
        ('foldercrawler', 'Processes galleries on the filesystem', 'processor'),
        ('post_downloader', 'Transfers archives downloaded with other programs (torrent, hath)', 'scheduler'),
        ('auto_updater', 'Auto updates existing gallery metadata after being added, to get new metadata', 'scheduler'),
        ('auto_wanted', 'Parses providers for new galleries to create wanted galleries entries', 'scheduler'),
    ]


class ConfigFileError(Exception):
    pass


# Each sub setting that doesn't need attribute creating is created using __slots__:
class AutoCheckerSettings:
    __slots__ = ['enable', 'startup', 'cycle_timer', 'providers']

    def __init__(self) -> None:
        self.enable: bool = False
        self.startup: bool = False
        self.cycle_timer: float = 0
        self.providers: List[str] = []


class AutoWantedSettings:
    __slots__ = [
        'enable', 'startup', 'cycle_timer', 'providers', 'unwanted_title'
    ]

    def __init__(self) -> None:
        self.enable: bool = False
        self.startup: bool = False
        self.cycle_timer: float = 0
        self.providers: List[str] = []
        self.unwanted_title: str = ''


class AutoUpdaterSettings:
    __slots__ = ['enable', 'startup', 'cycle_timer', 'buffer_delta', 'buffer_back', 'buffer_after', 'providers']

    def __init__(self) -> None:
        self.enable: bool = False
        self.startup: bool = False
        self.cycle_timer: float = 0
        self.buffer_delta: int = 0
        self.buffer_back: int = 0
        self.buffer_after: int = 0
        self.providers: List[str] = []


class PushoverSettings:
    __slots__ = ['enable', 'user_key', 'token', 'device', 'sound']

    def __init__(self) -> None:
        self.enable: bool = False
        self.user_key: str = ''
        self.token: str = ''
        self.device: str = ''
        self.sound: str = ''


class MailSettings:
    __slots__ = ['enable', 'mailhost', 'from_', 'to', 'credentials', 'username', 'password', 'subject']

    def __init__(self) -> None:
        self.enable: bool = False
        self.mailhost: str = ''
        self.from_: str = ''
        self.to: str = ''
        self.credentials: Optional[Tuple[str, str]] = None
        self.username: str = ''
        self.password: str = ''
        self.subject: str = 'CherryPy Error'


class ElasticSearchSettings:
    __slots__ = [
        'enable', 'url', 'max_result_window', 'auto_refresh',
        'index_name', 'auto_refresh_gallery', 'gallery_index_name',
        'only_index_public'
    ]

    def __init__(self) -> None:
        self.enable: bool = False
        self.url: str = 'http://127.0.0.1:9200/'
        self.max_result_window: int = 10000
        self.auto_refresh: bool = False
        self.auto_refresh_gallery: bool = False
        self.index_name: str = 'viewer'
        self.gallery_index_name: str = 'viewer_gallery'
        self.only_index_public: bool = False


class WebServerSettings:
    __slots__ = [
        'bind_address', 'bind_port', 'enable_ssl', 'ssl_certificate',
        'ssl_private_key', 'write_access_log', 'log_to_screen',
        'socket_file'
    ]

    def __init__(self) -> None:
        self.bind_address: str = '127.0.0.1'
        self.bind_port: int = 8090
        self.socket_file: Optional[str] = None
        self.enable_ssl: bool = False
        self.ssl_certificate: str = ''
        self.ssl_private_key: str = ''
        self.write_access_log: bool = False
        self.log_to_screen: bool = True


class UrlSettings:
    __slots__ = [
        'behind_proxy', 'enable_public_submit', 'enable_public_stats',
        'viewer_main_url', 'media_url', 'static_url', 'external_media_server',
        'main_webserver_url'
    ]

    def __init__(self) -> None:
        self.behind_proxy: bool = False
        self.enable_public_submit: bool = False
        self.enable_public_stats: bool = False
        self.viewer_main_url: str = ''
        self.media_url: str = '/media/'
        self.static_url: str = '/static/'
        self.external_media_server = ''
        self.main_webserver_url = ''


class Settings:

    # We are storing the context here, but it should be somewhere else, available globally.
    provider_context = ProviderContext()
    workers = WorkerContext()
    gallery_model: Optional['typing.Type[Gallery]'] = None
    archive_model: Optional['typing.Type[Archive]'] = None
    found_gallery_model: Optional['typing.Type[FoundGallery]'] = None
    wanted_gallery_model: Optional['typing.Type[WantedGallery]'] = None

    def __init__(self, load_from_disk: bool = False,
                 default_dir: str = None,
                 load_from_config: typing.Union[DataDict, configparser.ConfigParser] = None) -> None:
        # INTERNAL USE
        self.gallery_reason: Optional[str] = None
        # Used by autoupdater to not overwrite original value, since it would be replaced by provider_info
        self.keep_dl_type = False
        self.archive_reason = ''
        self.archive_source = ''
        self.archive_details = ''
        self.archive_user: Optional[User] = None
        self.silent_processing = False
        self.update_metadata_mode = False

        # USER SETTINGS
        self.replace_metadata = False
        self.redownload = False

        self.MEDIA_ROOT = ''
        self.django_secret_key = ''
        self.django_debug_mode = False
        self.api_key = ''
        self.download_handler = 'local'
        self.autochecker = AutoCheckerSettings()
        self.auto_wanted = AutoWantedSettings()
        self.autoupdater = AutoUpdaterSettings()

        self.pushover = PushoverSettings()

        self.mail_logging = MailSettings()

        self.elasticsearch = ElasticSearchSettings()

        self.filename_filter = '*.zip'

        self.retry_failed = False
        self.internal_matches_for_non_matches = False
        self.timed_downloader_startup = False
        self.timed_downloader_cycle_timer: float = 5
        self.parallel_post_downloaders = 4
        self.cherrypy_auto_restart = False
        self.add_as_public = False

        self.db_engine = 'sqlite'
        self.database: Dict[str, Any] = {}
        self.torrent: Dict[str, Any] = {}
        self.webserver = WebServerSettings()
        self.urls = UrlSettings()

        self.ftps: Dict[str, Any] = {}

        self.rematch_file = False
        self.rematch_file_list = ['non-match']
        self.rehash_files = False
        self.discard_tags: List[str] = []

        self.matchers: Dict[str, int] = {}
        self.downloaders: Dict[str, int] = {}

        self.copy_match_file = True

        self.archive_dl_folder = ''
        self.torrent_dl_folder = ''
        self.log_location = ''

        self.convert_rar_to_zip = False

        self.requests_headers = {
            'user-agent': 'Mozilla/5.0 (Windows NT 6.3; WOW64; rv:38.0) \
            Gecko/20100101 Firefox/38.0',
        }

        self.providers: Dict[str, Any] = {}

        self.remote_site: Dict[str, Any] = {}

        self.wait_timer = 6
        self.timeout_timer = 25

        self.fatal = 0
        self.default_dir = ''

        if load_from_disk:
            self.load_config_from_file(default_dir=default_dir)
        if load_from_config:
            self.config = configparser.ConfigParser()
            self.config.read_dict(load_from_config)
            self.dict_to_settings(self.config)

    def load_config_from_file(self,
                              default_dir: str = None) -> None:
        if default_dir:
            self.default_dir = default_dir
        else:
            self.default_dir = os.getcwd()

        if not os.path.isfile(os.path.join(self.default_dir, "settings.ini")):
            shutil.copyfile(
                os.path.join(os.path.dirname(__file__), "../../default.ini"),
                os.path.join(self.default_dir, "settings.ini")
            )
            if not os.path.isfile(os.path.join(self.default_dir, "settings.ini")):
                raise ConfigFileError("Config file does not exist.")
        config = configparser.ConfigParser()
        with open(os.path.join(self.default_dir, "settings.ini"), "r", encoding="utf-8") as f:
            first = f.read(1)
            if first != '\ufeff':
                # not a BOM, rewind
                f.seek(0)
            config.read_file(f)
        #    config.read(os.path.join(default_dir, "settings.ini"))
        self.dict_to_settings(config)
        self.config = config

    def write(self) -> None:
        with open(
            os.path.join(self.default_dir, "settings.ini"),
            'w'
        ) as configfile:
            self.config.write(configfile)

    def allow_type_downloaders_only(self, downloader_type: str) -> None:
        for downloader in self.downloaders.keys():
            if downloader.endswith(downloader_type):
                self.downloaders[downloader] = 1
            else:
                self.downloaders[downloader] = -1
        self.replace_metadata = False

    def disable_provider_downloaders(self, provider_name: str) -> None:
        for downloader in self.downloaders.keys():
            downloader_provider = downloader.split("_", maxsplit=1)[0]
            if downloader_provider == provider_name:
                self.downloaders[downloader] = -1

    def enable_downloader_only(self, downloader_name: str) -> None:
        for downloader in self.downloaders.keys():
            if downloader == downloader_name:
                self.downloaders[downloader] = 1
            else:
                self.downloaders[downloader] = -1

    def allow_downloaders_only(self, downloaders: List[str],
                               replace_existing: bool = True, retry_failed: bool = True,
                               redownload: bool = False) -> None:
        for downloader in self.downloaders.keys():
            self.downloaders[downloader] = -1
        for count, downloader in reversed(list(enumerate(downloaders))):
            self.downloaders[downloader] = count + 1
        self.replace_metadata = replace_existing
        self.retry_failed = retry_failed
        self.redownload = redownload

    def set_update_metadata_options(self, providers: typing.Iterable[str] = None) -> None:
        if not providers:
            for downloader in self.downloaders.keys():
                if downloader.endswith("info"):
                    self.downloaders[downloader] = 1
                else:
                    self.downloaders[downloader] = -1
        else:
            for downloader in self.downloaders.keys():
                if downloader.replace("_info", "") in providers:
                    self.downloaders[downloader] = 1
                else:
                    self.downloaders[downloader] = -1
            for provider in providers:
                self.downloaders[provider + "_info"] = 1
        self.keep_dl_type = True
        self.replace_metadata = True
        self.retry_failed = True
        self.redownload = True
        self.update_metadata_mode = True
        self.silent_processing = True

    @classmethod
    def set_models(cls, gallery_model: 'typing.Type[Gallery]',
                   archive_model: 'typing.Type[Archive]', found_gallery_model: 'typing.Type[FoundGallery]',
                   wanted_gallery_model: 'typing.Type[WantedGallery]'):
        cls.gallery_model = gallery_model
        cls.archive_model = archive_model
        cls.found_gallery_model = found_gallery_model
        cls.wanted_gallery_model = wanted_gallery_model

    def dict_to_settings(self, config: configparser.ConfigParser) -> None:

        if 'requests_headers' in config:
            self.requests_headers.update(config['requests_headers'])
        if 'allowed' in config:
            if 'replace_metadata' in config['allowed']:
                self.replace_metadata = config['allowed'].getboolean('replace_metadata')
            if 'redownload' in config['allowed']:
                self.redownload = config['allowed'].getboolean('redownload')
            if 'retry_failed' in config['allowed']:
                self.retry_failed = config['allowed'].getboolean('retry_failed')
            if 'internal_matches_for_non_matches' in config['allowed']:
                self.internal_matches_for_non_matches = config['allowed'].getboolean('internal_matches_for_non_matches')
            if 'convert_rar_to_zip' in config['allowed']:
                self.convert_rar_to_zip = config['allowed'].getboolean('convert_rar_to_zip')
        if 'general' in config:
            if 'filename_filter' in config['general']:
                self.filename_filter = config['general']['filename_filter']
            if 'db_engine' in config['general']:
                self.db_engine = config['general']['db_engine']
            if 'django_secret_key' in config['general']:
                self.django_secret_key = config['general']['django_secret_key']
            if 'django_debug_mode' in config['general']:
                self.django_debug_mode = config['general'].getboolean('django_debug_mode')
            if 'api_key' in config['general']:
                self.api_key = config['general']['api_key']
            if 'download_handler' in config['general']:
                self.download_handler = config['general']['download_handler']
            if 'wait_timer' in config['general']:
                self.wait_timer = int(config['general']['wait_timer'])
            if 'timed_downloader_startup' in config['general']:
                self.timed_downloader_startup = config['general'].getboolean('timed_downloader_startup')
            if 'timed_downloader_cycle_timer' in config['general']:
                self.timed_downloader_cycle_timer = float(config['general']['timed_downloader_cycle_timer'])
            if 'parallel_post_downloaders' in config['general']:
                self.parallel_post_downloaders = int(config['general']['parallel_post_downloaders'])
            if 'cherrypy_auto_restart' in config['general']:
                self.cherrypy_auto_restart = config['general'].getboolean('cherrypy_auto_restart')
            if 'discard_tags' in config['general']:
                self.discard_tags = config['general']['discard_tags'].split(",")
            if 'add_as_public' in config['general']:
                self.add_as_public = config['general'].getboolean('add_as_public')
            if 'timeout_timer' in config['general']:
                self.timeout_timer = int(config['general']['timeout_timer'])
        if 'matchers' in config:
            for matcher in config['matchers']:
                self.matchers[matcher] = int(config['matchers'][matcher])
        if 'downloaders' in config:
            for downloader in config['downloaders']:
                self.downloaders[downloader] = int(config['downloaders'][downloader])
        if 'auto_wanted' in config:
            if 'enable' in config['auto_wanted']:
                self.auto_wanted.enable = config['auto_wanted'].getboolean('enable')
            if 'startup' in config['auto_wanted']:
                self.auto_wanted.startup = config['auto_wanted'].getboolean('startup')
            if 'cycle_timer' in config['auto_wanted']:
                self.auto_wanted.cycle_timer = int(config['auto_wanted']['cycle_timer'])
            if 'providers' in config['auto_wanted']:
                self.auto_wanted.providers = config['auto_wanted']['providers'].split(",")
            if 'unwanted_title' in config['auto_wanted']:
                self.auto_wanted.unwanted_title = config['auto_wanted']['unwanted_title']
        if 'pushover' in config:
            if 'enable' in config['pushover']:
                self.pushover.enable = config['pushover'].getboolean('enable')
            if 'user_key' in config['pushover']:
                self.pushover.user_key = config['pushover']['user_key']
            if 'token' in config['pushover']:
                self.pushover.token = config['pushover']['token']
            if 'device' in config['pushover']:
                self.pushover.device = config['pushover']['device']
            if 'sound' in config['pushover']:
                self.pushover.sound = config['pushover']['sound']
        if 'mail_logging' in config:
            if 'enable' in config['mail_logging']:
                self.mail_logging.enable = config['mail_logging'].getboolean('enable')
            if 'mailhost' in config['mail_logging']:
                self.mail_logging.mailhost = config['mail_logging']['mailhost']
            if 'from' in config['mail_logging']:
                self.mail_logging.from_ = config['mail_logging']['from']
            if 'to' in config['mail_logging']:
                self.mail_logging.to = config['mail_logging']['to']
            if 'subject' in config['mail_logging']:
                self.mail_logging.subject = config['mail_logging']['subject']
            if 'username' in config['mail_logging'] and 'password' in config['mail_logging']:
                self.mail_logging.credentials = (
                    config['mail_logging']['username'],
                    config['mail_logging']['password']
                )
                self.mail_logging.username = config['mail_logging']['username']
                self.mail_logging.password = config['mail_logging']['password']
        if 'elasticsearch' in config:
            if 'enable' in config['elasticsearch']:
                self.elasticsearch.enable = config['elasticsearch'].getboolean('enable')
            if 'url' in config['elasticsearch']:
                self.elasticsearch.url = config['elasticsearch']['url']
            if 'max_result_window' in config['elasticsearch']:
                self.elasticsearch.max_result_window = int(config['elasticsearch']['max_result_window'])
            if 'auto_refresh' in config['elasticsearch']:
                self.elasticsearch.auto_refresh = config['elasticsearch'].getboolean('auto_refresh')
            if 'auto_refresh_gallery' in config['elasticsearch']:
                self.elasticsearch.auto_refresh_gallery = config['elasticsearch'].getboolean('auto_refresh_gallery')
            if 'index_name' in config['elasticsearch']:
                self.elasticsearch.index_name = config['elasticsearch']['index_name']
            if 'gallery_index_name' in config['elasticsearch']:
                self.elasticsearch.gallery_index_name = config['elasticsearch']['gallery_index_name']
            if 'only_index_public' in config['elasticsearch']:
                self.elasticsearch.only_index_public = config['elasticsearch'].getboolean('only_index_public')
        if 'autochecker' in config:
            if 'enable' in config['autochecker']:
                self.autochecker.enable = config['autochecker'].getboolean('enable')
            if 'startup' in config['autochecker']:
                self.autochecker.startup = config['autochecker'].getboolean('startup')
            else:
                self.autochecker.startup = False
            if 'providers' in config['autochecker']:
                self.autochecker.providers = config['autochecker']['providers'].split(",")
            if 'cycle_timer' in config['autochecker']:
                self.autochecker.cycle_timer = float(config['autochecker']['cycle_timer'])
        if 'autoupdater' in config:
            if 'enable' in config['autoupdater']:
                self.autoupdater.enable = config['autoupdater'].getboolean('enable')
            if 'startup' in config['autoupdater']:
                self.autoupdater.startup = config['autoupdater'].getboolean('startup')
            if 'cycle_timer' in config['autoupdater']:
                self.autoupdater.cycle_timer = int(config['autoupdater']['cycle_timer'])
            if 'buffer_delta' in config['autoupdater']:
                self.autoupdater.buffer_delta = int(config['autoupdater']['buffer_delta'])
            if 'buffer_back' in config['autoupdater']:
                self.autoupdater.buffer_back = int(config['autoupdater']['buffer_back'])
            if 'buffer_after' in config['autoupdater']:
                self.autoupdater.buffer_after = int(config['autoupdater']['buffer_after'])
            if 'providers' in config['autoupdater']:
                self.autoupdater.providers = config['autoupdater']['providers'].split(",")
        if 'match_params' in config:
            if 'rematch_file' in config['match_params']:
                self.rematch_file = config['match_params'].getboolean('rematch_file')
            if 'rematch_file_list' in config['match_params']:
                self.rematch_file_list = config['match_params']['rematch_file_list'].split(",")
            if 'rehash_files' in config['match_params']:
                self.rehash_files = config['match_params'].getboolean('rehash_files')
            if 'copy_match_file' in config['match_params']:
                self.copy_match_file = config['match_params'].getboolean('copy_match_file')
        if 'locations' in config:
            if 'media_root' in config['locations']:
                self.MEDIA_ROOT = config['locations']['media_root']
            if 'archive_dl_folder' in config['locations']:
                self.archive_dl_folder = config['locations']['archive_dl_folder']
                if not os.path.exists(os.path.join(self.MEDIA_ROOT, self.archive_dl_folder)):
                    os.makedirs(os.path.join(self.MEDIA_ROOT, self.archive_dl_folder))
            if 'torrent_dl_folder' in config['locations']:
                self.torrent_dl_folder = config['locations']['torrent_dl_folder']
                if not os.path.exists(os.path.join(self.MEDIA_ROOT, self.torrent_dl_folder)):
                    os.makedirs(os.path.join(self.MEDIA_ROOT, self.torrent_dl_folder))
            if 'log_location' in config['locations']:
                self.log_location = config['locations']['log_location']
                if not os.path.exists(os.path.dirname(self.log_location)):
                    os.makedirs(os.path.dirname(self.log_location))
            else:
                self.log_location = os.path.join(self.default_dir, 'viewer.log')
        if 'database' in config:
            self.database = dict(config['database'])
            if(('db_engine' in config['general'])
               and (config['general']['db_engine'] == 'sqlite')) or ('db_engine' not in config['general']):
                if 'sqlite_location' in config['database']:
                    if not os.path.exists(os.path.dirname(self.database['sqlite_location'])):
                        os.makedirs(os.path.dirname(self.database['sqlite_location']))
                else:
                    self.database['sqlite_location'] = os.path.join(self.default_dir, "pgallery.db")
        if 'torrent' in config:
            if 'client' in config['torrent']:
                self.torrent['client'] = config['torrent']['client']
            if 'user' in config['torrent']:
                self.torrent['user'] = config['torrent']['user']
            if 'pass' in config['torrent']:
                self.torrent['pass'] = config['torrent']['pass']
            if 'address' in config['torrent']:
                self.torrent['address'] = config['torrent']['address']
            if 'port' in config['torrent']:
                self.torrent['port'] = int(config['torrent']['port'])
            if 'download_dir' in config['torrent']:
                self.torrent['download_dir'] = config['torrent']['download_dir']
            if 'no_certificate_check' in config['torrent']:
                self.torrent['no_certificate_check'] = config['torrent'].getboolean('no_certificate_check')
            else:
                self.torrent['no_certificate_check'] = False
        if 'ftps' in config:
            if 'user' in config['ftps']:
                self.ftps['user'] = config['ftps']['user']
            if 'passwd' in config['ftps']:
                self.ftps['passwd'] = config['ftps']['passwd']
            if 'address' in config['ftps']:
                self.ftps['address'] = config['ftps']['address']
            if 'remote_torrent_dir' in config['ftps']:
                self.ftps['remote_torrent_dir'] = config['ftps']['remote_torrent_dir']
            if 'bind_address' in config['ftps']:
                self.ftps['source_address'] = (config['ftps']['bind_address'], 0)
            else:
                self.ftps['source_address'] = None
            if 'no_certificate_check' in config['ftps']:
                self.ftps['no_certificate_check'] = config['ftps'].getboolean('no_certificate_check')
            else:
                self.ftps['no_certificate_check'] = False
        if 'webserver' in config:
            if 'bind_address' in config['webserver']:
                self.webserver.bind_address = config['webserver']['bind_address']
            if 'bind_port' in config['webserver']:
                self.webserver.bind_port = int(config['webserver']['bind_port'])
            if 'socket_file' in config['webserver']:
                self.webserver.socket_file = config['webserver']['socket_file']
            if 'enable_ssl' in config['webserver']:
                self.webserver.enable_ssl = config['webserver'].getboolean('enable_ssl')
            else:
                self.webserver.enable_ssl = False
            if 'ssl_certificate' in config['webserver']:
                self.webserver.ssl_certificate = config['webserver']['ssl_certificate']
            if 'ssl_private_key' in config['webserver']:
                self.webserver.ssl_private_key = config['webserver']['ssl_private_key']
            if 'write_access_log' in config['webserver']:
                self.webserver.write_access_log = config['webserver'].getboolean('write_access_log')
            if 'log_to_screen' in config['webserver']:
                self.webserver.log_to_screen = config['webserver'].getboolean('log_to_screen')
        if 'urls' in config:
            if 'media_url' in config['urls']:
                self.urls.media_url = config['urls']['media_url']
            if 'static_url' in config['urls']:
                self.urls.static_url = config['urls']['static_url']
            if 'viewer_main_url' in config['urls']:
                self.urls.viewer_main_url = config['urls']['viewer_main_url']
            if 'behind_proxy' in config['urls']:
                self.urls.behind_proxy = config['urls'].getboolean('behind_proxy')
            if 'enable_public_submit' in config['urls']:
                self.urls.enable_public_submit = config['urls'].getboolean('enable_public_submit')
            if 'enable_public_stats' in config['urls']:
                self.urls.enable_public_stats = config['urls'].getboolean('enable_public_stats')
            if 'external_media_server' in config['urls']:
                self.urls.external_media_server = config['urls']['external_media_server']
            if 'main_webserver_url' in config['urls']:
                self.urls.main_webserver_url = config['urls']['main_webserver_url']
        if 'remote_site' in config:
            if 'api_url' in config['remote_site']:
                self.remote_site['api_url'] = config['remote_site']['api_url']
            if 'api_key' in config['remote_site']:
                self.remote_site['api_key'] = config['remote_site']['api_key']
            if 'remote_folder' in config['remote_site']:
                self.remote_site['remote_folder'] = config['remote_site']['remote_folder']

        for provider_name, parse_config in self.provider_context.settings_parsers:
            provider_sections = [(section.replace(provider_name + "_", ""), config[section]) for section in config.sections() if provider_name in section]
            self.providers[provider_name] = parse_config(self, dict(provider_sections))

        for downloader, priority in self.provider_context.get_downloaders_name_priority(self):
            if downloader not in self.downloaders:
                self.downloaders[downloader] = -1

        for matcher, priority in self.provider_context.get_matchers_name_priority(self):
            if matcher not in self.matchers:
                self.matchers[matcher] = -1
