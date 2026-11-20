import logging
import os
import shutil
import threading
import typing
from copy import deepcopy
from typing import Generic, TypeVar, overload

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
        ("contextual_extractor_worker", "Extracts contextual metrics from archives via manga-extractor", "processor"),
        ("web_queue", "Queue that processes gallery links, one at a time", "queue"),
        ("webcrawler", "Processes gallery links, can coexist with web_queue", "processor"),
        ("foldercrawler", "Processes galleries on the filesystem", "processor"),
        (
            "download_progress_checker",
            "Follows download progress, and transfers archives downloaded with other programs (torrent, hath)",
            "processor",
        ),
        ("archive_download_queue", "Downloads queued archives in background", "scheduler"),
        ("auto_wanted", "Parses providers for new galleries to create wanted galleries entries", "scheduler"),
        ("timed_admin_reporter", "Emails the admin report to the staff users who enabled it", "scheduler"),
    ]


class ConfigFileError(Exception):
    pass


from core.base.config_models import (
    AdminReportSettings,
    AllowedSettings,
    AutoUpdaterSettings,
    AutoWantedSettings,
    BacklogSearchSettings,
    CloningImageToolSettings,
    DatabaseSettings,
    ElasticSearchSettings,
    FtpsSettings,
    GalleryDLSettings,
    GeneralSettings,
    LocationsSettings,
    MailSettings,
    MangaExtractorSettings,
    MatchParamsSettings,
    MonitoredLinksSettings,
    PandaGalleryConfig,
    PushoverSettings,
    RemoteSiteSettings,
    RetentionSettings,
    TorrentSettings,
    UrlSettings,
    WebServerSettings,
    _parse_bool,
    get_config_schema,
)

# Section models whose fields are exposed as top-level Settings attributes through _ModelField.
_FLATTENED_SECTION_MODELS = ("general_model", "allowed_model", "locations_model", "match_params_model")


_T = TypeVar("_T")


class _ModelField(Generic[_T]):
    """A ``Settings`` attribute stored on one of its config section models.

    ``Settings.wait_timer`` reads and writes ``settings.general_model.wait_timer``, so the model stays the
    single source of truth and assignments go through its validation.
    """

    def __init__(self, model_attr: str, field: str | None = None) -> None:
        self.model_attr = model_attr
        self.field = field or ""

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name
        if not self.field:
            self.field = name

    @overload
    def __get__(self, obj: None, objtype: type | None = None) -> "_ModelField[_T]": ...
    @overload
    def __get__(self, obj: object, objtype: type | None = None) -> _T: ...
    def __get__(self, obj: object | None, objtype: type | None = None) -> "_T | _ModelField[_T]":
        if obj is None:
            return self
        return getattr(getattr(obj, self.model_attr), self.field)

    def __set__(self, obj: object, value: _T) -> None:
        setattr(getattr(obj, self.model_attr), self.field, value)


class Settings:

    # We are storing the context here, but it should be somewhere else, available globally.
    provider_context = ProviderContext()
    workers = WorkerContext()

    # 'allowed' section
    replace_metadata = _ModelField[bool]("allowed_model")
    redownload = _ModelField[bool]("allowed_model")
    auto_download_nested = _ModelField[bool]("allowed_model")
    retry_failed = _ModelField[bool]("allowed_model")
    internal_matches_for_non_matches = _ModelField[bool]("allowed_model")
    convert_others_to_zip = _ModelField[bool]("allowed_model")
    # Can also be set in 'general' (see _resolve_shared_fields).
    non_current_links_as_deleted = _ModelField[bool]("allowed_model")

    # 'general' section
    db_engine = _ModelField[str]("general_model")
    filename_filter = _ModelField[list[str]]("general_model")
    django_secret_key = _ModelField[str]("general_model")
    django_debug_mode = _ModelField[bool]("general_model")
    download_handler = _ModelField[str]("general_model")
    # More specific, if not set, will use 'download_handler'
    download_handler_torrent = _ModelField[str]("general_model")
    download_handler_hath = _ModelField[str]("general_model")
    download_ftp_torrent = _ModelField[str]("general_model")
    download_ftp_hath = _ModelField[str]("general_model")
    temp_directory_path = _ModelField[str | None]("general_model")
    wait_timer = _ModelField[float]("general_model")
    timeout_timer = _ModelField[int]("general_model")
    parallel_post_downloaders = _ModelField[int]("general_model")
    # Also settable from the 'archive_download_queue' section.
    parallel_archive_downloaders = _ModelField[int]("general_model")
    queue_archive_downloads = _ModelField[bool]("general_model")
    archive_download_queue_startup = _ModelField[bool]("general_model")
    archive_download_queue_cycle_timer = _ModelField[float]("general_model")
    archive_download_stall_timeout = _ModelField[float]("general_model")
    worker_task_stall_timeout = _ModelField[float]("general_model")
    download_progress_checker_startup = _ModelField[bool]("general_model")
    download_progress_checker_cycle_timer = _ModelField[float]("general_model")
    cherrypy_auto_restart = _ModelField[bool]("general_model")
    stop_on_seen_links = _ModelField[bool]("general_model")
    banned_tags = _ModelField[list[str]]("general_model")
    banned_uploaders = _ModelField[list[str]]("general_model")
    banned_search_queries = _ModelField[list[str]]("general_model")
    ignore_global_filters = _ModelField[bool]("general_model")
    add_as_public = _ModelField[bool]("general_model")
    mark_similar_new_archives = _ModelField[bool]("general_model")
    auto_hash_images = _ModelField[bool]("general_model")
    auto_phash_images = _ModelField[bool]("general_model")
    process_pool_workers = _ModelField[int]("general_model")
    auto_match_wanted_images = _ModelField[bool]("general_model")
    auto_match_phash_tags_archives = _ModelField[bool]("general_model")
    default_wanted_publisher = _ModelField[str]("general_model")
    default_wanted_categories = _ModelField[list[str]]("general_model")
    default_wanted_providers = _ModelField[list[str]]("general_model")
    recheck_wanted_on_update = _ModelField[bool]("general_model")
    vertical_image_max_width = _ModelField[int]("general_model")
    horizontal_image_max_width = _ModelField[int]("general_model")
    log_level = _ModelField[str]("general_model", "force_log_level")
    disable_sql_log = _ModelField[bool]("general_model")
    # Option to auto-purge a torrent after a certain time has passed
    max_torrent_lifespan = _ModelField[int]("general_model")
    max_torrent_lifespan_type = _ModelField[str]("general_model")
    recycle_failed_checks_downloads = _ModelField[bool]("general_model")
    # Can also be set in 'allowed' or at the top level (see _resolve_shared_fields).
    requeue_failed_downloads = _ModelField[bool]("general_model")
    failed_download_downloader = _ModelField[str]("general_model")
    requeue_downloader = _ModelField[str]("general_model", "failed_download_downloader")

    # 'locations' section
    MEDIA_ROOT = _ModelField[str]("locations_model", "media_root")
    STATIC_ROOT = _ModelField[str]("locations_model", "static_root")
    archive_dl_folder = _ModelField[str]("locations_model")
    torrent_dl_folder = _ModelField[str]("locations_model")

    # 'match_params' section
    rematch_file = _ModelField[bool]("match_params_model")
    rematch_file_list = _ModelField[list[str]]("match_params_model")
    rehash_files = _ModelField[bool]("match_params_model")
    copy_match_file = _ModelField[bool]("match_params_model")

    @classmethod
    def model_field_names(cls) -> list[str]:
        """Names of the attributes stored on a config section model (not in the instance ``__dict__``)."""
        return [name for name, value in vars(cls).items() if isinstance(value, _ModelField)]

    def attribute_items(self) -> list[tuple[str, Any]]:
        """Instance attributes plus the model-backed ones, leaving out the section models those are stored on."""
        items = [(k, v) for k, v in vars(self).items() if k not in _FLATTENED_SECTION_MODELS]
        items.extend((name, getattr(self, name)) for name in self.model_field_names())
        items.append(("log_location", self.log_location))
        return items

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
        # Monitored link phases: only store the new links as pending / only process the stored ones.
        self.capture_links_only: bool = False
        self.skip_link_capture: bool = False
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
        self.current_task_type: str = ""
        self.current_task_id: int | None = None
        self.current_task_priority: int = 10
        self.dispatch_gallery_creation: bool = True
        self.activity_callback: typing.Callable[[], None] | None = None
        self.cancel_event: threading.Event | None = None
        self.is_cancelled: typing.Callable[[], bool] | None = None
        self.crawl_batch_collector: Any = None

        # Config sections. Most top-level attributes below are stored on these models (see the
        # _ModelField declarations on the class), so there is a single source of truth per setting.
        self.general_model = GeneralSettings()
        self.allowed_model = AllowedSettings()
        self.locations_model = LocationsSettings()
        self.match_params_model = MatchParamsSettings()
        self.database = DatabaseSettings()
        self.torrent = TorrentSettings()
        self.ftps = FtpsSettings()
        self.remote_site = RemoteSiteSettings()
        self.auto_wanted = AutoWantedSettings()
        self.backlog_search = BacklogSearchSettings()
        self.autoupdater = AutoUpdaterSettings()
        self.pushover = PushoverSettings()
        self.mail_logging = MailSettings()
        self.elasticsearch = ElasticSearchSettings()
        self.gallery_dl = GalleryDLSettings()
        self.monitored_links = MonitoredLinksSettings()
        self.retention = RetentionSettings()
        self.admin_report = AdminReportSettings()
        self.manga_extractor = MangaExtractorSettings()
        self.cloning_image_tool = CloningImageToolSettings()
        self.webserver = WebServerSettings()
        self.urls = UrlSettings()

        # USER SETTINGS (runtime only, not read from the config file)
        self.monitored_link: typing.Any | None = None
        self.already_seen_links_mode: str = "dont_use"
        self.how_long_after_already_seen: typing.Any | None = None

        self.config: dict[str, Any] = {}
        # Named remote connections; entries for download_ftp_torrent / download_ftp_hath default to 'ftps'.
        self.ftp_configs: dict[str, dict[str, Any]] = {}

        self.matchers: dict[str, int] = {}
        self.downloaders: dict[str, int] = {}
        self.back_up_downloaders: dict[str, int] = {}
        self.custom_downloaders: dict[str, dict[str, int]] = {}
        self.custom_downloader_providers: dict[str, str] = {}

        self.requests_headers: dict[str, Any] = {}

        self.experimental: dict[str, Any] = {}

        self.providers: ProvidersDict = ProvidersDict()

        self._proxy: str = ""
        self.proxies: dict[str, Any] = {}

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

        if 'DATABASE_CONN_MAX_AGE' in os.environ:
            val = os.environ['DATABASE_CONN_MAX_AGE'].strip()
            self.database['conn_max_age'] = None if val.lower() in ('none', 'null', '') else int(val)

        if 'DATABASE_WORKER_CONN_MAX_AGE' in os.environ:
            val = os.environ['DATABASE_WORKER_CONN_MAX_AGE'].strip()
            self.database['worker_conn_max_age'] = None if val.lower() in ('none', 'null', '') else int(val)

        if 'DATABASE_CONN_HEALTH_CHECKS' in os.environ:
            self.database['conn_health_checks'] = os.environ['DATABASE_CONN_HEALTH_CHECKS'].strip().lower() in ('true', '1', 't', 'yes')

        for pool_env in ('DATABASE_POOLING_ENABLE', 'DATABASE_POOL_ENABLE'):
            if pool_env in os.environ:
                self.database['pooling'] = os.environ[pool_env].strip().lower() in ('true', '1', 't', 'yes')

        if 'DATABASE_POOL_MIN_SIZE' in os.environ:
            self.database['pool_min_size'] = int(os.environ['DATABASE_POOL_MIN_SIZE'].strip())

        if 'DATABASE_POOL_MAX_SIZE' in os.environ:
            val = os.environ['DATABASE_POOL_MAX_SIZE'].strip()
            self.database['pool_max_size'] = None if val.lower() in ('none', 'null', '') else int(val)

        if 'DATABASE_POOL_TIMEOUT' in os.environ:
            self.database['pool_timeout'] = float(os.environ['DATABASE_POOL_TIMEOUT'].strip())

        if 'DATABASE_POOL_MAX_LIFETIME' in os.environ:
            val = os.environ['DATABASE_POOL_MAX_LIFETIME'].strip()
            self.database['pool_max_lifetime'] = None if val.lower() in ('none', 'null', '') else float(val)

        if 'DATABASE_POOL_MAX_IDLE' in os.environ:
            val = os.environ['DATABASE_POOL_MAX_IDLE'].strip()
            self.database['pool_max_idle'] = None if val.lower() in ('none', 'null', '') else float(val)

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

        if 'MANGA_EXTRACTOR_ENABLE' in os.environ and os.environ['MANGA_EXTRACTOR_ENABLE'] != '':
            self.manga_extractor.enable = os.environ['MANGA_EXTRACTOR_ENABLE'].lower() in ('true', '1', 't', 'yes')

        if 'MANGA_EXTRACTOR_URL' in os.environ and os.environ['MANGA_EXTRACTOR_URL']:
            self.manga_extractor.url = os.environ['MANGA_EXTRACTOR_URL']

        for token_var in ('MANGA_EXTRACTOR_AUTH_TOKEN', 'AUTH_TOKEN', 'MANGA_EXTRACTOR_TOKEN', 'API_TOKEN'):
            if token_var in os.environ and os.environ[token_var].strip():
                self.manga_extractor.auth_token = os.environ[token_var].strip()
                break

        if 'MANGA_EXTRACTOR_RUN_ON_NEW_ARCHIVE' in os.environ and os.environ['MANGA_EXTRACTOR_RUN_ON_NEW_ARCHIVE'] != '':
            self.manga_extractor.run_on_new_archive = os.environ['MANGA_EXTRACTOR_RUN_ON_NEW_ARCHIVE'].lower() in ('true', '1', 't', 'yes')

        if 'MANGA_EXTRACTOR_TIMEOUT' in os.environ and os.environ['MANGA_EXTRACTOR_TIMEOUT']:
            try:
                self.manga_extractor.timeout = float(os.environ['MANGA_EXTRACTOR_TIMEOUT'])
            except ValueError:
                pass

        if 'MANGA_EXTRACTOR_DEFAULT_PROFILE' in os.environ and os.environ['MANGA_EXTRACTOR_DEFAULT_PROFILE']:
            self.manga_extractor.default_profile = os.environ['MANGA_EXTRACTOR_DEFAULT_PROFILE'].strip()

        if 'MANGA_EXTRACTOR_BACKGROUND_PROFILE' in os.environ and os.environ['MANGA_EXTRACTOR_BACKGROUND_PROFILE']:
            self.manga_extractor.background_profile = os.environ['MANGA_EXTRACTOR_BACKGROUND_PROFILE'].strip()

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

    def to_pydantic(self) -> PandaGalleryConfig:
        """Convert current runtime Settings into a fully validated PandaGalleryConfig model,

        incorporating any environment variable overrides.
        """
        base_config: dict[str, Any] = dict(self.config) if self.config else {}
        db_copy = dict(base_config.get("database", {}))
        db_copy.update(self.database.to_dict())
        base_config["database"] = db_copy
        if self.db_engine:
            gen_copy = dict(base_config.get("general", {}))
            gen_copy["db_engine"] = self.db_engine
            base_config["general"] = gen_copy
        return PandaGalleryConfig.model_validate(base_config)

    @classmethod
    def get_config_schema(cls) -> dict[str, Any]:
        """Return the JSON Schema dictionary defining configuration structure and metadata."""
        return get_config_schema()

    def _resolve_shared_fields(self, config: DataDict) -> None:
        """Apply settings that more than one config location can set to the model that stores them.

        - non_current_links_as_deleted lives on 'allowed'; 'general' is used when only it sets the key.
        - requeue_failed_downloads / failed_download_downloader live on 'general'. When there is no 'general'
          section, 'allowed' (requeue_failed / requeue_downloader) provides them. Top-level keys override both.
        - 'archive_download_queue' overrides the matching 'general' queue settings.
        """
        general = config.get("general")
        allowed = config.get("allowed")
        general_keys = general.keys() if isinstance(general, dict) else None
        allowed_keys = allowed.keys() if isinstance(allowed, dict) else None

        if general_keys is not None and "non_current_links_as_deleted" in general_keys and not (
            allowed_keys is not None and "non_current_links_as_deleted" in allowed_keys
        ):
            self.allowed_model.non_current_links_as_deleted = self.general_model.non_current_links_as_deleted

        if allowed_keys is not None and general_keys is None:
            self.general_model.requeue_failed_downloads = self.allowed_model.requeue_failed_downloads
            self.general_model.failed_download_downloader = self.allowed_model.failed_download_downloader

        if "requeue_failed_downloads" in config:
            self.requeue_failed_downloads = _parse_bool(config["requeue_failed_downloads"])
        if "requeue_failed" in config:
            self.requeue_failed_downloads = _parse_bool(config["requeue_failed"])
        if "failed_download_downloader" in config:
            self.failed_download_downloader = config["failed_download_downloader"]
        if "requeue_downloader" in config:
            self.failed_download_downloader = config["requeue_downloader"]

        adq_cfg = config.get("archive_download_queue")
        if isinstance(adq_cfg, dict):
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

    def dict_to_settings(self, config: DataDict) -> None:
        if "requests_headers" in config and config["requests_headers"] is not None:
            self.requests_headers.update(config["requests_headers"])

        section_models: list[tuple[str, Any]] = [
            ("allowed", self.allowed_model),
            ("general", self.general_model),
            ("locations", self.locations_model),
            ("match_params", self.match_params_model),
            ("database", self.database),
            ("torrent", self.torrent),
            ("ftps", self.ftps),
            ("remote_site", self.remote_site),
            ("cloning_image_tool", self.cloning_image_tool),
            ("auto_wanted", self.auto_wanted),
            ("backlog_search", self.backlog_search),
            ("pushover", self.pushover),
            ("mail_logging", self.mail_logging),
            ("elasticsearch", self.elasticsearch),
            ("gallery_dl", self.gallery_dl),
            ("autoupdater", self.autoupdater),
            ("manga_extractor", self.manga_extractor),
            ("webserver", self.webserver),
            ("urls", self.urls),
            ("monitored_links", self.monitored_links),
            ("retention", self.retention),
            ("admin_report", self.admin_report),
        ]
        for section_name, model in section_models:
            if section_name in config and isinstance(config[section_name], dict):
                model.update_from_dict(config[section_name])

        self._resolve_shared_fields(config)

        if "experimental" in config and config["experimental"] is not None:
            self.experimental.update(config["experimental"])
        if "matchers" in config and isinstance(config["matchers"], dict):
            self.matchers = dict(config["matchers"])
        if "downloaders" in config and isinstance(config["downloaders"], dict):
            self.downloaders = dict(config["downloaders"])
        if "custom_downloaders" in config:
            self.parse_custom_downloaders(config["custom_downloaders"])

        if "ftp_configs" in config:
            # Copy, so the default entries added below don't end up in self.config (written back to settings.yaml).
            self.ftp_configs = dict(config["ftp_configs"] or {})
        for ftp_config_name in (self.download_ftp_torrent, self.download_ftp_hath):
            if ftp_config_name not in self.ftp_configs:
                self.ftp_configs[ftp_config_name] = self.ftps.to_dict()

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

    @property
    def log_location(self) -> str:
        return self.locations_model.log_location or os.path.join(self.default_dir, "viewer.log")

    @log_location.setter
    def log_location(self, value: str) -> None:
        self.locations_model.log_location = value

    @property
    def stop_on_seen_link(self) -> bool:
        return self.stop_on_seen_links

    @stop_on_seen_link.setter
    def stop_on_seen_link(self, value: bool) -> None:
        self.stop_on_seen_links = value

    @property
    def proxy(self) -> str:
        return self._proxy

    @proxy.setter
    def proxy(self, value: str | None) -> None:
        self._proxy = str(value) if value else ""
        if self._proxy:
            self.proxies = {"http": self._proxy, "https": self._proxy}
        else:
            self.proxies = {}
