# -*- coding: utf-8 -*-
from copy import deepcopy

import yaml
import os
import shutil
import typing

# Reads all providers, to parse each provider specific options into the general settings object.
# Main concern with this first approach is that a provider could read settings from another provider (cookies, etc).
# Another option is to have each provider construct it's own settings object, inheriting from this,
# is that it would need to copy the original settings object each time a provider specific setting is needed.
from typing import Any, Optional

from core.base.providers import ProviderContext
from core.base.types import DataDict
from core.workers.holder import WorkerContext

if typing.TYPE_CHECKING:
    from viewer.models import Gallery, Archive, WantedGallery, FoundGallery, ArchiveManageEntry, DownloadEvent
    from django.contrib.auth.models import User


class GlobalInfo:

    worker_threads = [
        ("match_unmatched_worker", "Matches unmatched galleries", "processor"),
        ("web_search_worker", "Matches unmatched galleries with web providers", "processor"),
        ("wanted_local_search_worker", "Matches unmatched wanted galleries with the internal database", "processor"),
        ("thumbnails_worker", "Generates thumbnails for every gallery", "processor"),
        ("fileinfo_worker", "Generates file information for every gallery", "processor"),
        ("web_queue", "Queue that processes gallery links, one at a time", "queue"),
        ("webcrawler", "Processes gallery links, can coexist with web_queue", "processor"),
        ("foldercrawler", "Processes galleries on the filesystem", "processor"),
        ("download_progress_checker", "Checks for progress on downloads", "processor"),
        ("post_downloader", "Transfers archives downloaded with other programs (torrent, hath)", "scheduler"),
        ("auto_wanted", "Parses providers for new galleries to create wanted galleries entries", "scheduler"),
    ]


class ConfigFileError(Exception):
    pass


# Each sub setting that doesn't need attribute creating is created using __slots__:
class MonitoredLinksSettings:
    __slots__ = ["enable"]

    def __init__(self) -> None:
        self.enable: bool = False


class AutoWantedSettings:
    __slots__ = ["enable", "startup", "cycle_timer", "providers", "unwanted_title"]

    def __init__(self) -> None:
        self.enable: bool = False
        self.startup: bool = False
        self.cycle_timer: float = 0
        self.providers: list[str] = []
        self.unwanted_title: str = ""


class AutoUpdaterSettings:
    __slots__ = ["enable", "startup", "cycle_timer", "buffer_back", "buffer_after", "providers"]

    def __init__(self) -> None:
        self.enable: bool = False
        self.startup: bool = False
        self.cycle_timer: float = 0
        self.buffer_back: int = 0
        self.buffer_after: int = 0
        self.providers: list[str] = []


class PushoverSettings:
    __slots__ = ["enable", "user_key", "token", "device", "sound"]

    def __init__(self) -> None:
        self.enable: bool = False
        self.user_key: str = ""
        self.token: str = ""
        self.device: str = ""
        self.sound: str = ""


class GalleryDLSettings:
    __slots__ = ["executable_name", "executable_path", "config_file", "extra_arguments"]

    def __init__(self) -> None:
        self.executable_name: str = "gallery-dl"
        self.executable_path: str = ""
        self.config_file: str = ""
        self.extra_arguments: str = ""


class CloningImageToolSettings:
    __slots__ = ["enable", "name", "executable_path", "description", "file_filters", "extra_arguments"]

    def __init__(self) -> None:
        self.enable: bool = False
        self.name: str = ""
        self.executable_path: str = ""
        self.description: str = ""
        self.file_filters: list[str] = []
        self.extra_arguments: list[str] = []


class MailSettings:
    __slots__ = ["enable", "mailhost", "from_", "to", "credentials", "username", "password", "subject"]

    def __init__(self) -> None:
        self.enable: bool = False
        self.mailhost: str = ""
        self.from_: str = ""
        self.to: str = ""
        self.credentials: Optional[tuple[str, str]] = None
        self.username: str = ""
        self.password: str = ""
        self.subject: str = "CherryPy Error"


class ElasticSearchSettings:
    __slots__ = [
        "enable",
        "url",
        "max_result_window",
        "auto_refresh",
        "index_name",
        "auto_refresh_gallery",
        "gallery_index_name",
        "only_index_public",
        "timeout",
    ]

    def __init__(self) -> None:
        self.enable: bool = False
        self.url: str = "http://127.0.0.1:9200/"
        self.max_result_window: int = 10000
        self.auto_refresh: bool = False
        self.auto_refresh_gallery: bool = False
        self.index_name: str = "viewer"
        self.gallery_index_name: str = "viewer_gallery"
        self.only_index_public: bool = False
        self.timeout: int = 20


class WebServerSettings:
    __slots__ = [
        "bind_address",
        "bind_port",
        "enable_ssl",
        "ssl_certificate",
        "ssl_private_key",
        "write_access_log",
        "log_to_screen",
        "socket_file",
    ]

    def __init__(self) -> None:
        self.bind_address: str = "127.0.0.1"
        self.bind_port: int = 8090
        self.socket_file: Optional[str] = None
        self.enable_ssl: bool = False
        self.ssl_certificate: str = ""
        self.ssl_private_key: str = ""
        self.write_access_log: bool = False
        self.log_to_screen: bool = True


class UrlSettings:
    __slots__ = [
        "behind_proxy",
        "enable_public_submit",
        "enable_public_stats",
        "enable_public_marks",
        "public_mark_reasons",
        "viewer_main_url",
        "media_url",
        "static_url",
        "external_media_server",
        "main_webserver_url",
        "external_as_main_download",
        "elasticsearch_as_main_urls",
        "cors_allowed_origins",
        "cors_allow_all_origins",
    ]

    def __init__(self) -> None:
        self.behind_proxy: bool = False
        self.enable_public_submit: bool = False
        self.enable_public_stats: bool = False
        self.enable_public_marks: bool = False
        self.public_mark_reasons: list[str] = []
        self.elasticsearch_as_main_urls: bool = False
        self.viewer_main_url: str = ""
        self.media_url: str = "/media/"
        self.static_url: str = "/static/"
        self.external_media_server = ""
        self.external_as_main_download = False
        self.main_webserver_url = ""
        self.cors_allowed_origins: list[str] = []
        self.cors_allow_all_origins: bool = False


class Settings:

    # We are storing the context here, but it should be somewhere else, available globally.
    provider_context = ProviderContext()
    workers = WorkerContext()
    gallery_model: Optional["typing.Type[Gallery]"] = None
    archive_model: Optional["typing.Type[Archive]"] = None
    found_gallery_model: Optional["typing.Type[FoundGallery]"] = None
    wanted_gallery_model: Optional["typing.Type[WantedGallery]"] = None
    archive_manage_entry_model: Optional["typing.Type[ArchiveManageEntry]"] = None
    download_event_model: Optional["typing.Type[DownloadEvent]"] = None

    def __init__(
        self,
        load_from_disk: bool = False,
        default_dir: Optional[str] = None,
        load_from_config: Optional[DataDict] = None,
    ) -> None:
        # INTERNAL USE
        self.gallery_reason: Optional[str] = None
        self.stop_nested = False
        self.link_child: Optional[str] = None
        self.link_newer: Optional[str] = None
        self.stop_nested = False
        # Used by autoupdater to not overwrite original value, since it would be replaced by provider_info
        self.keep_dl_type = False
        self.archive_reason = ""
        self.archive_source = ""
        self.archive_details = ""
        self.archive_origin: Optional[int] = None
        self.archive_user: Optional[User] = None
        self.silent_processing = False
        self.update_metadata_mode = False

        # USER SETTINGS
        self.replace_metadata = False
        self.redownload = False
        self.auto_download_nested = False
        self.recheck_wanted_on_update = False
        self.vertical_image_max_width = 900
        self.horizontal_image_max_width = 1500
        # Option to add metadata from a non-current link, but no archive download.
        # The logic for a current link is provider-specific
        self.non_current_links_as_deleted = False

        self.MEDIA_ROOT = ""
        self.STATIC_ROOT = ""
        self.django_secret_key = ""
        self.django_debug_mode = False
        self.download_handler = "local"
        self.temp_directory_path: Optional[str] = None
        # More specific, if not set, will use 'download_handler'
        self.download_handler_torrent = ""
        self.download_handler_hath = ""
        self.download_ftp_torrent = "default"
        self.download_ftp_hath = "default"
        self.auto_wanted = AutoWantedSettings()
        self.autoupdater = AutoUpdaterSettings()

        self.pushover = PushoverSettings()

        self.mail_logging = MailSettings()

        self.elasticsearch = ElasticSearchSettings()

        self.gallery_dl = GalleryDLSettings()

        self.monitored_links = MonitoredLinksSettings()

        self.filename_filter = ["*.zip"]

        self.retry_failed = False
        self.internal_matches_for_non_matches = False
        self.timed_downloader_startup = False
        self.timed_downloader_cycle_timer: float = 5
        self.parallel_post_downloaders = 4
        self.cherrypy_auto_restart = False
        self.add_as_public = False

        self.download_progress_checker_startup = False
        self.download_progress_checker_cycle_timer: float = 10

        self.db_engine = "postgresql"
        self.database: dict[str, Any] = {}
        self.torrent: dict[str, Any] = {}
        self.webserver = WebServerSettings()
        self.urls = UrlSettings()

        self.ftps: dict[str, Any] = {}
        self.ftp_configs: dict[str, dict[str, Any]] = {}

        self.rematch_file = False
        self.rematch_file_list = ["non-match"]
        self.rehash_files = False
        self.banned_tags: list[str] = []
        self.banned_uploaders: list[str] = []

        self.matchers: dict[str, int] = {}
        self.downloaders: dict[str, int] = {}
        self.back_up_downloaders: dict[str, int] = {}

        self.copy_match_file = True

        self.archive_dl_folder = ""
        self.torrent_dl_folder: str = ""
        self.log_location = ""
        self.log_level: str = "INFO"
        self.disable_sql_log: bool = False

        self.convert_others_to_zip = False
        self.mark_similar_new_archives = False
        self.auto_hash_images = False
        self.auto_phash_images = False
        self.auto_match_wanted_images = False
        self.default_wanted_publisher = ''
        self.default_wanted_categories = ["Doujinshi", "Artist CG"]
        self.default_wanted_providers = ["panda"]
        self.cloning_image_tool = CloningImageToolSettings()

        self.requests_headers: dict[str, Any] = {}

        self.experimental: dict[str, Any] = {}

        self.providers: dict[str, Any] = {}

        self.remote_site: dict[str, Any] = {}

        self.wait_timer = 6
        self.timeout_timer = 25

        self.fatal = 0
        self.default_dir = ""

        if load_from_disk:
            self.load_config_from_file(default_dir=default_dir)
        if load_from_config:
            self.config = deepcopy(load_from_config)
            self.dict_to_settings(self.config)

        self.load_from_environment()

    def load_from_environment(self):

        if 'DATABASE_ENGINE' in os.environ:
            DATABASE_ENGINE = os.environ['DATABASE_ENGINE']
            self.db_engine = DATABASE_ENGINE
        else:
            DATABASE_ENGINE = self.db_engine

        if 'DATABASE_NAME' in os.environ:
            if DATABASE_ENGINE == 'postgresql':
                self.database['postgresql_name'] = os.environ['DATABASE_NAME']
            else:
                self.database['mysql_name'] = os.environ['DATABASE_NAME']

        if 'DATABASE_USERNAME' in os.environ:
            if DATABASE_ENGINE == 'postgresql':
                self.database['postgresql_user'] = os.environ['DATABASE_USERNAME']
            else:
                self.database['mysql_user'] = os.environ['DATABASE_USERNAME']

        if 'DATABASE_PASSWORD' in os.environ:
            if DATABASE_ENGINE == 'postgresql':
                self.database['postgresql_password'] = os.environ['DATABASE_PASSWORD']
            else:
                self.database['mysql_password'] = os.environ['DATABASE_PASSWORD']

        if 'DATABASE_HOST' in os.environ:
            if DATABASE_ENGINE == 'postgresql':
                self.database['postgresql_host'] = os.environ['DATABASE_HOST']
            else:
                self.database['mysql_host'] = os.environ['DATABASE_HOST']

        if 'DATABASE_PORT' in os.environ:
            if DATABASE_ENGINE == 'postgresql':
                self.database['postgresql_port'] = os.environ['DATABASE_PORT']
            else:
                self.database['mysql_port'] = os.environ['DATABASE_PORT']

        if 'DEBUG' in os.environ:
            self.django_debug_mode = os.getenv("DEBUG", "False").lower() in ('true', '1', 't')

        if 'DJANGO_SECRET_KEY' in os.environ:
            self.django_secret_key = os.environ.get("DJANGO_SECRET_KEY")

        if 'MEDIA_ROOT' in os.environ:
            self.MEDIA_ROOT = os.environ.get("MEDIA_ROOT")

        if 'STATIC_ROOT' in os.environ:
            self.STATIC_ROOT = os.environ.get("STATIC_ROOT")

        if 'USING_REVERSE_PROXY' in os.environ:
            self.urls.behind_proxy = os.getenv("USING_REVERSE_PROXY", "False").lower() in ('true', '1', 't')

        if 'CORS_ALLOWED_ORIGINS' in os.environ:
            cors_allowed_origins = os.getenv("CORS_ALLOWED_ORIGINS", "")
            if cors_allowed_origins:
                self.urls.cors_allowed_origins = cors_allowed_origins.split(",")

        if 'CORS_ALLOW_ALL_ORIGINS' in os.environ:
            self.urls.cors_allow_all_origins = os.getenv("CORS_ALLOW_ALL_ORIGINS", "False").lower() in ('true', '1', 't')

    def load_config_from_file(self, default_dir: Optional[str] = None) -> None:
        if default_dir:
            self.default_dir = default_dir
        else:
            self.default_dir = os.getcwd()

        yaml_config = os.path.join(self.default_dir, "settings.yaml")
        if not os.path.isfile(yaml_config):
            shutil.copyfile(os.path.join(os.path.dirname(__file__), "../../default.yaml"), yaml_config)
            if not os.path.isfile(yaml_config):
                raise ConfigFileError("Config file {} does not exist.".format(yaml_config))

        with open(yaml_config, "r", encoding="utf-8") as f:
            first = f.read(1)
            if first != "\ufeff":
                # not a BOM, rewind
                f.seek(0)
            yaml_settings = yaml.safe_load(f)
        self.dict_to_settings(yaml_settings)
        self.config = yaml_settings

    def write(self) -> None:
        with open(os.path.join(self.default_dir, "settings.yaml"), "w") as yaml_file:
            yaml.dump(self.config, yaml_file, default_flow_style=False, sort_keys=False)

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

    def allow_downloaders_only(
        self, downloaders: list[str], replace_existing: bool = True, retry_failed: bool = True, redownload: bool = False
    ) -> None:
        for downloader in self.downloaders.keys():
            self.downloaders[downloader] = -1
        for count, downloader in reversed(list(enumerate(downloaders))):
            self.downloaders[downloader] = count + 1
        self.replace_metadata = replace_existing
        self.retry_failed = retry_failed
        self.redownload = redownload

    def set_update_metadata_options(self, providers: Optional[typing.Iterable[str]] = None) -> None:
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

    def set_enable_download(self) -> None:
        self.keep_dl_type = True
        self.replace_metadata = True
        self.retry_failed = True
        self.silent_processing = True

    @classmethod
    def set_models(
        cls,
        gallery_model: "typing.Type[Gallery]",
        archive_model: "typing.Type[Archive]",
        found_gallery_model: "typing.Type[FoundGallery]",
        wanted_gallery_model: "typing.Type[WantedGallery]",
        archive_manage_entry_model: "typing.Type[ArchiveManageEntry]",
        download_event_model: "typing.Type[DownloadEvent]",
    ):
        cls.gallery_model = gallery_model
        cls.archive_model = archive_model
        cls.found_gallery_model = found_gallery_model
        cls.wanted_gallery_model = wanted_gallery_model
        cls.archive_manage_entry_model = archive_manage_entry_model
        cls.download_event_model = download_event_model

    def dict_to_settings(self, config: DataDict) -> None:
        if "requests_headers" in config and config["requests_headers"] is not None:
            self.requests_headers.update(config["requests_headers"])
        if "allowed" in config:
            if "replace_metadata" in config["allowed"]:
                self.replace_metadata = config["allowed"]["replace_metadata"]
            if "redownload" in config["allowed"]:
                self.redownload = config["allowed"]["redownload"]
            if "auto_download_nested" in config["allowed"]:
                self.auto_download_nested = config["allowed"]["auto_download_nested"]
            if "retry_failed" in config["allowed"]:
                self.retry_failed = config["allowed"]["retry_failed"]
            if "internal_matches_for_non_matches" in config["allowed"]:
                self.internal_matches_for_non_matches = config["allowed"]["internal_matches_for_non_matches"]
            if "convert_others_to_zip" in config["allowed"]:
                self.convert_others_to_zip = config["allowed"]["convert_others_to_zip"]
            if "non_current_links_as_deleted" in config["general"]:
                self.non_current_links_as_deleted = config["general"]["non_current_links_as_deleted"]
        if "general" in config:
            if "filename_filter" in config["general"]:
                self.filename_filter = config["general"]["filename_filter"]
            if "db_engine" in config["general"]:
                self.db_engine = config["general"]["db_engine"]
            if "django_secret_key" in config["general"]:
                self.django_secret_key = config["general"]["django_secret_key"]
            if "django_debug_mode" in config["general"]:
                self.django_debug_mode = config["general"]["django_debug_mode"]
            if "download_handler" in config["general"]:
                self.download_handler = config["general"]["download_handler"]
            if "download_handler_torrent" in config["general"]:
                self.download_handler_torrent = config["general"]["download_handler_torrent"]
            if "download_handler_hath" in config["general"]:
                self.download_handler_hath = config["general"]["download_handler_hath"]
            if "download_ftp_torrent" in config["general"]:
                self.download_ftp_torrent = config["general"]["download_ftp_torrent"]
            if "download_ftp_hath" in config["general"]:
                self.download_ftp_hath = config["general"]["download_ftp_hath"]
            if "temp_directory_path" in config["general"]:
                self.temp_directory_path = config["general"]["temp_directory_path"]
            if "wait_timer" in config["general"]:
                self.wait_timer = config["general"]["wait_timer"]
            if "timed_downloader_startup" in config["general"]:
                self.timed_downloader_startup = config["general"]["timed_downloader_startup"]
            if "download_progress_checker_startup" in config["general"]:
                self.download_progress_checker_startup = config["general"]["download_progress_checker_startup"]
            if "download_progress_checker_cycle_timer" in config["general"]:
                self.download_progress_checker_cycle_timer = config["general"]["download_progress_checker_cycle_timer"]
            if "timed_downloader_cycle_timer" in config["general"]:
                self.timed_downloader_cycle_timer = config["general"]["timed_downloader_cycle_timer"]
            if "parallel_post_downloaders" in config["general"]:
                self.parallel_post_downloaders = config["general"]["parallel_post_downloaders"]
            if "cherrypy_auto_restart" in config["general"]:
                self.cherrypy_auto_restart = config["general"]["cherrypy_auto_restart"]
            if "discard_tags" in config["general"]:
                self.banned_tags = config["general"]["discard_tags"]
            if "banned_tags" in config["general"]:
                self.banned_tags = config["general"]["banned_tags"]
            if "banned_uploaders" in config["general"]:
                self.banned_uploaders = config["general"]["banned_uploaders"]
            if "add_as_public" in config["general"]:
                self.add_as_public = config["general"]["add_as_public"]
            if "timeout_timer" in config["general"]:
                self.timeout_timer = config["general"]["timeout_timer"]
            if "mark_similar_new_archives" in config["general"]:
                self.mark_similar_new_archives = config["general"]["mark_similar_new_archives"]
            if "auto_hash_images" in config["general"]:
                self.auto_hash_images = config["general"]["auto_hash_images"]
            if "auto_phash_images" in config["general"]:
                self.auto_phash_images = config["general"]["auto_phash_images"]
            if "auto_match_wanted_images" in config["general"]:
                self.auto_match_wanted_images = config["general"]["auto_match_wanted_images"]
            if "default_wanted_publisher" in config["general"]:
                self.default_wanted_publisher = config["general"]["default_wanted_publisher"]
            if "default_wanted_categories" in config["general"]:
                self.default_wanted_categories = config["general"]["default_wanted_categories"]
            if "default_wanted_providers" in config["general"]:
                self.default_wanted_providers = config["general"]["default_wanted_providers"]
            if "recheck_wanted_on_update" in config["general"]:
                self.recheck_wanted_on_update = config["general"]["recheck_wanted_on_update"]
            if "force_log_level" in config["general"]:
                self.log_level = config["general"]["force_log_level"]
            if "disable_sql_log" in config["general"]:
                self.disable_sql_log = config["general"]["disable_sql_log"]
            if "vertical_image_max_width" in config["general"]:
                self.vertical_image_max_width = config["general"]["vertical_image_max_width"]
            if "horizontal_image_max_width" in config["general"]:
                self.horizontal_image_max_width = config["general"]["horizontal_image_max_width"]

        if "cloning_image_tool" in config:
            if "enable" in config["cloning_image_tool"]:
                self.cloning_image_tool.enable = config["cloning_image_tool"]["enable"]
            if "name" in config["cloning_image_tool"]:
                self.cloning_image_tool.name = config["cloning_image_tool"]["name"]
            if "executable_path" in config["cloning_image_tool"]:
                self.cloning_image_tool.executable_path = config["cloning_image_tool"]["executable_path"]
            if "description" in config["cloning_image_tool"]:
                self.cloning_image_tool.description = config["cloning_image_tool"]["description"]
            if "file_filters" in config["cloning_image_tool"]:
                self.cloning_image_tool.file_filters = config["cloning_image_tool"]["file_filters"]
            if "extra_arguments" in config["cloning_image_tool"]:
                self.cloning_image_tool.extra_arguments = config["cloning_image_tool"]["extra_arguments"]

        if "experimental" in config and config["experimental"] is not None:
            self.experimental.update(config["experimental"])
        if "matchers" in config:
            self.matchers = config["matchers"]
        if "downloaders" in config:
            self.downloaders = config["downloaders"]
        if "auto_wanted" in config:
            if "enable" in config["auto_wanted"]:
                self.auto_wanted.enable = config["auto_wanted"]["enable"]
            if "startup" in config["auto_wanted"]:
                self.auto_wanted.startup = config["auto_wanted"]["startup"]
            if "cycle_timer" in config["auto_wanted"]:
                self.auto_wanted.cycle_timer = config["auto_wanted"]["cycle_timer"]
            if "providers" in config["auto_wanted"]:
                self.auto_wanted.providers = config["auto_wanted"]["providers"]
            if "unwanted_title" in config["auto_wanted"]:
                self.auto_wanted.unwanted_title = config["auto_wanted"]["unwanted_title"]
        if "pushover" in config:
            if "enable" in config["pushover"]:
                self.pushover.enable = config["pushover"]["enable"]
            if "user_key" in config["pushover"]:
                self.pushover.user_key = config["pushover"]["user_key"]
            if "token" in config["pushover"]:
                self.pushover.token = config["pushover"]["token"]
            if "device" in config["pushover"]:
                self.pushover.device = config["pushover"]["device"]
            if "sound" in config["pushover"]:
                self.pushover.sound = config["pushover"]["sound"]
        if "mail_logging" in config:
            if "enable" in config["mail_logging"]:
                self.mail_logging.enable = config["mail_logging"]["enable"]
            if "mailhost" in config["mail_logging"]:
                self.mail_logging.mailhost = config["mail_logging"]["mailhost"]
            if "from" in config["mail_logging"]:
                self.mail_logging.from_ = config["mail_logging"]["from"]
            if "to" in config["mail_logging"]:
                self.mail_logging.to = config["mail_logging"]["to"]
            if "subject" in config["mail_logging"]:
                self.mail_logging.subject = config["mail_logging"]["subject"]
            if "username" in config["mail_logging"] and "password" in config["mail_logging"]:
                self.mail_logging.credentials = (config["mail_logging"]["username"], config["mail_logging"]["password"])
                self.mail_logging.username = config["mail_logging"]["username"]
                self.mail_logging.password = config["mail_logging"]["password"]
        if "elasticsearch" in config:
            if "enable" in config["elasticsearch"]:
                self.elasticsearch.enable = config["elasticsearch"]["enable"]
            if "url" in config["elasticsearch"]:
                self.elasticsearch.url = config["elasticsearch"]["url"]
            if "max_result_window" in config["elasticsearch"]:
                self.elasticsearch.max_result_window = config["elasticsearch"]["max_result_window"]
            if "auto_refresh" in config["elasticsearch"]:
                self.elasticsearch.auto_refresh = config["elasticsearch"]["auto_refresh"]
            if "auto_refresh_gallery" in config["elasticsearch"]:
                self.elasticsearch.auto_refresh_gallery = config["elasticsearch"]["auto_refresh_gallery"]
            if "index_name" in config["elasticsearch"]:
                self.elasticsearch.index_name = config["elasticsearch"]["index_name"]
            if "gallery_index_name" in config["elasticsearch"]:
                self.elasticsearch.gallery_index_name = config["elasticsearch"]["gallery_index_name"]
            if "only_index_public" in config["elasticsearch"]:
                self.elasticsearch.only_index_public = config["elasticsearch"]["only_index_public"]
            if "timeout" in config["elasticsearch"]:
                self.elasticsearch.timeout = config["elasticsearch"]["timeout"]
        if "gallery_dl" in config:
            if "executable_name" in config["gallery_dl"]:
                self.gallery_dl.executable_name = config["gallery_dl"]["executable_name"]
            if "executable_path" in config["gallery_dl"]:
                self.gallery_dl.executable_path = config["gallery_dl"]["executable_path"]
            if "config_file" in config["gallery_dl"]:
                self.gallery_dl.config_file = config["gallery_dl"]["config_file"]
            if "extra_arguments" in config["gallery_dl"]:
                self.gallery_dl.extra_arguments = config["gallery_dl"]["extra_arguments"]
        if "autoupdater" in config:
            if "enable" in config["autoupdater"]:
                self.autoupdater.enable = config["autoupdater"]["enable"]
            if "startup" in config["autoupdater"]:
                self.autoupdater.startup = config["autoupdater"]["startup"]
            if "cycle_timer" in config["autoupdater"]:
                self.autoupdater.cycle_timer = config["autoupdater"]["cycle_timer"]
            if "buffer_back" in config["autoupdater"]:
                self.autoupdater.buffer_back = config["autoupdater"]["buffer_back"]
            if "buffer_after" in config["autoupdater"]:
                self.autoupdater.buffer_after = config["autoupdater"]["buffer_after"]
            if "providers" in config["autoupdater"]:
                self.autoupdater.providers = config["autoupdater"]["providers"]
        if "match_params" in config:
            if "rematch_file" in config["match_params"]:
                self.rematch_file = config["match_params"]["rematch_file"]
            if "rematch_file_list" in config["match_params"]:
                self.rematch_file_list = config["match_params"]["rematch_file_list"]
            if "rehash_files" in config["match_params"]:
                self.rehash_files = config["match_params"]["rehash_files"]
            if "copy_match_file" in config["match_params"]:
                self.copy_match_file = config["match_params"]["copy_match_file"]
        if "locations" in config:
            if "media_root" in config["locations"]:
                self.MEDIA_ROOT = config["locations"]["media_root"]
            if "static_root" in config["locations"]:
                self.STATIC_ROOT = config["locations"]["static_root"]
            if "archive_dl_folder" in config["locations"]:
                self.archive_dl_folder = config["locations"]["archive_dl_folder"]
                if not os.path.exists(os.path.join(self.MEDIA_ROOT, self.archive_dl_folder)):
                    os.makedirs(os.path.join(self.MEDIA_ROOT, self.archive_dl_folder))
            if "torrent_dl_folder" in config["locations"]:
                self.torrent_dl_folder = config["locations"]["torrent_dl_folder"]
                if not os.path.exists(os.path.join(self.MEDIA_ROOT, self.torrent_dl_folder)):
                    os.makedirs(os.path.join(self.MEDIA_ROOT, self.torrent_dl_folder))
            if "log_location" in config["locations"]:
                self.log_location = config["locations"]["log_location"]
                if not os.path.exists(os.path.dirname(self.log_location)):
                    os.makedirs(os.path.dirname(self.log_location))
            else:
                self.log_location = os.path.join(self.default_dir, "viewer.log")
        if "database" in config:
            self.database = config["database"]
        if "torrent" in config:
            self.torrent = config["torrent"]
        if "ftps" in config:
            self.ftps = config["ftps"]
        if "ftp_configs" in config:
            self.ftp_configs = config["ftp_configs"]
        if self.download_ftp_torrent not in self.ftp_configs:
            self.ftp_configs[self.download_ftp_torrent] = self.ftps
        if self.download_ftp_hath not in self.ftp_configs:
            self.ftp_configs[self.download_ftp_hath] = self.ftps
        if "webserver" in config:
            if "bind_address" in config["webserver"]:
                self.webserver.bind_address = config["webserver"]["bind_address"]
            if "bind_port" in config["webserver"]:
                self.webserver.bind_port = config["webserver"]["bind_port"]
            if "socket_file" in config["webserver"]:
                self.webserver.socket_file = config["webserver"]["socket_file"]
            if "enable_ssl" in config["webserver"]:
                self.webserver.enable_ssl = config["webserver"]["enable_ssl"]
            else:
                self.webserver.enable_ssl = False
            if "ssl_certificate" in config["webserver"]:
                self.webserver.ssl_certificate = config["webserver"]["ssl_certificate"]
            if "ssl_private_key" in config["webserver"]:
                self.webserver.ssl_private_key = config["webserver"]["ssl_private_key"]
            if "write_access_log" in config["webserver"]:
                self.webserver.write_access_log = config["webserver"]["write_access_log"]
            if "log_to_screen" in config["webserver"]:
                self.webserver.log_to_screen = config["webserver"]["log_to_screen"]
        if "urls" in config:
            if "media_url" in config["urls"]:
                self.urls.media_url = config["urls"]["media_url"]
            if "static_url" in config["urls"]:
                self.urls.static_url = config["urls"]["static_url"]
            if "viewer_main_url" in config["urls"]:
                self.urls.viewer_main_url = config["urls"]["viewer_main_url"]
            if "behind_proxy" in config["urls"]:
                self.urls.behind_proxy = config["urls"]["behind_proxy"]
            if "cors_allowed_origins" in config["urls"]:
                self.urls.cors_allowed_origins = config["urls"]["cors_allowed_origins"]
            if "cors_allow_all_origins" in config["urls"]:
                self.urls.cors_allow_all_origins = config["urls"]["cors_allow_all_origins"]
            if "behind_proxy" in config["urls"]:
                self.urls.behind_proxy = config["urls"]["behind_proxy"]
            if "enable_public_submit" in config["urls"]:
                self.urls.enable_public_submit = config["urls"]["enable_public_submit"]
            if "enable_public_stats" in config["urls"]:
                self.urls.enable_public_stats = config["urls"]["enable_public_stats"]
            if "enable_public_marks" in config["urls"]:
                self.urls.enable_public_marks = config["urls"]["enable_public_marks"]
            if "public_mark_reasons" in config["urls"]:
                self.urls.public_mark_reasons = config["urls"]["public_mark_reasons"]
            if "external_media_server" in config["urls"]:
                self.urls.external_media_server = config["urls"]["external_media_server"]
            if "external_as_main_download" in config["urls"]:
                self.urls.external_as_main_download = config["urls"]["external_as_main_download"]
            if "main_webserver_url" in config["urls"]:
                self.urls.main_webserver_url = config["urls"]["main_webserver_url"]
            if "elasticsearch_as_main_urls" in config["urls"]:
                self.urls.elasticsearch_as_main_urls = config["urls"]["elasticsearch_as_main_urls"]
        if "remote_site" in config:
            self.remote_site = config["remote_site"]
        if "monitored_links" in config:
            if "enable" in config["monitored_links"]:
                self.monitored_links.enable = config["monitored_links"]["enable"]

        for provider_name, parse_config in self.provider_context.settings_parsers:
            if provider_name in config["providers"] and config["providers"][provider_name] is not None:
                provider_sections = config["providers"][provider_name]
            else:
                provider_sections = {}
            self.providers[provider_name] = parse_config(self, provider_sections)

        for downloader, priority in self.provider_context.get_downloaders_name_priority(self):
            if downloader not in self.downloaders:
                self.downloaders[downloader] = -1

        for matcher, priority in self.provider_context.get_matchers_name_priority(self):
            if matcher not in self.matchers:
                self.matchers[matcher] = -1
