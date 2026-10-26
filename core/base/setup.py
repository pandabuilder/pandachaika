import logging
import os
import shutil
import typing
from copy import deepcopy
from dataclasses import asdict, dataclass, field

import yaml

logger = logging.getLogger(__name__)

# Reads all providers, to parse each provider specific options into the general settings object.
# Main concern with this first approach is that a provider could read settings from another provider (cookies, etc).
# Another option is to have each provider construct it's own settings object, inheriting from this,
# is that it would need to copy the original settings object each time a provider specific setting is needed.
from typing import Any

from core.base.providers import ProviderContext
from core.base.types import DataDict
from core.workers.holder import WorkerContext

if typing.TYPE_CHECKING:
    from viewer.models import Gallery, Archive, WantedGallery, FoundGallery, ArchiveManageEntry, DownloadEvent
    from django.contrib.auth.models import User, AnonymousUser
    from core.base.types import ProviderSettings

    from core.providers.chaika.settings import OwnSettings as ChaikaSettings
    from core.providers.fakku.settings import OwnSettings as FakkuSettings
    from core.providers.generic.settings import OwnSettings as GenericSettings
    from core.providers.irodori.settings import OwnSettings as IrodoriSettings
    from core.providers.j18.settings import OwnSettings as J18Settings
    from core.providers.mega.settings import OwnSettings as MegaSettings
    from core.providers.mugimugi.settings import OwnSettings as MugimugiSettings
    from core.providers.nexus.settings import OwnSettings as NexusSettings
    from core.providers.nhentai.settings import OwnSettings as NhentaiSettings
    from core.providers.nyaa.settings import OwnSettings as NyaaSettings
    from core.providers.panda.settings import OwnSettings as PandaSettings
    from core.providers.twitter.settings import OwnSettings as TwitterSettings
    from core.providers.twodmarket.settings import OwnSettings as TwodmarketSettings
    from core.providers.wanimagazine.settings import OwnSettings as WanimagazineSettings

    class ProvidersDict(dict[str, ProviderSettings]):
        @typing.overload
        def __getitem__(self, key: typing.Literal["chaika"]) -> ChaikaSettings: ...
        @typing.overload
        def __getitem__(self, key: typing.Literal["fakku"]) -> FakkuSettings: ...
        @typing.overload
        def __getitem__(self, key: typing.Literal["generic"]) -> GenericSettings: ...
        @typing.overload
        def __getitem__(self, key: typing.Literal["irodori"]) -> IrodoriSettings: ...
        @typing.overload
        def __getitem__(self, key: typing.Literal["j18"]) -> J18Settings: ...
        @typing.overload
        def __getitem__(self, key: typing.Literal["mega"]) -> MegaSettings: ...
        @typing.overload
        def __getitem__(self, key: typing.Literal["mugimugi"]) -> MugimugiSettings: ...
        @typing.overload
        def __getitem__(self, key: typing.Literal["nexus"]) -> NexusSettings: ...
        @typing.overload
        def __getitem__(self, key: typing.Literal["nhentai"]) -> NhentaiSettings: ...
        @typing.overload
        def __getitem__(self, key: typing.Literal["nyaa"]) -> NyaaSettings: ...
        @typing.overload
        def __getitem__(self, key: typing.Literal["panda"]) -> PandaSettings: ...
        @typing.overload
        def __getitem__(self, key: typing.Literal["twitter"]) -> TwitterSettings: ...
        @typing.overload
        def __getitem__(self, key: typing.Literal["twodmarket"]) -> TwodmarketSettings: ...
        @typing.overload
        def __getitem__(self, key: typing.Literal["wanimagazine"]) -> WanimagazineSettings: ...
        @typing.overload
        def __getitem__(self, key: str) -> typing.Any: ...
        def __getitem__(self, key: typing.Any) -> typing.Any: ...
else:
    ProvidersDict = dict



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
        ("archive_download_queue", "Downloads queued archives in background", "scheduler"),
        ("auto_wanted", "Parses providers for new galleries to create wanted galleries entries", "scheduler"),
    ]


class ConfigFileError(Exception):
    pass


def _parse_bool(val: Any) -> bool:
    if isinstance(val, str):
        return val.lower() in ("true", "1", "t", "yes", "y")
    return bool(val)


@dataclass
class BaseConfigSection:
    def update_from_dict(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        for key, val in data.items():
            if hasattr(self, key):
                if val is None:
                    continue
                current = getattr(self, key)
                if isinstance(current, bool):
                    setattr(self, key, _parse_bool(val))
                elif isinstance(current, int) and not isinstance(current, bool):
                    try:
                        setattr(self, key, int(val))
                    except (ValueError, TypeError):
                        pass
                elif isinstance(current, float):
                    try:
                        setattr(self, key, float(val))
                    except (ValueError, TypeError):
                        pass
                elif isinstance(current, list) and isinstance(val, (list, tuple)):
                    setattr(self, key, list(val))
                elif isinstance(current, str):
                    setattr(self, key, str(val))
                else:
                    setattr(self, key, val)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> typing.Self:
        instance = cls()
        instance.update_from_dict(data)
        return instance

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MonitoredLinksSettings(BaseConfigSection):
    enable: bool = False


@dataclass
class BacklogSearchSettings(BaseConfigSection):
    enable: bool = False
    urls: list[dict[str, str]] = field(default_factory=list)


@dataclass
class AutoWantedSettings(BaseConfigSection):
    enable: bool = False
    startup: bool = False
    cycle_timer: float = 0.0
    providers: list[str] = field(default_factory=list)
    unwanted_title: str = ""
    regexp_unwanted_title: bool = False
    regexp_unwanted_title_icase: bool = False


@dataclass
class AutoUpdaterSettings(BaseConfigSection):
    enable: bool = False
    startup: bool = False
    cycle_timer: float = 0.0
    buffer_back: int = 0
    buffer_after: int = 0
    providers: list[str] = field(default_factory=list)


@dataclass
class PushoverSettings(BaseConfigSection):
    enable: bool = False
    user_key: str = ""
    token: str = ""
    device: str = ""
    sound: str = ""


@dataclass
class GalleryDLSettings(BaseConfigSection):
    executable_name: str = "gallery-dl"
    executable_path: str = ""
    config_file: str = ""
    extra_arguments: str = ""


@dataclass
class CloningImageToolSettings(BaseConfigSection):
    enable: bool = False
    name: str = ""
    executable_path: str = ""
    description: str = ""
    file_filters: list[str] = field(default_factory=list)
    extra_arguments: list[str] = field(default_factory=list)


@dataclass
class MailSettings(BaseConfigSection):
    enable: bool = False
    mailhost: str = ""
    from_: str = ""
    to: str = ""
    credentials: tuple[str, str] | None = None
    username: str = ""
    password: str = ""
    subject: str = "CherryPy Error"

    def update_from_dict(self, data: dict[str, Any]) -> None:
        BaseConfigSection.update_from_dict(self, data)
        if "from" in data:
            self.from_ = str(data["from"])
        if "username" in data and "password" in data:
            self.credentials = (str(data["username"]), str(data["password"]))
            self.username = str(data["username"])
            self.password = str(data["password"])


@dataclass
class ElasticSearchSettings(BaseConfigSection):
    enable: bool = False
    enable_match: bool = False
    url: str = "http://127.0.0.1:9200/"
    max_result_window: int = 10000
    auto_refresh: bool = False
    auto_refresh_gallery: bool = False
    index_name: str = "viewer"
    gallery_index_name: str = "viewer_gallery"
    match_index_name: str = "viewer_match"
    only_index_public: bool = False
    timeout: int = 20


@dataclass
class WebServerSettings(BaseConfigSection):
    bind_address: str = "127.0.0.1"
    bind_port: int = 8090
    enable_ssl: bool = False
    ssl_certificate: str = ""
    ssl_private_key: str = ""
    write_access_log: bool = False
    log_to_screen: bool = True
    socket_file: str | None = None
    socket_queue_size: int = 5
    thread_pool: int = 10
    backend: str = "gunicorn"
    workers: int = 4
    threads: int = 1
    timeout: int = 60

    def update_from_dict(self, data: dict[str, Any]) -> None:
        BaseConfigSection.update_from_dict(self, data)
        backend = data.get("backend") or data.get("engine") or data.get("type")
        if backend:
            self.backend = str(backend).lower()


@dataclass
class UrlSettings(BaseConfigSection):
    behind_proxy: bool = False
    enable_public_submit: bool = False
    enable_public_stats: bool = False
    enable_public_marks: bool = False
    public_mark_reasons: list[str] = field(default_factory=list)
    viewer_main_url: str = ""
    media_url: str = "/media/"
    static_url: str = "/static/"
    external_media_server: str = ""
    main_webserver_url: str = ""
    external_as_main_download: bool = False
    elasticsearch_as_main_urls: bool = False
    elasticsearch_archive_url: str = "es-index"
    elasticsearch_gallery_url: str = "es-gallery-index"
    cors_allowed_origins: list[str] = field(default_factory=list)
    cors_allow_all_origins: bool = False


class Settings:

    # We are storing the context here, but it should be somewhere else, available globally.
    provider_context = ProviderContext()
    workers = WorkerContext()
    gallery_model: "type[Gallery] | None" = None
    archive_model: "type[Archive] | None" = None
    found_gallery_model: "type[FoundGallery] | None" = None
    wanted_gallery_model: "type[WantedGallery] | None" = None
    archive_manage_entry_model: "type[ArchiveManageEntry] | None" = None
    download_event_model: "type[DownloadEvent] | None" = None

    def __init__(
        self,
        load_from_disk: bool = False,
        default_dir: str | None = None,
        load_from_config: DataDict | None = None,
    ) -> None:
        # INTERNAL USE
        self.gallery_reason: str | None = None
        self.stop_nested = False
        self.link_child: str | None = None
        self.link_newer: str | None = None
        self.stop_nested = False
        # Used by autoupdater to not overwrite original value, since it would be replaced by provider_info
        self.keep_dl_type = False
        self.archive_reason = ""
        self.archive_source = ""
        self.archive_details = ""
        self.archive_origin: int | None = None
        self.archive_user: User | AnonymousUser | None = None
        self.event_action: str | None = None
        self.submit_group_uuid: str | None = None
        self.preserve_user_favorites: list[Any] = []
        self.preserve_extracted: bool = False
        self.silent_processing = False
        self.update_metadata_mode = False
        self.is_worker_process: bool = False

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
        self.recycle_failed_checks_downloads = False
        self.requeue_failed_downloads = True
        self.failed_download_downloader = "panda_archive"
        self.requeue_downloader = "panda_archive"
        # Option to auto-purge a torrent after a certain time has passed
        self.max_torrent_lifespan = 0
        self.max_torrent_lifespan_type = "days"

        self.MEDIA_ROOT: str = ""
        self.STATIC_ROOT: str = ""
        self.django_secret_key: str = ""
        self.django_debug_mode: bool = False
        self.download_handler: str = "local"
        self.temp_directory_path: str | None = None
        # More specific, if not set, will use 'download_handler'
        self.download_handler_torrent = ""
        self.download_handler_hath = ""
        self.download_ftp_torrent = "default"
        self.download_ftp_hath = "default"
        self.auto_wanted = AutoWantedSettings()
        self.backlog_search = BacklogSearchSettings()
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

        self.download_progress_checker_startup = True
        self.download_progress_checker_cycle_timer: float = 10

        self.queue_archive_downloads: bool = True
        self.archive_download_queue_startup: bool = True
        self.archive_download_queue_cycle_timer: float = 2.0
        self.parallel_archive_downloaders: int = 1

        self.db_engine = "postgresql"
        self.database: dict[str, Any] = {}
        self.torrent: dict[str, Any] = {}
        self.config: dict[str, Any] = {}
        self.webserver = WebServerSettings()
        self.urls = UrlSettings()

        self.ftps: dict[str, Any] = {}
        self.ftp_configs: dict[str, dict[str, Any]] = {}

        self.rematch_file = False
        self.rematch_file_list = ["non-match"]
        self.rehash_files = False
        self.banned_tags: list[str] = []
        self.banned_uploaders: list[str] = []
        self.banned_search_queries: list[str] = []

        self.matchers: dict[str, int] = {}
        self.downloaders: dict[str, int] = {}
        self.back_up_downloaders: dict[str, int] = {}
        self.custom_downloaders: dict[str, dict[str, int]] = {}
        self.custom_downloader_providers: dict[str, str] = {}

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
        self.process_pool_workers: int = 0
        self.auto_match_wanted_images = False
        self.auto_match_phash_tags_archives = False
        self.default_wanted_publisher = ''
        self.default_wanted_categories = ["Doujinshi", "Artist CG"]
        self.default_wanted_providers = ["panda"]
        self.cloning_image_tool = CloningImageToolSettings()

        self.requests_headers: dict[str, Any] = {}

        self.experimental: dict[str, Any] = {}

        self.providers: ProvidersDict = ProvidersDict()

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

    def create_missing_directories(self) -> None:
        def _ensure_dir(path: str) -> None:
            if path and not os.path.exists(path):
                try:
                    os.makedirs(path, exist_ok=True)
                except OSError as e:
                    logger.warning(f"Could not create directory {path}: {e}")

        if self.archive_dl_folder:
            _ensure_dir(os.path.join(self.MEDIA_ROOT, self.archive_dl_folder))
        if self.torrent_dl_folder:
            _ensure_dir(os.path.join(self.MEDIA_ROOT, self.torrent_dl_folder))
        if self.log_location:
            _ensure_dir(os.path.dirname(self.log_location))

        for provider_name, provider_settings in self.providers.items():
            folders = [a for a in dir(provider_settings) if a.endswith('_dl_folder')]
            for folder in folders:
                folder_name = getattr(provider_settings, folder)
                if folder_name:
                    _ensure_dir(os.path.join(self.MEDIA_ROOT, folder_name))

    def load_from_environment(self):

        if 'DATABASE_ENGINE' in os.environ:
            database_engine = os.environ['DATABASE_ENGINE']
            self.db_engine = database_engine
        else:
            database_engine = self.db_engine

        if 'DATABASE_NAME' in os.environ:
            if database_engine == 'postgresql':
                self.database['postgresql_name'] = os.environ['DATABASE_NAME']
            else:
                self.database['mysql_name'] = os.environ['DATABASE_NAME']

        if 'DATABASE_USERNAME' in os.environ:
            if database_engine == 'postgresql':
                self.database['postgresql_user'] = os.environ['DATABASE_USERNAME']
            else:
                self.database['mysql_user'] = os.environ['DATABASE_USERNAME']

        if 'DATABASE_PASSWORD' in os.environ:
            if database_engine == 'postgresql':
                self.database['postgresql_password'] = os.environ['DATABASE_PASSWORD']
            else:
                self.database['mysql_password'] = os.environ['DATABASE_PASSWORD']

        if 'DATABASE_HOST' in os.environ:
            if database_engine == 'postgresql':
                self.database['postgresql_host'] = os.environ['DATABASE_HOST']
            else:
                self.database['mysql_host'] = os.environ['DATABASE_HOST']

        if 'DATABASE_PORT' in os.environ:
            if database_engine == 'postgresql':
                self.database['postgresql_port'] = os.environ['DATABASE_PORT']
            else:
                self.database['mysql_port'] = os.environ['DATABASE_PORT']

        if 'DEBUG' in os.environ:
            self.django_debug_mode = os.getenv("DEBUG", "False").lower() in ('true', '1', 't')

        if 'DJANGO_SECRET_KEY' in os.environ:
            django_secret_key = os.environ.get("DJANGO_SECRET_KEY")
            if django_secret_key:
                self.django_secret_key = django_secret_key

        if 'MEDIA_ROOT' in os.environ:
            media_root = os.environ.get("MEDIA_ROOT")
            if media_root:
                self.MEDIA_ROOT = media_root

        if 'STATIC_ROOT' in os.environ:
            static_root = os.environ.get("STATIC_ROOT")
            if static_root:
                self.STATIC_ROOT = static_root

        if 'USING_REVERSE_PROXY' in os.environ:
            self.urls.behind_proxy = os.getenv("USING_REVERSE_PROXY", "False").lower() in ('true', '1', 't')

        if 'CORS_ALLOWED_ORIGINS' in os.environ:
            cors_allowed_origins = os.getenv("CORS_ALLOWED_ORIGINS", "")
            if cors_allowed_origins:
                self.urls.cors_allowed_origins = cors_allowed_origins.split(",")

        if 'CORS_ALLOW_ALL_ORIGINS' in os.environ:
            self.urls.cors_allow_all_origins = os.getenv("CORS_ALLOW_ALL_ORIGINS", "False").lower() in ('true', '1', 't')

        if 'WEBSERVER_BACKEND' in os.environ and os.environ['WEBSERVER_BACKEND']:
            self.webserver.backend = os.environ['WEBSERVER_BACKEND'].lower()

        if 'WEBSERVER_WORKERS' in os.environ and os.environ['WEBSERVER_WORKERS']:
            self.webserver.workers = int(os.environ['WEBSERVER_WORKERS'])

        if 'WEBSERVER_THREADS' in os.environ and os.environ['WEBSERVER_THREADS']:
            self.webserver.threads = int(os.environ['WEBSERVER_THREADS'])

        if 'WEBSERVER_TIMEOUT' in os.environ and os.environ['WEBSERVER_TIMEOUT']:
            self.webserver.timeout = int(os.environ['WEBSERVER_TIMEOUT'])

        if 'ELASTICSEARCH_ENABLE' in os.environ and os.environ['ELASTICSEARCH_ENABLE'] != '':
            self.elasticsearch.enable = os.environ['ELASTICSEARCH_ENABLE'].lower() in ('true', '1', 't')

        if 'ELASTICSEARCH_ENABLE_MATCH' in os.environ and os.environ['ELASTICSEARCH_ENABLE_MATCH'] != '':
            self.elasticsearch.enable_match = os.environ['ELASTICSEARCH_ENABLE_MATCH'].lower() in ('true', '1', 't')

        if 'ELASTICSEARCH_URL' in os.environ and os.environ['ELASTICSEARCH_URL']:
            self.elasticsearch.url = os.environ['ELASTICSEARCH_URL']

        if 'ELASTICSEARCH_INDEX_NAME' in os.environ and os.environ['ELASTICSEARCH_INDEX_NAME']:
            self.elasticsearch.index_name = os.environ['ELASTICSEARCH_INDEX_NAME']

        if 'ELASTICSEARCH_GALLERY_INDEX_NAME' in os.environ and os.environ['ELASTICSEARCH_GALLERY_INDEX_NAME']:
            self.elasticsearch.gallery_index_name = os.environ['ELASTICSEARCH_GALLERY_INDEX_NAME']

        if 'ELASTICSEARCH_MATCH_INDEX_NAME' in os.environ and os.environ['ELASTICSEARCH_MATCH_INDEX_NAME']:
            self.elasticsearch.match_index_name = os.environ['ELASTICSEARCH_MATCH_INDEX_NAME']

        if 'ELASTICSEARCH_TIMEOUT' in os.environ and os.environ['ELASTICSEARCH_TIMEOUT']:
            try:
                self.elasticsearch.timeout = int(os.environ['ELASTICSEARCH_TIMEOUT'])
            except ValueError:
                pass

    def load_config_from_file(self, default_dir: str | None = None) -> None:
        if default_dir:
            self.default_dir = default_dir
        elif self.default_dir:
            pass
        elif "PANDA_CONFIG_DIR" in os.environ:
            self.default_dir = os.environ["PANDA_CONFIG_DIR"]
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
        self.load_from_environment()

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

    def parse_custom_downloaders(self, custom_dl_config: Any) -> None:
        self.custom_downloaders = {}
        self.custom_downloader_providers = {}
        if not isinstance(custom_dl_config, dict):
            return

        for custom_name, custom_data in custom_dl_config.items():
            provider: str | None = None
            raw_targets: Any = None

            if isinstance(custom_data, dict):
                if "downloaders" in custom_data:
                    provider = custom_data.get("provider")
                    raw_targets = custom_data.get("downloaders")
                else:
                    provider = custom_data.get("provider")
                    raw_targets = {k: v for k, v in custom_data.items() if k != "provider"}
            elif isinstance(custom_data, list):
                raw_targets = custom_data

            targets: dict[str, int] = {}
            if isinstance(raw_targets, dict):
                for k, v in raw_targets.items():
                    try:
                        targets[str(k)] = int(v)
                    except (ValueError, TypeError):
                        pass
            elif isinstance(raw_targets, list):
                for item in raw_targets:
                    if isinstance(item, dict):
                        for k, v in item.items():
                            try:
                                targets[str(k)] = int(v)
                            except (ValueError, TypeError):
                                pass
                    elif isinstance(item, (list, tuple)) and len(item) == 2:
                        try:
                            targets[str(item[0])] = int(item[1])
                        except (ValueError, TypeError):
                            pass

            if not provider:
                target_providers = set()
                for t in targets.keys():
                    if "_" in t:
                        target_providers.add(t.split("_", 1)[0])
                if len(target_providers) == 1:
                    provider = list(target_providers)[0]
                elif "_" in custom_name:
                    provider = custom_name.split("_", 1)[0]
                elif target_providers:
                    provider = list(target_providers)[0]

            if provider:
                provider_str = str(provider)
                self.custom_downloader_providers[custom_name] = provider_str
                filtered_targets: dict[str, int] = {}
                for t, prio in targets.items():
                    t_provider = t.split("_", 1)[0] if "_" in t else ""
                    if not t_provider or t_provider == provider_str:
                        filtered_targets[t] = prio
                self.custom_downloaders[custom_name] = filtered_targets
            else:
                self.custom_downloaders[custom_name] = targets

    def get_custom_downloader_provider(self, custom_name: str) -> str | None:
        if hasattr(self, "custom_downloader_providers") and custom_name in self.custom_downloader_providers:
            return self.custom_downloader_providers[custom_name]
        if "_" in custom_name:
            return custom_name.split("_", maxsplit=1)[0]
        return None

    def get_downloader_provider(self, downloader_name: str) -> str | None:
        if hasattr(self, "custom_downloaders") and downloader_name in self.custom_downloaders:
            return self.get_custom_downloader_provider(downloader_name)
        if "_" in downloader_name:
            return downloader_name.split("_", maxsplit=1)[0]
        return None

    def disable_provider_downloaders(self, provider_name: str) -> None:
        for downloader in self.downloaders.keys():
            downloader_provider = self.get_downloader_provider(downloader)
            if downloader_provider == provider_name:
                self.downloaders[downloader] = -1

    def enable_downloader_only(self, downloader_name: str) -> None:
        for downloader in self.downloaders.keys():
            self.downloaders[downloader] = -1
        if hasattr(self, "custom_downloaders") and downloader_name in self.custom_downloaders:
            for dl, prio in self.custom_downloaders[downloader_name].items():
                self.downloaders[dl] = prio
        else:
            self.downloaders[downloader_name] = 1

    def allow_downloaders_only(
        self, downloaders: list[str], replace_existing: bool = True, retry_failed: bool = True, redownload: bool = False
    ) -> None:
        for downloader in self.downloaders.keys():
            self.downloaders[downloader] = -1
        for count, downloader in reversed(list(enumerate(downloaders))):
            if hasattr(self, "custom_downloaders") and downloader in self.custom_downloaders:
                for target_downloader, priority in self.custom_downloaders[downloader].items():
                    self.downloaders[target_downloader] = priority
            else:
                self.downloaders[downloader] = count + 1
        self.replace_metadata = replace_existing
        self.retry_failed = retry_failed
        self.redownload = redownload

    def set_update_metadata_options(self, providers: typing.Iterable[str] | None = None) -> None:
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
        gallery_model: "type[Gallery]",
        archive_model: "type[Archive]",
        found_gallery_model: "type[FoundGallery]",
        wanted_gallery_model: "type[WantedGallery]",
        archive_manage_entry_model: "type[ArchiveManageEntry]",
        download_event_model: "type[DownloadEvent]",
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
            if "non_current_links_as_deleted" in config["allowed"]:
                self.non_current_links_as_deleted = config["allowed"]["non_current_links_as_deleted"]
            if "requeue_failed_downloads" in config["allowed"]:
                self.requeue_failed_downloads = _parse_bool(config["allowed"]["requeue_failed_downloads"])
            if "requeue_failed" in config["allowed"]:
                self.requeue_failed_downloads = _parse_bool(config["allowed"]["requeue_failed"])
            if "failed_download_downloader" in config["allowed"]:
                self.failed_download_downloader = config["allowed"]["failed_download_downloader"]
                self.requeue_downloader = self.failed_download_downloader
            if "requeue_downloader" in config["allowed"]:
                self.requeue_downloader = config["allowed"]["requeue_downloader"]
                self.failed_download_downloader = self.requeue_downloader
        if "general" in config:
            if "non_current_links_as_deleted" in config["general"]:
                self.non_current_links_as_deleted = config["general"]["non_current_links_as_deleted"]
            if "recycle_failed_checks_downloads" in config["general"]:
                self.recycle_failed_checks_downloads = config["general"]["recycle_failed_checks_downloads"]
            if "requeue_failed_downloads" in config["general"]:
                self.requeue_failed_downloads = _parse_bool(config["general"]["requeue_failed_downloads"])
            if "requeue_failed" in config["general"]:
                self.requeue_failed_downloads = _parse_bool(config["general"]["requeue_failed"])
            if "failed_download_downloader" in config["general"]:
                self.failed_download_downloader = config["general"]["failed_download_downloader"]
                self.requeue_downloader = self.failed_download_downloader
            if "requeue_downloader" in config["general"]:
                self.requeue_downloader = config["general"]["requeue_downloader"]
                self.failed_download_downloader = self.requeue_downloader
            if "max_torrent_lifespan" in config["general"]:
                self.max_torrent_lifespan = config["general"]["max_torrent_lifespan"]
            if "max_torrent_lifespan_type" in config["general"]:
                self.max_torrent_lifespan_type = config["general"]["max_torrent_lifespan_type"]
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
                self.download_progress_checker_startup = _parse_bool(config["general"]["download_progress_checker_startup"])
            if "download_progress_checker_cycle_timer" in config["general"]:
                self.download_progress_checker_cycle_timer = config["general"]["download_progress_checker_cycle_timer"]
            if "timed_downloader_cycle_timer" in config["general"]:
                self.timed_downloader_cycle_timer = config["general"]["timed_downloader_cycle_timer"]
            if "parallel_post_downloaders" in config["general"]:
                self.parallel_post_downloaders = config["general"]["parallel_post_downloaders"]
            if "queue_archive_downloads" in config["general"]:
                self.queue_archive_downloads = bool(config["general"]["queue_archive_downloads"])
            if "archive_download_queue_startup" in config["general"]:
                self.archive_download_queue_startup = bool(config["general"]["archive_download_queue_startup"])
            if "archive_download_queue_cycle_timer" in config["general"]:
                self.archive_download_queue_cycle_timer = float(
                    config["general"]["archive_download_queue_cycle_timer"]
                )
            if "parallel_archive_downloaders" in config["general"]:
                self.parallel_archive_downloaders = int(config["general"]["parallel_archive_downloaders"])
            if "cherrypy_auto_restart" in config["general"]:
                self.cherrypy_auto_restart = config["general"]["cherrypy_auto_restart"]
            if "discard_tags" in config["general"]:
                self.banned_tags = config["general"]["discard_tags"]
            if "banned_tags" in config["general"]:
                self.banned_tags = config["general"]["banned_tags"]
            if "banned_uploaders" in config["general"]:
                self.banned_uploaders = config["general"]["banned_uploaders"]
            if "banned_search_queries" in config["general"]:
                self.banned_search_queries = config["general"]["banned_search_queries"]
            if "add_as_public" in config["general"]:
                self.add_as_public = config["general"]["add_as_public"]
            if "timeout_timer" in config["general"]:
                self.timeout_timer = config["general"]["timeout_timer"]
            if "mark_similar_new_archives" in config["general"]:
                self.mark_similar_new_archives = config["general"]["mark_similar_new_archives"]
            if "auto_match_phash_tags_archives" in config["general"]:
                self.auto_match_phash_tags_archives = config["general"]["auto_match_phash_tags_archives"]
            if "auto_hash_images" in config["general"]:
                self.auto_hash_images = config["general"]["auto_hash_images"]
            if "auto_phash_images" in config["general"]:
                self.auto_phash_images = config["general"]["auto_phash_images"]
            if "process_pool_workers" in config["general"]:
                self.process_pool_workers = int(config["general"]["process_pool_workers"])
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

        if "cloning_image_tool" in config and isinstance(config["cloning_image_tool"], dict):
            self.cloning_image_tool.update_from_dict(config["cloning_image_tool"])

        if "experimental" in config and config["experimental"] is not None:
            self.experimental.update(config["experimental"])
        if "matchers" in config:
            self.matchers = config["matchers"]
        if "downloaders" in config:
            self.downloaders = config["downloaders"]
        if "requeue_failed_downloads" in config:
            self.requeue_failed_downloads = _parse_bool(config["requeue_failed_downloads"])
        if "requeue_failed" in config:
            self.requeue_failed_downloads = _parse_bool(config["requeue_failed"])
        if "failed_download_downloader" in config:
            self.failed_download_downloader = config["failed_download_downloader"]
            self.requeue_downloader = self.failed_download_downloader
        if "requeue_downloader" in config:
            self.requeue_downloader = config["requeue_downloader"]
            self.failed_download_downloader = self.requeue_downloader
        if "custom_downloaders" in config:
            self.parse_custom_downloaders(config["custom_downloaders"])
        if "archive_download_queue" in config and isinstance(config["archive_download_queue"], dict):
            adq_cfg = config["archive_download_queue"]
            if "enable" in adq_cfg:
                self.queue_archive_downloads = bool(adq_cfg["enable"])
            if "queue_archive_downloads" in adq_cfg:
                self.queue_archive_downloads = bool(adq_cfg["queue_archive_downloads"])
            if "startup" in adq_cfg:
                self.archive_download_queue_startup = bool(adq_cfg["startup"])
            if "cycle_timer" in adq_cfg:
                self.archive_download_queue_cycle_timer = float(adq_cfg["cycle_timer"])
            if "parallel_downloaders" in adq_cfg:
                self.parallel_archive_downloaders = int(adq_cfg["parallel_downloaders"])

        if "auto_wanted" in config and isinstance(config["auto_wanted"], dict):
            self.auto_wanted.update_from_dict(config["auto_wanted"])
        if "backlog_search" in config and isinstance(config["backlog_search"], dict):
            self.backlog_search.update_from_dict(config["backlog_search"])
        if "pushover" in config and isinstance(config["pushover"], dict):
            self.pushover.update_from_dict(config["pushover"])
        if "mail_logging" in config and isinstance(config["mail_logging"], dict):
            self.mail_logging.update_from_dict(config["mail_logging"])
        if "elasticsearch" in config and isinstance(config["elasticsearch"], dict):
            self.elasticsearch.update_from_dict(config["elasticsearch"])
        if "gallery_dl" in config and isinstance(config["gallery_dl"], dict):
            self.gallery_dl.update_from_dict(config["gallery_dl"])
        if "autoupdater" in config and isinstance(config["autoupdater"], dict):
            self.autoupdater.update_from_dict(config["autoupdater"])
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
            if "torrent_dl_folder" in config["locations"]:
                self.torrent_dl_folder = config["locations"]["torrent_dl_folder"]
            if "log_location" in config["locations"]:
                self.log_location = config["locations"]["log_location"]
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
        if "webserver" in config and isinstance(config["webserver"], dict):
            self.webserver.update_from_dict(config["webserver"])
        if "urls" in config and isinstance(config["urls"], dict):
            self.urls.update_from_dict(config["urls"])
        if "remote_site" in config:
            self.remote_site = config["remote_site"]
        if "monitored_links" in config and isinstance(config["monitored_links"], dict):
            self.monitored_links.update_from_dict(config["monitored_links"])

        for provider_name, parse_config in self.provider_context.settings_parsers:
            if "providers" in config and provider_name in config["providers"] and config["providers"][provider_name] is not None:
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
