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
        ("extract_worker", "Extracts images from archives in the background", "processor"),
        ("publish_worker", "Publishes archives in the background", "processor"),
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


from core.base.config_models import (
    AllowedSettings,
    ArchiveDownloadQueueSettings,
    AutoUpdaterSettings,
    AutoWantedSettings,
    BacklogSearchSettings,
    BaseConfigModel,
    BaseConfigSection,
    CloningImageToolSettings,
    DatabaseSettings,
    ElasticSearchSettings,
    FtpsSettings,
    GalleryDLSettings,
    GeneralSettings,
    LocationsSettings,
    MailSettings,
    MatchParamsSettings,
    MonitoredLinksSettings,
    PandaGalleryConfig,
    PushoverSettings,
    RemoteSiteSettings,
    TorrentSettings,
    UrlSettings,
    WebServerSettings,
    _parse_bool,
    generate_default_yaml_content,
    get_config_schema,
    save_default_yaml_file,
)


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
        self.activity_callback: typing.Callable[[], None] | None = None

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
        self.archive_download_stall_timeout: float = 180.0
        self.worker_task_stall_timeout: float = 1800.0
        self.parallel_archive_downloaders: int = 1

        self.general_model = GeneralSettings()
        self.allowed_model = AllowedSettings()
        self.locations_model = LocationsSettings()
        self.database_model = DatabaseSettings()
        self.torrent_model = TorrentSettings()
        self.ftps_model = FtpsSettings()
        self.match_params_model = MatchParamsSettings()
        self.remote_site_model = RemoteSiteSettings()

        self.db_engine = "postgresql"
        self.database: dict[str, Any] = self.database_model.to_dict()
        self.torrent: dict[str, Any] = self.torrent_model.to_dict()
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

        self.wait_timer: float = 6.0
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
            self.db_engine = os.environ['DATABASE_ENGINE']

        if 'DATABASE_NAME' in os.environ:
            self.database['postgresql_name'] = os.environ['DATABASE_NAME']

        if 'DATABASE_USERNAME' in os.environ:
            self.database['postgresql_user'] = os.environ['DATABASE_USERNAME']

        if 'DATABASE_PASSWORD' in os.environ:
            self.database['postgresql_password'] = os.environ['DATABASE_PASSWORD']

        if 'DATABASE_HOST' in os.environ:
            self.database['postgresql_host'] = os.environ['DATABASE_HOST']

        if 'DATABASE_PORT' in os.environ:
            self.database['postgresql_port'] = os.environ['DATABASE_PORT']

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

    def to_pydantic(self) -> PandaGalleryConfig:
        """Convert current runtime Settings into a fully validated PandaGalleryConfig model,

        incorporating any environment variable overrides.
        """
        base_config: dict[str, Any] = dict(self.config) if self.config else {}
        if hasattr(self, "database") and isinstance(self.database, dict):
            db_copy = dict(base_config.get("database", {}))
            db_copy.update(self.database)
            base_config["database"] = db_copy
        if hasattr(self, "db_engine") and self.db_engine:
            gen_copy = dict(base_config.get("general", {}))
            gen_copy["db_engine"] = self.db_engine
            base_config["general"] = gen_copy
        return PandaGalleryConfig.model_validate(base_config)

    @classmethod
    def get_config_schema(cls) -> dict[str, Any]:
        """Return the JSON Schema dictionary defining configuration structure and metadata."""
        return get_config_schema()

    def dict_to_settings(self, config: DataDict) -> None:
        if "requests_headers" in config and config["requests_headers"] is not None:
            self.requests_headers.update(config["requests_headers"])
        if "allowed" in config and isinstance(config["allowed"], dict):
            self.allowed_model.update_from_dict(config["allowed"])
            self.replace_metadata = self.allowed_model.replace_metadata
            self.redownload = self.allowed_model.redownload
            self.auto_download_nested = self.allowed_model.auto_download_nested
            self.retry_failed = self.allowed_model.retry_failed
            self.internal_matches_for_non_matches = self.allowed_model.internal_matches_for_non_matches
            self.convert_others_to_zip = self.allowed_model.convert_others_to_zip
            self.non_current_links_as_deleted = self.allowed_model.non_current_links_as_deleted
            self.requeue_failed_downloads = self.allowed_model.requeue_failed_downloads
            self.failed_download_downloader = self.allowed_model.failed_download_downloader
            self.requeue_downloader = self.failed_download_downloader

        if "general" in config and isinstance(config["general"], dict):
            self.general_model.update_from_dict(config["general"])
            self.db_engine = self.general_model.db_engine
            self.filename_filter = self.general_model.filename_filter
            self.django_secret_key = self.general_model.django_secret_key
            self.django_debug_mode = self.general_model.django_debug_mode
            self.download_handler = self.general_model.download_handler
            self.download_handler_torrent = self.general_model.download_handler_torrent
            self.download_handler_hath = self.general_model.download_handler_hath
            self.download_ftp_torrent = self.general_model.download_ftp_torrent
            self.download_ftp_hath = self.general_model.download_ftp_hath
            self.temp_directory_path = self.general_model.temp_directory_path
            self.wait_timer = self.general_model.wait_timer
            self.timeout_timer = self.general_model.timeout_timer
            self.timed_downloader_startup = self.general_model.timed_downloader_startup
            self.timed_downloader_cycle_timer = self.general_model.timed_downloader_cycle_timer
            self.parallel_post_downloaders = self.general_model.parallel_post_downloaders
            self.parallel_archive_downloaders = self.general_model.parallel_archive_downloaders
            self.queue_archive_downloads = self.general_model.queue_archive_downloads
            self.archive_download_queue_startup = self.general_model.archive_download_queue_startup
            self.archive_download_queue_cycle_timer = self.general_model.archive_download_queue_cycle_timer
            self.archive_download_stall_timeout = self.general_model.archive_download_stall_timeout
            self.worker_task_stall_timeout = self.general_model.worker_task_stall_timeout
            self.download_progress_checker_startup = self.general_model.download_progress_checker_startup
            self.download_progress_checker_cycle_timer = self.general_model.download_progress_checker_cycle_timer
            self.cherrypy_auto_restart = self.general_model.cherrypy_auto_restart
            self.banned_tags = list(self.general_model.banned_tags)
            self.banned_uploaders = list(self.general_model.banned_uploaders)
            self.banned_search_queries = list(self.general_model.banned_search_queries)
            self.add_as_public = self.general_model.add_as_public
            self.mark_similar_new_archives = self.general_model.mark_similar_new_archives
            self.auto_hash_images = self.general_model.auto_hash_images
            self.auto_phash_images = self.general_model.auto_phash_images
            self.process_pool_workers = self.general_model.process_pool_workers
            self.auto_match_wanted_images = self.general_model.auto_match_wanted_images
            self.auto_match_phash_tags_archives = self.general_model.auto_match_phash_tags_archives
            self.default_wanted_publisher = self.general_model.default_wanted_publisher
            self.default_wanted_categories = list(self.general_model.default_wanted_categories)
            self.default_wanted_providers = list(self.general_model.default_wanted_providers)
            self.recheck_wanted_on_update = self.general_model.recheck_wanted_on_update
            self.vertical_image_max_width = self.general_model.vertical_image_max_width
            self.horizontal_image_max_width = self.general_model.horizontal_image_max_width
            self.log_level = self.general_model.force_log_level
            self.disable_sql_log = self.general_model.disable_sql_log
            self.max_torrent_lifespan = self.general_model.max_torrent_lifespan
            self.max_torrent_lifespan_type = self.general_model.max_torrent_lifespan_type
            self.non_current_links_as_deleted = self.general_model.non_current_links_as_deleted
            self.recycle_failed_checks_downloads = self.general_model.recycle_failed_checks_downloads
            self.requeue_failed_downloads = self.general_model.requeue_failed_downloads
            self.failed_download_downloader = self.general_model.failed_download_downloader
            self.requeue_downloader = self.failed_download_downloader

        # Global-level overrides for requeue
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

        if "cloning_image_tool" in config and isinstance(config["cloning_image_tool"], dict):
            self.cloning_image_tool.update_from_dict(config["cloning_image_tool"])

        if "experimental" in config and config["experimental"] is not None:
            self.experimental.update(config["experimental"])
        if "matchers" in config:
            self.matchers = config["matchers"]
        if "downloaders" in config:
            self.downloaders = config["downloaders"]
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
            if "stall_timeout" in adq_cfg:
                self.archive_download_stall_timeout = float(adq_cfg["stall_timeout"])
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
        if "match_params" in config and isinstance(config["match_params"], dict):
            self.match_params_model.update_from_dict(config["match_params"])
            self.rematch_file = self.match_params_model.rematch_file
            self.rematch_file_list = self.match_params_model.rematch_file_list
            self.rehash_files = self.match_params_model.rehash_files
            self.copy_match_file = self.match_params_model.copy_match_file

        if "locations" in config and isinstance(config["locations"], dict):
            self.locations_model.update_from_dict(config["locations"])
            self.MEDIA_ROOT = self.locations_model.media_root
            self.STATIC_ROOT = self.locations_model.static_root
            self.archive_dl_folder = self.locations_model.archive_dl_folder
            self.torrent_dl_folder = self.locations_model.torrent_dl_folder
            if self.locations_model.log_location:
                self.log_location = self.locations_model.log_location
            else:
                self.log_location = os.path.join(self.default_dir, "viewer.log")

        if "database" in config and isinstance(config["database"], dict):
            self.database_model.update_from_dict(config["database"])
            self.database = self.database_model.to_dict()

        if "torrent" in config and isinstance(config["torrent"], dict):
            self.torrent_model.update_from_dict(config["torrent"])
            self.torrent = self.torrent_model.to_dict()
            if self.torrent_model.pass_:
                self.torrent["pass"] = self.torrent_model.pass_

        if "ftps" in config and isinstance(config["ftps"], dict):
            self.ftps_model.update_from_dict(config["ftps"])
            self.ftps = self.ftps_model.to_dict()

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
        if "remote_site" in config and isinstance(config["remote_site"], dict):
            self.remote_site_model.update_from_dict(config["remote_site"])
            self.remote_site = self.remote_site_model.to_dict()
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
