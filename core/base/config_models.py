from __future__ import annotations

import logging
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)


def _parse_bool(val: Any) -> bool:
    if isinstance(val, str):
        return val.lower() in ("true", "1", "t", "yes", "y")
    return bool(val)


class BaseConfigModel(BaseModel):
    """Base model for all configuration sections, providing type coercion,

    dict-like access, serialization, and backward-compatible helper methods.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        arbitrary_types_allowed=True,
        validate_assignment=True,
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_none_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            new_data = dict(data)
            for k, v in list(new_data.items()):
                if v is None and k in cls.model_fields:
                    finfo = cls.model_fields[k]
                    # If field expects a model or dict and is passed None, coerce to default/{}
                    factory = finfo.default_factory
                    if callable(factory):
                        try:
                            new_data[k] = factory()  # type: ignore[call-arg]
                        except TypeError:
                            new_data[k] = {}
                    elif isinstance(finfo.default, (BaseModel, dict)):
                        new_data[k] = {}
            return new_data
        return data

    def update_from_dict(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        for key, val in data.items():
            if val is None:
                continue
            field_name = key
            if field_name not in self.__class__.model_fields:
                for fname, f_info in self.__class__.model_fields.items():
                    if f_info.alias == key:
                        field_name = fname
                        break

            if field_name in self.__class__.model_fields:
                current = getattr(self, field_name, None)
                if isinstance(current, BaseConfigModel) and isinstance(val, dict):
                    current.update_from_dict(val)
                else:
                    try:
                        setattr(self, field_name, val)
                    except Exception:
                        pass
            else:
                setattr(self, key, val)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        if not isinstance(data, dict):
            return cls()
        return cls.model_validate(data)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=False, mode="python")

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def __setitem__(self, key: str, value: Any) -> None:
        setattr(self, key, value)

    def __contains__(self, key: object) -> bool:
        return hasattr(self, str(key))

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


# Backward compatibility alias
BaseConfigSection = BaseConfigModel


# ============================================================================
# Section Models
# ============================================================================


class GeneralSettings(BaseConfigModel):
    """General system settings governing database engines, download handling,

    worker loops, rate limits, and archive lifecycle behavior.
    """

    db_engine: str = Field(
        default="postgresql",
        description="Database backend engine. Supported option: 'postgresql'. MySQL and SQLite are unsupported.",
    )
    filename_filter: list[str] = Field(
        default_factory=lambda: ["*.zip"],
        description="File glob patterns used when scanning directories for new archives. Allows accepting alternate extensions such as '*.cbz'.",
    )
    django_secret_key: str = Field(
        default="89!yi9gvd2r*m5rq9y-elj86u*zg@-j3ce90@^7+0(-!fkgpzd",
        description="Django secret key used for session signing and cryptographic tokens. Must be changed in production.",
    )
    django_debug_mode: bool = Field(
        default=False,
        description="Enable verbose Django debug mode with interactive stack traces. Must be false in production.",
    )
    download_handler: str = Field(
        default="local",
        description="Handler for completed downloads: 'local' (same server), 'remote' (fetch via FTPS from remote seedbox), 'local_copy', 'local_move', or 'local_hardlink'.",
    )
    download_handler_torrent: str = Field(
        default="",
        description="Specific download handler override for torrents. If blank, falls back to general.download_handler.",
    )
    download_handler_hath: str = Field(
        default="",
        description="Specific download handler override for H@H downloads. If blank, falls back to general.download_handler.",
    )
    download_ftp_torrent: str = Field(
        default="default",
        description="FTP configuration key from ftp_configs to use for downloading completed torrents.",
    )
    download_ftp_hath: str = Field(
        default="default",
        description="FTP configuration key from ftp_configs to use for downloading completed H@H archives.",
    )
    temp_directory_path: str | None = Field(
        default=None,
        description="Custom filesystem directory for temporary extraction and download staging. If null, the system temp directory is used.",
    )
    wait_timer: float = Field(
        default=6.0,
        description="Politeness delay (in seconds) between outbound HTTP requests to avoid rate limits and IP bans (default: 6.0).",
    )
    timeout_timer: int = Field(
        default=25,
        description="HTTP request timeout (in seconds) for outgoing provider web requests.",
    )
    timed_downloader_startup: bool = Field(
        default=False,
        description="Automatically start the timed downloader background worker when the runner launches.",
    )
    timed_downloader_cycle_timer: float = Field(
        default=5.0,
        description="Cycle interval (in seconds) for the timed downloader background worker loop.",
    )
    parallel_post_downloaders: int = Field(
        default=4,
        description="Number of parallel post-downloader workers (each opens an independent FTP/FTPS connection).",
    )
    parallel_archive_downloaders: int = Field(
        default=1,
        description="Maximum concurrent HTTP archive downloads across providers (0 for synchronous inline downloads, 1+ for background queue).",
    )
    queue_archive_downloads: bool = Field(
        default=True,
        description="Enable queuing of archive HTTP downloads through the background archive download queue.",
    )
    archive_download_queue_startup: bool = Field(
        default=True,
        description="Start the archive download queue scheduler on worker startup.",
    )
    archive_download_queue_cycle_timer: float = Field(
        default=2.0,
        description="Loop interval (in seconds) for the background archive download queue scheduler.",
    )
    archive_download_stall_timeout: float = Field(
        default=180.0,
        description="Stall/inactivity timeout (in seconds) for queued HTTP archive downloads. Downloads making steady progress continue indefinitely, whereas stalled downloads with no progress for this duration are aborted and retried/failed (0 disables).",
    )
    worker_task_stall_timeout: float = Field(
        default=1800.0,
        description="Stall/inactivity timeout (in seconds) for background TaskQueue jobs in WorkerRunner. Tasks making no progress for this duration are terminated and marked as FAILED (0 disables).",
    )
    download_progress_checker_startup: bool = Field(
        default=True,
        description="Start the download progress checker scheduler on worker startup.",
    )
    download_progress_checker_cycle_timer: float = Field(
        default=10.0,
        description="Polling interval (in seconds) for querying torrent client and H@H download progress.",
    )
    cherrypy_auto_restart: bool = Field(
        default=False,
        description="Automatically reload the web server when source file modifications are detected (CherryPy engine only).",
    )
    banned_tags: list[str] = Field(
        default_factory=lambda: [
            "language:thai",
            "language:chinese",
            "language:korean",
            "language:french",
            "language:spanish",
            "language:italian",
            "language:hungarian",
            "language:portuguese",
            "language:polish",
            "language:russian",
            "language:vietnamese",
        ],
        alias="discard_tags",
        description="Tags that trigger automatic discarding of galleries during crawling and matching.",
    )
    banned_uploaders: list[str] = Field(
        default_factory=lambda: ["random_guy"],
        description="Uploader usernames whose galleries will be automatically discarded during crawling and matching.",
    )
    banned_search_queries: list[str] = Field(
        default_factory=lambda: [
            'tags.full:"language:chinese" AND (NOT tags.full:"language:japanese")'
        ],
        description="Elasticsearch DSL query filters. Galleries matching these queries in the match index are automatically discarded.",
    )
    add_as_public: bool = Field(
        default=False,
        description="Automatically mark all newly discovered or downloaded galleries as public (visible to anonymous guests).",
    )
    mark_similar_new_archives: bool = Field(
        default=False,
        description="Automatically generate review marks in the Mark system when a newly added archive is similar to an existing one.",
    )
    auto_hash_images: bool = Field(
        default=False,
        description="Calculate image SHA1 and CRC32 checksums for every page when adding new archives.",
    )
    auto_phash_images: bool = Field(
        default=False,
        description="Compute perceptual image hashes (pHash) on pages when adding new archives to enable visual similarity matching.",
    )
    process_pool_workers: int = Field(
        default=0,
        description="Number of worker processes in the process pool for CPU-bound tasks (pHash, resizing, zip CRC). 0 auto-selects half of CPU cores.",
    )
    auto_match_wanted_images: bool = Field(
        default=False,
        description="Automatically match wanted images against newly added archives (requires OpenCV and NumPy).",
    )
    auto_match_phash_tags_archives: bool = Field(
        default=False,
        description="Automatically correlate perceptual hash tags across similar archives in the database.",
    )
    default_wanted_publisher: str = Field(
        default="",
        description="Default publisher value applied when creating new wanted galleries.",
    )
    default_wanted_categories: list[str] = Field(
        default_factory=lambda: ["Doujinshi", "Artist CG"],
        description="Default categories selected for wanted gallery searches.",
    )
    default_wanted_providers: list[str] = Field(
        default_factory=lambda: ["panda"],
        description="Default upstream providers queried for wanted gallery searches.",
    )
    recheck_wanted_on_update: bool = Field(
        default=False,
        description="Create review marks when metadata updates invalidate a previously matched WantedGallery.",
    )
    vertical_image_max_width: int = Field(
        default=900,
        description="Maximum width (in pixels) for vertical portrait pages during reduced/resized extraction.",
    )
    horizontal_image_max_width: int = Field(
        default=1500,
        description="Maximum width (in pixels) for horizontal landscape pages during reduced/resized extraction.",
    )
    force_log_level: str = Field(
        default="INFO",
        description="Minimum logging level output to log files and database ('DEBUG', 'INFO', 'WARNING', 'ERROR').",
    )
    disable_sql_log: bool = Field(
        default=False,
        description="Suppress Django SQL query logging in console output during debug mode.",
    )
    max_torrent_lifespan: int = Field(
        default=0,
        description="Maximum time before completed torrents are removed from the torrent client (0 keeps torrents indefinitely).",
    )
    max_torrent_lifespan_type: str = Field(
        default="days",
        description="Time unit for max_torrent_lifespan ('minutes', 'hours', 'days', 'weeks').",
    )
    non_current_links_as_deleted: bool = Field(
        default=False,
        description="When crawling revision chains, treat non-latest/superseded gallery links as deleted.",
    )
    recycle_failed_checks_downloads: bool = Field(
        default=False,
        description="Automatically move downloaded archives failing integrity or checksum checks to the recycle bin.",
    )
    requeue_failed_downloads: bool = Field(
        default=True,
        description="Automatically re-enqueue failed downloads (e.g. corrupt zip, size mismatch) with an alternate downloader.",
    )
    failed_download_downloader: str = Field(
        default="panda_archive",
        description="Downloader or custom downloader to invoke when requeueing a failed download.",
    )


class AllowedSettings(BaseConfigModel):
    """User permissions and operational toggles controlling metadata overwrites,

    redownloads, and archive conversions.
    """

    replace_metadata: bool = Field(
        default=False,
        description="Allow overwriting existing gallery metadata with upstream data when processing an already added gallery.",
    )
    redownload: bool = Field(
        default=False,
        description="Allow redownloading archives that have already been downloaded previously. Caution: link monitors will repeatedly re-fetch files if enabled.",
    )
    auto_download_nested: bool = Field(
        default=False,
        description="For providers supporting gallery relationships, automatically fetch and download child/parent galleries.",
    )
    retry_failed: bool = Field(
        default=False,
        description="Allow retrying downloads for galleries previously marked with a failed download status.",
    )
    internal_matches_for_non_matches: bool = Field(
        default=False,
        description="After finding a non-match upstream, attempt matching against local database archives immediately.",
    )
    convert_others_to_zip: bool = Field(
        default=True,
        description="Automatically convert downloaded RAR (.rar) and 7-Zip (.7z) archives into standard ZIP files.",
    )
    non_current_links_as_deleted: bool = Field(
        default=False,
        description="Add links not considered the last of their chain as deleted.",
    )
    requeue_failed_downloads: bool = Field(
        default=True,
        alias="requeue_failed",
        description="Requeue failed downloads with a fallback downloader.",
    )
    failed_download_downloader: str = Field(
        default="panda_archive",
        alias="requeue_downloader",
        description="Downloader to use when requeueing a failed download.",
    )


class MonitoredLinksSettings(BaseConfigModel):
    """Controls the background threads for monitored links polling."""

    enable: bool = Field(
        default=False,
        description="Enable the monitored links background subsystem to periodically check subscribed URLs.",
    )


class BacklogSearchUrlItem(BaseConfigModel):
    name: str = Field(default="", description="Identifier name for the backlog search feed.")
    url: str = Field(default="", description="Target search URL to crawl for backlog archives.")


class BacklogSearchSettings(BaseConfigModel):
    """Periodic search crawler to identify and ingest historical backlog works."""

    enable: bool = Field(
        default=False,
        description="Enable periodic backlog search crawler.",
    )
    urls: list[BacklogSearchUrlItem] | list[dict[str, str]] = Field(
        default_factory=lambda: [
            {
                "name": "panda_backlog",
                "url": "https://e-hentai.org/?f_doujinshi=1&f_manga=1&f_artistcg=1&f_gamecg=1&f_western=0&f_non-h=1&f_imageset=1&f_cosplay=0&f_asianporn=0&f_misc=0&f_apply=Apply+Filter&f_search=",
            },
            {
                "name": "chaika_backlog",
                "url": "https://panda.chaika.moe/meta/es-gallery-json/?sort=posted_date&order=desc&q=",
            },
        ],
        description="List of backlog search queries and feed URLs to crawl.",
    )


class AutoWantedSettings(BaseConfigModel):
    """Automatic wanted gallery generator based on provider scraping and filters."""

    enable: bool = Field(
        default=True,
        description="Enable automatic wanted gallery generator.",
    )
    startup: bool = Field(
        default=False,
        description="Start the auto-wanted scheduler upon worker runner startup.",
    )
    cycle_timer: float = Field(
        default=4.0,
        description="Cycle interval (in seconds) between wanted gallery generator runs.",
    )
    providers: list[str] = Field(
        default_factory=lambda: ["twitter"],
        description="Providers queried by the auto-wanted generator.",
    )
    unwanted_title: str = Field(
        default="[Chinese]",
        description="Text filter automatically assigned to the unwanted_title field on generated wanted galleries.",
    )
    regexp_unwanted_title: bool = Field(
        default=False,
        description="Treat unwanted_title as a regular expression pattern.",
    )
    regexp_unwanted_title_icase: bool = Field(
        default=False,
        description="Enable case-insensitive matching for the unwanted_title regex.",
    )


class AutoUpdaterSettings(BaseConfigModel):
    """Automatic background updater for refreshing gallery tags, ratings, and metadata."""

    enable: bool = Field(
        default=False,
        description="Enable automatic gallery metadata updates from upstream providers.",
    )
    startup: bool = Field(
        default=False,
        description="Start the auto-updater worker upon worker runner launch.",
    )
    cycle_timer: float = Field(
        default=2.0,
        description="Cycle timer (in seconds) between update checks.",
    )
    buffer_back: int = Field(
        default=8,
        description="Number of days in the past from current date to filter galleries for updating.",
    )
    buffer_after: int = Field(
        default=0,
        description="Number of days after/offset to filter galleries for updating.",
    )
    providers: list[str] = Field(
        default_factory=lambda: ["panda"],
        description="List of providers enabled for automatic metadata updating.",
    )


class PushoverSettings(BaseConfigModel):
    """Push notifications via the Pushover service for wanted gallery discovery and alerts."""

    enable: bool = Field(
        default=False,
        description="Enable Pushover notifications for found wanted galleries and critical alerts.",
    )
    user_key: str = Field(
        default="",
        description="Pushover user or group key.",
    )
    token: str = Field(
        default="",
        description="Pushover application API token.",
    )
    device: str = Field(
        default="",
        description="Optional specific device name to receive notifications. Blank sends to all devices.",
    )
    sound: str = Field(
        default="",
        description="Notification sound (e.g. 'pushover', 'bike', 'siren'). Blank uses account default.",
    )


class GalleryDLSettings(BaseConfigModel):
    """Configuration for external gallery-dl downloader integration."""

    executable_name: str = Field(
        default="gallery-dl",
        description="gallery-dl executable name.",
    )
    executable_path: str = Field(
        default="",
        description="Custom absolute path to gallery-dl binary. If blank, resolved from system PATH.",
    )
    config_file: str = Field(
        default="",
        description="Optional custom configuration file path passed to gallery-dl.",
    )
    extra_arguments: str = Field(
        default="",
        description="Additional command-line arguments passed to gallery-dl invocations.",
    )


class CloningImageToolSettings(BaseConfigModel):
    """External image processing tool applied during archive cloning and modification."""

    enable: bool = Field(
        default=False,
        description="Enable external image post-processing tool during archive cloning.",
    )
    name: str = Field(
        default="",
        description="Display name for the image tool.",
    )
    executable_path: str = Field(
        default="",
        description="Executable command path. Use '{input}' and '{output}' kwargs for runtime replacements.",
    )
    description: str = Field(
        default="",
        description="Human-readable description of the tool.",
    )
    file_filters: list[str] = Field(
        default_factory=lambda: ["*.png"],
        description="File glob patterns to process with the tool (e.g. ['*.png', '*.jpg']).",
    )
    extra_arguments: list[str] | str = Field(
        default_factory=list,
        description="Additional CLI arguments passed to the executable.",
    )


class MailSettings(BaseConfigModel):
    """SMTP email notifications for server errors and gallery submissions."""

    enable: bool = Field(
        default=False,
        description="Enable email error reporting and submission notifications.",
    )
    mailhost: str = Field(
        default="",
        description="SMTP mail server host (e.g. 'smtp.example.com' or 'smtp.example.com:587').",
    )
    from_: str = Field(
        default="",
        alias="from",
        description="Sender email address for outgoing alerts.",
    )
    to: str = Field(
        default="",
        description="Destination email address for system alerts.",
    )
    credentials: tuple[str, str] | None = Field(
        default=None,
        description="Tuple of (username, password) for SMTP auth.",
    )
    username: str = Field(
        default="",
        description="SMTP username for authentication.",
    )
    password: str = Field(
        default="",
        description="SMTP password for authentication.",
    )
    subject: str = Field(
        default="CherryPy Error",
        description="Subject line for error notification emails.",
    )

    @model_validator(mode="after")
    def _compute_credentials(self) -> Self:
        if self.username and self.password and self.credentials is None:
            self.credentials = (self.username, self.password)
        return self

    def update_from_dict(self, data: dict[str, Any]) -> None:
        super().update_from_dict(data)
        if "from" in data:
            self.from_ = str(data["from"])
        if "username" in data and "password" in data:
            self.credentials = (str(data["username"]), str(data["password"]))
            self.username = str(data["username"])
            self.password = str(data["password"])


class ElasticSearchSettings(BaseConfigModel):
    """Elasticsearch 8.x configuration for full-text search, autocomplete, and match deduplication."""

    enable: bool = Field(
        default=False,
        description="Enable Elasticsearch for advanced search and faceted navigation.",
    )
    enable_match: bool = Field(
        default=False,
        description="Enable dedicated Elasticsearch matching index for fast title and hash similarity lookups.",
    )
    url: str = Field(
        default="http://localhost:9200/",
        description="Elasticsearch base connection URL.",
    )
    max_result_window: int = Field(
        default=10000,
        description="Maximum search result window limit for pagination (index.max_result_window).",
    )
    auto_refresh: bool = Field(
        default=False,
        description="Force index refresh immediately after indexing archives (leave false in production).",
    )
    auto_refresh_gallery: bool = Field(
        default=False,
        description="Force index refresh immediately after indexing upstream galleries.",
    )
    index_name: str = Field(
        default="viewer",
        description="Elasticsearch index name for local archives.",
    )
    gallery_index_name: str = Field(
        default="viewer_gallery",
        description="Elasticsearch index name for upstream gallery metadata.",
    )
    match_index_name: str = Field(
        default="viewer_match",
        description="Elasticsearch index name used for fast title and tag matching.",
    )
    only_index_public: bool = Field(
        default=False,
        description="Only index public archives, hiding private archives from search results.",
    )
    timeout: int = Field(
        default=20,
        description="Elasticsearch request timeout in seconds.",
    )


class WebServerSettings(BaseConfigModel):
    """Web server configuration for WSGI hosting (Gunicorn or CherryPy)."""

    backend: str = Field(
        default="gunicorn",
        description="Webserver backend to use: 'gunicorn' (recommended for production) or 'cherrypy'.",
    )
    bind_address: str = Field(
        default="0.0.0.0",
        description="IP address to bind the web server to ('127.0.0.1' for local proxy, '0.0.0.0' for public access).",
    )
    bind_port: int = Field(
        default=8090,
        description="TCP port to listen on.",
    )
    socket_file: str | None = Field(
        default=None,
        description="UNIX domain socket file path to bind to (e.g. '/tmp/panda.sock') instead of a TCP port.",
    )
    enable_ssl: bool = Field(
        default=False,
        description="Enable HTTPS/SSL on the web server directly.",
    )
    ssl_certificate: str = Field(
        default="/home/panda/certs/panda.crt",
        description="Path to SSL certificate file (required if enable_ssl is true).",
    )
    ssl_private_key: str = Field(
        default="/home/panda/certs/panda.key",
        description="Path to SSL private key file (required if enable_ssl is true).",
    )
    write_access_log: bool = Field(
        default=False,
        description="Write HTTP access log to file.",
    )
    log_to_screen: bool = Field(
        default=False,
        description="Print web server logs to console screen / stdout.",
    )
    socket_queue_size: int = Field(
        default=5,
        description="TCP listen backlog size for incoming connection queues.",
    )
    thread_pool: int = Field(
        default=10,
        description="Worker thread pool size for request handling (CherryPy only).",
    )
    workers: int = Field(
        default=4,
        description="Number of worker processes (Gunicorn only).",
    )
    threads: int = Field(
        default=1,
        description="Number of threads per worker process (Gunicorn only).",
    )
    timeout: int = Field(
        default=60,
        description="Worker timeout in seconds before restarting unresponsive workers (Gunicorn only).",
    )

    def update_from_dict(self, data: dict[str, Any]) -> None:
        super().update_from_dict(data)
        backend = data.get("backend") or data.get("engine") or data.get("type")
        if backend:
            self.backend = str(backend).lower()


class UrlSettings(BaseConfigModel):
    """URL routing, reverse proxy headers, CORS, and public access endpoints."""

    media_url: str = Field(
        default="/media/",
        description="Base URL path for serving user media and archives.",
    )
    static_url: str = Field(
        default="/static/",
        description="Base URL path for serving static CSS and JavaScript assets.",
    )
    viewer_main_url: str = Field(
        default="",
        description="Optional custom URL prefix for viewer routes.",
    )
    behind_proxy: bool = Field(
        default=False,
        description="Set to true if served behind a reverse proxy (Nginx, Traefik, Caddy) to fix absolute URL generation.",
    )
    enable_public_submit: bool = Field(
        default=False,
        description="Enable the public /submit endpoint for unregistered visitors to suggest URLs.",
    )
    enable_public_stats: bool = Field(
        default=False,
        description="Enable public access to repository statistics dashboards.",
    )
    enable_public_marks: bool = Field(
        default=False,
        description="Enable public display of similarity marks on archive detail pages.",
    )
    public_mark_reasons: list[str] = Field(
        default_factory=lambda: ["images_sha1_similarity", "images_phash_similarity"],
        description="List of mark reason identifiers visible to public viewers when enable_public_marks is true.",
    )
    external_media_server: str = Field(
        default="",
        description="Base URL of an external CDN or media server used for offloading archive downloads.",
    )
    external_as_main_download: bool = Field(
        default=False,
        description="Direct primary download links to the external media server instead of the local web server.",
    )
    main_webserver_url: str = Field(
        default="",
        description="Canonical public-facing URL (scheme + domain) used to resolve absolute links in background tasks.",
    )
    elasticsearch_as_main_urls: bool = Field(
        default=False,
        description="Route standard search URLs directly through Elasticsearch endpoints.",
    )
    elasticsearch_archive_url: str = Field(
        default="es-index",
        description="View name or URL path for Elasticsearch archive queries.",
    )
    elasticsearch_gallery_url: str = Field(
        default="es-gallery-index",
        description="View name or URL path for Elasticsearch gallery queries.",
    )
    cors_allowed_origins: list[str] = Field(
        default_factory=list,
        description="List of origin domains allowed to access APIs via Cross-Origin Resource Sharing (CORS).",
    )
    cors_allow_all_origins: bool = Field(
        default=False,
        description="Allow all origins to access API endpoints (Access-Control-Allow-Origin: *).",
    )


class MatchParamsSettings(BaseConfigModel):
    """Matching engine parameters and re-matching behavior."""

    rematch_file: bool = Field(
        default=False,
        description="Re-evaluate archives that already have a match entry if their current match type is in rematch_file_list.",
    )
    rematch_file_list: list[str] = Field(
        default_factory=lambda: ["non-match"],
        description="List of match_types eligible for rematching.",
    )
    rehash_files: bool = Field(
        default=False,
        description="Recompute CRC32 checksums for local archives during matching.",
    )
    copy_match_file: bool = Field(
        default=True,
        description="If an archive with the same CRC32 checksum is found, copy its matching metadata directly.",
    )


class LocationsSettings(BaseConfigModel):
    """Filesystem storage paths for media, static assets, downloads, and logs."""

    media_root: str = Field(
        default="",
        description="Absolute filesystem path where media files (archives, thumbnails, extracted pages) are stored.",
    )
    static_root: str = Field(
        default="",
        description="Absolute filesystem path where static assets are collected. If blank, Django defaults to '<BASE_DIR>/static/'.",
    )
    archive_dl_folder: str = Field(
        default="galleries/archive_dls",
        description="Relative subdirectory under media_root where direct archive HTTP downloads are stored.",
    )
    torrent_dl_folder: str = Field(
        default="galleries/torrent_dls",
        description="Relative subdirectory under media_root where completed torrent downloads are stored.",
    )
    log_location: str = Field(
        default="",
        description="Custom absolute path for application log file. If blank, defaults to '<default_dir>/viewer.log'.",
    )


class DatabaseSettings(BaseConfigModel):
    """Database connection parameters for PostgreSQL backend."""

    postgresql_name: str = Field(default="pandaweb", description="PostgreSQL database name.")
    postgresql_user: str = Field(default="pandaadmin", description="PostgreSQL username.")
    postgresql_password: str = Field(default="pandaadmin", description="PostgreSQL password.")
    postgresql_host: str = Field(default="localhost", description="PostgreSQL host server.")
    postgresql_port: str = Field(default="5432", description="PostgreSQL port.")

    @field_validator("postgresql_port", mode="before")
    @classmethod
    def _coerce_postgresql_port(cls, v: Any) -> str:
        if v is None:
            return "5432"
        return str(v)


class TorrentSettings(BaseConfigModel):
    """Torrent client RPC connection details (Transmission or qBittorrent)."""

    client: str = Field(
        default="transmission",
        description="Torrent client engine. Supported options: 'transmission' or 'qbittorrent'.",
    )
    user: str = Field(
        default="panda",
        description="Username for torrent client RPC / WebUI authentication.",
    )
    pass_: str = Field(
        default="panda",
        alias="pass",
        description="Password for torrent client RPC / WebUI authentication.",
    )
    address: str = Field(
        default="http://localhost/transmission/rpc",
        description="URL address or host for torrent client RPC / WebUI.",
    )
    port: int = Field(
        default=9091,
        description="Port number for torrent client RPC / WebUI.",
    )
    download_dir: str = Field(
        default="/home/panda/downloads/torrents",
        description="Directory path where the torrent client saves completed downloads.",
    )
    no_certificate_check: bool = Field(
        default=False,
        description="Skip SSL certificate validation when communicating with the torrent client.",
    )


class FtpsSettings(BaseConfigModel):
    """Remote FTPS connection settings for retrieving downloads from a remote seedbox."""

    user: str = Field(default="panda", description="FTPS username on remote server.")
    passwd: str = Field(default="panda", description="FTPS password on remote server.")
    address: str = Field(default="remote.server.com", description="Remote server hostname or IP address.")
    remote_torrent_dir: str = Field(
        default="/home/pandaremote/downloads/torrent",
        description="Directory on remote server where finished torrent downloads are located.",
    )
    bind_host: str | None = Field(
        default=None,
        description="Optional local IP address to bind to when opening FTPS connections.",
    )
    bind_port: int | None = Field(
        default=None,
        description="Optional local port to bind to when opening FTPS connections.",
    )
    no_certificate_check: bool = Field(
        default=False,
        description="Skip SSL/TLS certificate validation for self-signed remote certificates.",
    )


class RemoteSiteSettings(BaseConfigModel):
    """Instance-to-instance synchronization with remote Panda Backup deployments."""

    api_url: str = Field(
        default="https://panda.chaika.moe/jsonapi",
        description="Remote instance JSON API endpoint URL.",
    )
    user_token: str = Field(
        default="asdf1234",
        description="Long-lived user authentication token on the remote instance.",
    )
    remote_folder: str = Field(
        default="/media/galleries/archives",
        description="Remote folder where archives are stored on the remote instance.",
    )


class ArchiveDownloadQueueSettings(BaseConfigModel):
    """Scheduler settings for background HTTP archive downloads."""

    enable: bool = Field(
        default=True,
        alias="queue_archive_downloads",
        description="Enable background archive download queue scheduler.",
    )
    startup: bool = Field(
        default=True,
        description="Automatically start archive download queue scheduler on worker launch.",
    )
    cycle_timer: float = Field(
        default=2.0,
        description="Cycle interval (in seconds) between queue processing steps.",
    )
    parallel_downloaders: int = Field(
        default=1,
        description="Maximum concurrent downloads allowed by the queue scheduler.",
    )


# ============================================================================
# Provider Models
# ============================================================================


class BaseProviderGeneralSettings(BaseConfigModel):
    proxy: str = Field(default="", description="HTTP/HTTPS proxy server URL for this provider.")
    timeout_timer: int = Field(default=25, description="HTTP request timeout (in seconds).")
    wait_timer: int = Field(default=6, description="Rate-limit wait timer (in seconds) between requests.")
    stop_page_number: int | None = Field(default=None, description="Maximum page number to scrape when crawling.")
    autoupdater_enable: bool = Field(default=False, description="Enable metadata autoupdater for this provider.")
    autoupdater_timer: float = Field(default=0.0, description="Cycle timer for autoupdater.")
    autoupdater_buffer_back: int = Field(default=0, description="Days back for autoupdater filtering.")
    autoupdater_buffer_after: int = Field(default=0, description="Days after for autoupdater filtering.")
    queue_archive_downloads: bool | None = Field(
        default=None, description="Override global archive download queueing for this provider."
    )
    max_parallel_archive_downloads: int = Field(
        default=1, description="Maximum parallel archive downloads for this provider."
    )


class GenericLocationsSettings(BaseConfigModel):
    archive_dl_folder: str = Field(default="", description="Provider-specific archive download directory override.")
    torrent_dl_folder: str = Field(default="", description="Provider-specific torrent download directory override.")


class PandaProviderGeneralSettings(BaseProviderGeneralSettings):
    proceed_with_download_if_lowest_cost: bool = Field(
        default=False,
        description="Check cost of H@H and direct archive downloads, proceeding only with the lowest or equal cost.",
    )
    stop_page_number: int | None = Field(
        default=5,
        description="Maximum page number to stop crawling at when scanning gallery feeds.",
    )
    accepted_rss_categories: list[Any] = Field(
        default_factory=lambda: [
            ["Doujinshi"],
            ["Manga"],
            ["Artist CG Sets"],
            ["Game CG Sets"],
            ["Image Sets"],
            ["Non-H"],
            ["Cosplay"],
            ["Asian Porn"],
            ["Misc"],
            ["Private"],
        ],
        description="Pre-filter accepted gallery categories reported by upstream RSS.",
    )
    use_ex_for_fjord: bool = Field(
        default=False,
        description="Use ExHentai instead of E-Hentai for region-blocked/fjord galleries.",
    )
    auto_process_newer: bool = Field(
        default=False,
        description="Automatically process newer revisions in a gallery chain when discovered.",
    )
    auto_process_first: bool = Field(
        default=False,
        description="Automatically download the first original version in a gallery chain.",
    )
    auto_process_parent: bool = Field(
        default=False,
        description="Automatically download the direct parent gallery.",
    )
    mark_relationships: bool = Field(
        default=False,
        description="Create review marks linking discovered parent/child relationships.",
    )
    confirm_files_after_download: bool = Field(
        default=False,
        description="Verify downloaded files against provider file lists upon completion.",
    )
    maximum_gp_cost_for_dls: float | None = Field(
        default=None,
        description="Maximum GP cost allowable for H@H and archive downloads (0 = free only, null = disabled).",
    )
    maximum_gp_mb_ratio_for_dls: float | None = Field(
        default=None,
        description="Maximum GP per Megabyte ratio allowable for downloads (null = disabled).",
    )


class PandaProviderLocationsSettings(GenericLocationsSettings):
    hath_dl_folder: str = Field(
        default="galleries/hath_dls",
        description="Relative subdirectory under media_root where H@H downloads are stored.",
    )
    local_hath_folder: str = Field(
        default="/home/panda/hh/download",
        description="Local filesystem directory where local H@H client stores completed files.",
    )
    remote_hath_dir: str = Field(
        default="/hh/download",
        description="Directory on remote FTPS server where H@H downloads are located.",
    )


class PandaProviderApiSettings(BaseConfigModel):
    concurrent_limit: int = Field(
        default=4,
        description="Maximum concurrent API calls made to E-Hentai/ExHentai API (recommended 4-5).",
    )
    wait_limit: float = Field(
        default=5.5,
        description="Minimum seconds to wait between API calls to prevent rate limits (recommended 5.0+).",
    )


class PandaProviderCookiesSettings(BaseConfigModel):
    ipb_member_id: str = Field(default="", description="ExHentai member ID cookie ('ipb_member_id').")
    ipb_pass_hash: str = Field(default="", description="ExHentai password hash cookie ('ipb_pass_hash').")
    yay: str = Field(default="", description="ExHentai session cookie ('yay').")
    uconfig: str = Field(default="", description="ExHentai user configuration cookie ('uconfig').")


class PandaProviderConfig(BaseConfigModel):
    """Provider configuration for E-Hentai and ExHentai."""

    general: PandaProviderGeneralSettings = Field(default_factory=PandaProviderGeneralSettings)
    locations: PandaProviderLocationsSettings = Field(default_factory=PandaProviderLocationsSettings)
    api: PandaProviderApiSettings = Field(default_factory=PandaProviderApiSettings)
    cookies: PandaProviderCookiesSettings | dict[str, str] = Field(default_factory=PandaProviderCookiesSettings)


class FakkuProviderGeneralSettings(BaseProviderGeneralSettings):
    get_posted_date_from_feed: bool = Field(default=False, description="Extract posted date from RSS feed.")
    get_posted_date_from_wayback: bool = Field(
        default=False, description="Query Wayback Machine for historical release dates."
    )


class FakkuProviderConfig(BaseConfigModel):
    """Provider configuration for FAKKU."""

    general: FakkuProviderGeneralSettings = Field(default_factory=FakkuProviderGeneralSettings)
    cookies: dict[str, str] = Field(
        default_factory=dict,
        description="Session cookies (e.g. 'fakku_sid') for fetching subscriber and controversial galleries.",
    )


class MugimugiProviderGeneralSettings(BaseProviderGeneralSettings):
    api_key: str = Field(default="", description="Mugimugi API key for metadata lookups.")


class MugimugiProviderWantedSettings(BaseConfigModel):
    unwanted_title: str = Field(default="", description="Title text filter for auto-wanted galleries.")
    regexp_unwanted_title: str | bool = Field(default="", description="Regex pattern for unwanted titles.")
    regexp_unwanted_title_icase: str | bool = Field(
        default="", description="Case-insensitive regex for unwanted titles."
    )


class MugimugiProviderConfig(BaseConfigModel):
    """Provider configuration for Mugimugi."""

    general: MugimugiProviderGeneralSettings = Field(default_factory=MugimugiProviderGeneralSettings)
    wanted: MugimugiProviderWantedSettings = Field(default_factory=MugimugiProviderWantedSettings)


class NhentaiProviderCookiesSettings(BaseConfigModel):
    sessionid: str = Field(default="", description="NHentai sessionid cookie required for torrent downloads.")


class NhentaiProviderConfig(BaseConfigModel):
    """Provider configuration for NHentai."""

    cookies: NhentaiProviderCookiesSettings | dict[str, str] = Field(default_factory=NhentaiProviderCookiesSettings)
    locations: GenericLocationsSettings = Field(default_factory=GenericLocationsSettings)


class ChaikaProviderGeneralSettings(BaseProviderGeneralSettings):
    url: str = Field(default="https://panda.chaika.moe", description="Base URL for Chaika provider.")
    metadata_url: str = Field(default="https://panda.chaika.moe", description="Metadata API URL for Chaika.")
    feed_url: str = Field(default="https://panda.chaika.moe/feed/", description="RSS feed URL for latest additions.")


class ChaikaProviderConfig(BaseConfigModel):
    """Provider configuration for Chaika.moe."""

    general: ChaikaProviderGeneralSettings = Field(default_factory=ChaikaProviderGeneralSettings)
    locations: GenericLocationsSettings = Field(default_factory=GenericLocationsSettings)


class MegaProviderGeneralSettings(BaseProviderGeneralSettings):
    megadl_executable_path: str = Field(default="", description="Custom executable path to megatools/megadl binary.")
    megadl_executable_name: str = Field(default="megatools", description="Executable name for Mega downloads.")
    extra_megadl_arguments: str = Field(
        default="", description="Pipe-separated ('|') additional arguments for megatools."
    )


class MegaProviderConfig(BaseConfigModel):
    """Provider configuration for Mega.nz archives."""

    general: MegaProviderGeneralSettings = Field(default_factory=MegaProviderGeneralSettings)
    locations: GenericLocationsSettings = Field(default_factory=GenericLocationsSettings)


class TwitterProviderGeneralSettings(BaseProviderGeneralSettings):
    token: str = Field(default="", description="Twitter/X OAuth access token.")
    token_secret: str = Field(default="", description="Twitter/X OAuth token secret.")
    consumer_key: str = Field(default="", description="Twitter/X consumer API key.")
    consumer_secret: str = Field(default="", description="Twitter/X consumer API secret.")


class TwitterProviderWantedSettings(BaseConfigModel):
    add_as_public: bool = Field(default=False, description="Add Twitter-discovered galleries as public entries.")
    unwanted_title: str = Field(default="", description="Text filter for unwanted titles in Twitter scrapes.")
    regexp_unwanted_title: bool = Field(default=False, description="Treat unwanted_title as a regex.")
    regexp_unwanted_title_icase: bool = Field(default=False, description="Case-insensitive regex for unwanted_title.")


class TwitterProviderConfig(BaseConfigModel):
    """Provider configuration for Twitter/X integration."""

    general: TwitterProviderGeneralSettings = Field(default_factory=TwitterProviderGeneralSettings)
    wanted: TwitterProviderWantedSettings = Field(default_factory=TwitterProviderWantedSettings)
    enabled_handles: list[str] = Field(
        default_factory=list, description="Twitter usernames (@handles) to crawl and archive."
    )


class GenericProviderConfig(BaseConfigModel):
    """Provider configuration for generic/fallback downloads."""

    locations: GenericLocationsSettings = Field(default_factory=GenericLocationsSettings)


class BaseProviderConfig(BaseConfigModel):
    """Base provider configuration for standard providers without custom extra sections."""

    cookies: dict[str, Any] = Field(default_factory=dict, description="Session cookies for provider.")
    headers: dict[str, Any] = Field(default_factory=dict, description="Custom HTTP headers for provider requests.")
    general: BaseProviderGeneralSettings = Field(default_factory=BaseProviderGeneralSettings)
    locations: GenericLocationsSettings = Field(default_factory=GenericLocationsSettings)


class ProvidersConfig(BaseConfigModel):
    """Per-provider specific configuration mappings."""

    fakku: FakkuProviderConfig = Field(default_factory=FakkuProviderConfig)
    mugimugi: MugimugiProviderConfig = Field(default_factory=MugimugiProviderConfig)
    nhentai: NhentaiProviderConfig = Field(default_factory=NhentaiProviderConfig)
    panda: PandaProviderConfig = Field(default_factory=PandaProviderConfig)
    chaika: ChaikaProviderConfig = Field(default_factory=ChaikaProviderConfig)
    mega: MegaProviderConfig = Field(default_factory=MegaProviderConfig)
    twitter: TwitterProviderConfig = Field(default_factory=TwitterProviderConfig)
    nyaa: GenericProviderConfig = Field(default_factory=GenericProviderConfig)
    nexus: GenericProviderConfig = Field(default_factory=GenericProviderConfig)
    generic: GenericProviderConfig = Field(default_factory=GenericProviderConfig)
    irodori: BaseProviderConfig = Field(default_factory=BaseProviderConfig)
    j18: BaseProviderConfig = Field(default_factory=BaseProviderConfig)
    twodmarket: BaseProviderConfig = Field(default_factory=BaseProviderConfig)
    wanimagazine: BaseProviderConfig = Field(default_factory=BaseProviderConfig)


# ============================================================================
# Root Configuration Model
# ============================================================================


class PandaGalleryConfig(BaseConfigModel):
    """Root configuration model representing the entire Panda Backup settings file (settings.yaml / default.yaml)."""

    requests_headers: dict[str, Any] = Field(
        default_factory=lambda: {
            "user-agent": "Mozilla/5.0 (Windows NT 6.3; WOW64; rv:38.0) Gecko/20100101 Firefox/38.0"
        },
        description="Custom HTTP request headers applied globally across all outgoing scraper requests.",
    )
    general: GeneralSettings = Field(
        default_factory=GeneralSettings,
        description="General configuration options for database, workers, rate limits, and download handling.",
    )
    cloning_image_tool: CloningImageToolSettings = Field(
        default_factory=CloningImageToolSettings,
        description="Tool configuration for external image post-processing during archive cloning.",
    )
    allowed: AllowedSettings = Field(
        default_factory=AllowedSettings,
        description="Operational permission flags for redownloads, metadata overwrites, and conversions.",
    )
    matchers: dict[str, int] = Field(
        default_factory=lambda: {
            "panda_title": -2,
            "panda_cover": -1,
            "panda_image": -1,
            "chaika_hash": 1,
            "chaika_main_title": 2,
            "chaika_main_clean_title": 3,
            "chaika_meta_title": 4,
            "chaika_size": 5,
        },
        description="Priority mapping of matchers (negative values disable matcher; positive values define priority order ascending).",
    )
    downloaders: dict[str, int] = Field(
        default_factory=lambda: {
            "chaika_archive": 5,
            "panda_archive": 4,
            "panda_torrent": 3,
            "panda_hath": -1,
            "panda_info": -1,
        },
        description="Priority mapping of downloaders (negative values disable downloader; positive values define priority order ascending).",
    )
    custom_downloaders: dict[str, Any] = Field(
        default_factory=dict,
        description="Custom virtual downloaders chaining multiple downloaders with custom priorities.",
    )
    auto_wanted: AutoWantedSettings = Field(
        default_factory=AutoWantedSettings,
        description="Background wanted gallery discovery scheduler.",
    )
    pushover: PushoverSettings = Field(
        default_factory=PushoverSettings,
        description="Mobile push notifications via Pushover.",
    )
    backlog_search: BacklogSearchSettings = Field(
        default_factory=BacklogSearchSettings,
        description="Search feeds to periodically query for historical missing works.",
    )
    mail_logging: MailSettings = Field(
        default_factory=MailSettings,
        description="SMTP error alerts and submission email notifications.",
    )
    elasticsearch: ElasticSearchSettings = Field(
        default_factory=ElasticSearchSettings,
        description="Elasticsearch 8.x search cluster integration.",
    )
    gallery_dl: GalleryDLSettings = Field(
        default_factory=GalleryDLSettings,
        description="External gallery-dl downloader configuration.",
    )
    autoupdater: AutoUpdaterSettings = Field(
        default_factory=AutoUpdaterSettings,
        description="Automatic metadata updater for existing archives.",
    )
    match_params: MatchParamsSettings = Field(
        default_factory=MatchParamsSettings,
        description="Matching engine settings and CRC32 duplicate handling.",
    )
    locations: LocationsSettings = Field(
        default_factory=LocationsSettings,
        description="Filesystem root storage directories and download subfolders.",
    )
    database: DatabaseSettings = Field(
        default_factory=DatabaseSettings,
        description="Database connection credentials for PostgreSQL.",
    )
    torrent: TorrentSettings = Field(
        default_factory=TorrentSettings,
        description="Torrent client RPC connection details (Transmission / qBittorrent).",
    )
    ftps: FtpsSettings = Field(
        default_factory=FtpsSettings,
        description="Remote FTPS seedbox connection details.",
    )
    ftp_configs: dict[str, Any] = Field(
        default_factory=dict,
        description="Named FTP configuration dictionaries referenced by download_ftp_torrent / download_ftp_hath.",
    )
    webserver: WebServerSettings = Field(
        default_factory=WebServerSettings,
        description="WSGI web server configuration (Gunicorn / CherryPy).",
    )
    urls: UrlSettings = Field(
        default_factory=UrlSettings,
        description="URL prefixes, proxy headers, CORS, and public endpoints.",
    )
    remote_site: RemoteSiteSettings = Field(
        default_factory=RemoteSiteSettings,
        description="Synchronization credentials for remote Panda Backup instances.",
    )
    monitored_links: MonitoredLinksSettings = Field(
        default_factory=MonitoredLinksSettings,
        description="Monitored links polling subsystem control.",
    )
    archive_download_queue: ArchiveDownloadQueueSettings | None = Field(
        default_factory=ArchiveDownloadQueueSettings,
        description="Background archive download queue scheduler settings.",
    )
    experimental: dict[str, Any] = Field(
        default_factory=dict,
        description="Experimental features and developer settings.",
    )
    providers: ProvidersConfig = Field(
        default_factory=ProvidersConfig,
        description="Per-provider specific configuration sections.",
    )


# ============================================================================
# Helpers: Schema Generation & Default YAML Generation
# ============================================================================


def get_config_schema() -> dict[str, Any]:
    """Generate and return the full JSON Schema dictionary for the Panda Backup configuration."""
    return PandaGalleryConfig.model_json_schema()


def generate_default_yaml_content() -> str:
    """Generate a comprehensive, fully-commented default.yaml configuration file

    string documenting all global and per-provider parameters, their descriptions,
    allowed values, and defaults.
    """
    return """# ==============================================================================
# Panda Backup (Panda Gallery) — Default Configuration File
#
# This file defines all system, database, worker, network, security, and
# per-provider configuration settings.
#
# Schema validation: Managed by Pydantic configuration models in
# `core/base/config_models.py`.
# ==============================================================================

# Global HTTP Request Headers
# Mask every outgoing web request with custom headers (e.g. User-Agent).
requests_headers:
  user-agent: "Mozilla/5.0 (Windows NT 6.3; WOW64; rv:38.0) Gecko/20100101 Firefox/38.0"

# ------------------------------------------------------------------------------
# General Settings
# Core system options, database engine, rate limits, and worker lifecycle.
# ------------------------------------------------------------------------------
general:
  # Database backend engine to use. Supported options: 'postgresql'.
  # Note: SQLite and MySQL are unsupported.
  # Default: postgresql
  db_engine: postgresql

  # File glob filters used when scanning local directories for new archives.
  # Allows accepting alternative ZIP extensions (e.g. CBZ).
  # Default: ['*.zip']
  filename_filter:
    - '*.zip'

  # Django secret key used for cryptographic signing, cookies, and tokens.
  # IMPORTANT: Change this to a secure random string in production! Do not use '%' here.
  django_secret_key: "89!yi9gvd2r*m5rq9y-elj86u*zg@-j3ce90@^7+0(-!fkgpzd"

  # Enable Django debug mode for detailed error stack traces. Must be false in production.
  # Default: false
  django_debug_mode: false

  # How finished downloads are handled:
  #   - 'local': files reside on the same machine.
  #   - 'remote': files are downloaded on a remote seedbox/server and retrieved via FTPS.
  #   - 'local_copy': files are copied from the client download folder to media storage.
  #   - 'local_move': files are moved directly to media storage.
  #   - 'local_hardlink': files are hardlinked (requires same filesystem).
  # Default: local
  download_handler: local

  # Handler override for torrent downloads (empty string uses download_handler).
  # Default: ''
  download_handler_torrent: ''

  # Handler override for H@H downloads (empty string uses download_handler).
  # Default: ''
  download_handler_hath: ''

  # Named FTP config key from 'ftp_configs' used for torrent transfers when handler is 'remote'.
  # Default: default
  download_ftp_torrent: default

  # Named FTP config key from 'ftp_configs' used for H@H transfers when handler is 'remote'.
  # Default: default
  download_ftp_hath: default

  # Custom directory path for temporary extraction and download staging.
  # If null or empty, the system temporary directory (/tmp) is used.
  # Default: null
  temp_directory_path: null

  # Rate-limiting wait timer (in seconds) between web requests across scrapers.
  # Values below 5.0 seconds risk IP bans from upstream providers.
  # Default: 6.0
  wait_timer: 6.0

  # Timeout (in seconds) for outgoing HTTP requests before timing out.
  # Default: 25
  timeout_timer: 25

  # Start the timed downloader background worker thread automatically on startup.
  # Default: false
  timed_downloader_startup: false

  # Cycle interval (in seconds) for the timed downloader background worker.
  # Default: 5.0
  timed_downloader_cycle_timer: 5.0

  # Number of parallel post-downloader workers (each handling an independent FTP connection).
  # Default: 4
  parallel_post_downloaders: 4

  # Maximum number of concurrent background HTTP archive downloads across providers.
  # Set to 0 to download archives synchronously inline (legacy behavior).
  # Set to 1 or more to enable the background archive download queue.
  # Default: 1
  parallel_archive_downloaders: 1

  # Explicit boolean toggle to queue archive HTTP downloads in the background queue.
  # Default: true
  queue_archive_downloads: true

  # Start the archive download queue scheduler on worker runner launch.
  # Default: true
  archive_download_queue_startup: true

  # Cycle interval (in seconds) for the archive download queue scheduler loop.
  # Default: 2.0
  archive_download_queue_cycle_timer: 2.0

  # Stall/inactivity timeout (in seconds) for queued HTTP archive downloads.
  # Slow downloads making steady progress continue without interruption; stalled
  # downloads with no progress for this duration are aborted and retried/failed.
  # Set to 0 to disable.
  # Default: 180.0
  archive_download_stall_timeout: 180.0

  # Stall/inactivity timeout (in seconds) for background TaskQueue jobs in WorkerRunner.
  # Set to 0 to disable.
  # Default: 1800.0
  worker_task_stall_timeout: 1800.0

  # Start the download progress checker scheduler on worker startup.
  # Default: true
  download_progress_checker_startup: true

  # Polling interval (in seconds) for checking torrent and H@H download status.
  # Default: 10.0
  download_progress_checker_cycle_timer: 10.0

  # Reload the web server when Python file modifications are detected (CherryPy engine only).
  # Default: false
  cherrypy_auto_restart: false

  # Automatically discard/skip galleries containing these tags during crawling and matching.
  # Default: filtered languages
  banned_tags:
    - language:thai
    - language:chinese
    - language:korean
    - language:french
    - language:spanish
    - language:italian
    - language:hungarian
    - language:portuguese
    - language:polish
    - language:russian
    - language:vietnamese

  # Automatically discard/skip galleries uploaded by these usernames during crawling.
  # Default: []
  banned_uploaders:
    - random_guy

  # Elasticsearch DSL queries to discard matching galleries from ingestion.
  # Default: []
  banned_search_queries:
    - 'tags.full:"language:chinese" AND (NOT tags.full:"language:japanese")'

  # Automatically mark all newly added galleries as public (visible to anonymous visitors).
  # Default: false
  add_as_public: false

  # Automatically generate review marks in the Mark system when adding an archive similar to an existing one.
  # Default: false
  mark_similar_new_archives: false

  # Compute image SHA1 and CRC32 checksums for every page when adding new archives.
  # Default: false
  auto_hash_images: false

  # Compute perceptual hashes (pHash) on pages when adding new archives to support visual matching.
  # Default: false
  auto_phash_images: false

  # Number of worker processes in the process pool for CPU-bound tasks
  # (pHash calculation, image resizing, zip CRC recalculation).
  # 0 auto-selects half of available CPU cores.
  # Default: 0
  process_pool_workers: 0

  # Automatically match wanted images against newly added archives (requires OpenCV and NumPy).
  # Default: false
  auto_match_wanted_images: false

  # Automatically correlate perceptual hash tags across similar archives.
  # Default: false
  auto_match_phash_tags_archives: false

  # Default publisher name applied when creating new wanted gallery entries.
  # Default: ''
  default_wanted_publisher: ''

  # Default categories selected when generating or parsing wanted galleries.
  # Default: ['Doujinshi', 'Artist CG']
  default_wanted_categories:
    - Doujinshi
    - Artist CG

  # Upstream providers queried for wanted galleries.
  # Default: ['panda']
  default_wanted_providers:
    - panda

  # Create review marks for archives when metadata updates invalidate a previously accepted WantedGallery.
  # Default: false
  recheck_wanted_on_update: false

  # Maximum width (in pixels) for vertical portrait pages during reduced/resized extraction.
  # Default: 900
  vertical_image_max_width: 900

  # Maximum width (in pixels) for horizontal landscape pages during reduced/resized extraction.
  # Default: 1500
  horizontal_image_max_width: 1500

  # Minimum logging level sent to log files and database ('DEBUG', 'INFO', 'WARNING', 'ERROR').
  # Default: INFO
  force_log_level: INFO

  # Suppress verbose Django SQL query logs in debug mode.
  # Default: false
  disable_sql_log: false

  # Maximum age before auto-removing completed torrents from the torrent client (0 keeps indefinitely).
  # Default: 0
  max_torrent_lifespan: 0

  # Time unit for max_torrent_lifespan ('minutes', 'hours', 'days', 'weeks').
  # Default: days
  max_torrent_lifespan_type: days

  # When crawling revision chains, treat non-latest/superseded gallery links as deleted.
  # Default: false
  non_current_links_as_deleted: false

  # Automatically move downloaded archives that fail integrity or size checks to the recycle bin.
  # Default: false
  recycle_failed_checks_downloads: false

  # Automatically re-enqueue failed downloads (e.g. corrupt zip, size mismatch) with an alternate downloader.
  # Default: true
  requeue_failed_downloads: true

  # Downloader or custom downloader to use when requeueing a failed download.
  # Default: panda_archive
  failed_download_downloader: panda_archive

# ------------------------------------------------------------------------------
# Operational Permissions ('allowed')
# Controls whether existing archives can be replaced, redownloaded, or converted.
# ------------------------------------------------------------------------------
allowed:
  # Allow overwriting existing gallery metadata with upstream data when re-crawling an already added gallery.
  # Default: false
  replace_metadata: false

  # Allow redownloading archives that have already been downloaded previously.
  # WARNING: if link_monitor is enabled, this will repeatedly redownload existing files.
  # Default: false
  redownload: false

  # For providers supporting gallery relationships, automatically fetch and download child/parent revisions.
  # Default: false
  auto_download_nested: false

  # Allow retrying downloads for galleries previously marked with a failed download status.
  # Default: false
  retry_failed: false

  # After finding an upstream non-match, attempt matching against local database archives immediately.
  # Default: false
  internal_matches_for_non_matches: false

  # Automatically transform RAR (.rar) and 7-Zip (.7z) archives into standard ZIP files upon completion.
  # Default: true
  convert_others_to_zip: true

  # Mark parent/historical links as deleted when newer revisions exist.
  # Default: false
  non_current_links_as_deleted: false

# ------------------------------------------------------------------------------
# External Image Cloning Tool
# Optional command-line tool executed on pages when using the clone-plus feature.
# ------------------------------------------------------------------------------
cloning_image_tool:
  # Enable external image tool execution.
  # Default: false
  enable: false

  # Display name of the tool (e.g. 'optipng', 'waifu2x').
  # Default: ''
  name: ''

  # Executable command path. Supports '{input}' and '{output}' runtime template placeholders.
  # Default: ''
  executable_path: ''

  # Description of the tool's purpose.
  # Default: ''
  description: ''

  # File glob patterns to pass to the tool.
  # Default: ['*.png']
  file_filters:
    - '*.png'

  # Additional CLI arguments passed to the executable.
  # Default: []
  extra_arguments: []

# ------------------------------------------------------------------------------
# Matchers & Downloaders Priorities
# Positive integers define execution order ascending (1 runs before 2).
# Negative integers disable that specific matcher or downloader.
# ------------------------------------------------------------------------------
matchers:
  panda_title: -2
  panda_cover: -1
  panda_image: -1
  chaika_hash: 1
  chaika_main_title: 2
  chaika_main_clean_title: 3
  chaika_meta_title: 4
  chaika_size: 5

downloaders:
  chaika_archive: 5
  panda_archive: 4
  panda_torrent: 3
  panda_hath: -1
  panda_info: -1

# Custom virtual downloaders combining multiple downloaders with fallback priorities.
# custom_downloaders:
#   panda_archive_then_hath:
#     - panda_archive: 1
#     - panda_hath: 2

# ------------------------------------------------------------------------------
# Auto Wanted Generator
# Automatically queries providers to create WantedGallery entries for missing works.
# ------------------------------------------------------------------------------
auto_wanted:
  # Enable the auto-wanted generator background scheduler.
  # Default: true
  enable: true

  # Start the auto-wanted scheduler upon worker runner startup.
  # Default: false
  startup: false

  # Cycle interval (in seconds) between auto-wanted runs.
  # Default: 4.0
  cycle_timer: 4.0

  # Providers queried by the auto-wanted generator.
  # Default: ['twitter']
  providers:
    - twitter

  # Text filter automatically added to the unwanted_title field on generated wanted galleries.
  # Default: '[Chinese]'
  unwanted_title: '[Chinese]'

  # Treat unwanted_title as a regular expression.
  # Default: false
  regexp_unwanted_title: false

  # Case-insensitive matching for unwanted_title regex.
  # Default: false
  regexp_unwanted_title_icase: false

# ------------------------------------------------------------------------------
# Pushover Notifications
# Mobile push alerts when wanted galleries are located or system events occur.
# ------------------------------------------------------------------------------
pushover:
  # Enable Pushover notifications.
  # Default: false
  enable: false

  # Pushover user or group key.
  user_key: ''

  # Pushover application API token.
  token: ''

  # Specific device name to receive notifications (blank sends to all devices).
  device: ''

  # Notification alert sound (e.g. 'pushover', 'bike', 'siren'). Blank uses account default.
  sound: ''

# ------------------------------------------------------------------------------
# Backlog Search Crawler
# Periodic search feeds scanned to discover historical works.
# ------------------------------------------------------------------------------
backlog_search:
  # Enable the backlog search crawler.
  # Default: false
  enable: false

  # Feed URLs and query endpoints to scan.
  urls:
    - name: panda_backlog
      url: "https://e-hentai.org/?f_doujinshi=1&f_manga=1&f_artistcg=1&f_gamecg=1&f_western=0&f_non-h=1&f_imageset=1&f_cosplay=0&f_asianporn=0&f_misc=0&f_apply=Apply+Filter&f_search="
    - name: chaika_backlog
      url: "https://panda.chaika.moe/meta/es-gallery-json/?sort=posted_date&order=desc&q="

# ------------------------------------------------------------------------------
# Email Alerts (Mail Logging)
# SMTP error alerts and submission notifications.
# ------------------------------------------------------------------------------
mail_logging:
  # Enable email error notifications.
  # Default: false
  enable: false

  # SMTP server host and optional port (e.g. 'smtp.example.com:587').
  mailhost: ''

  # Sender email address.
  from: ''

  # Recipient email address for system alerts.
  to: ''

  # Subject line for error emails.
  # Default: 'CherryPy Error'
  subject: 'CherryPy Error'

  # SMTP authentication username.
  username: ''

  # SMTP authentication password.
  password: ''

# ------------------------------------------------------------------------------
# Elasticsearch 8.x Integration
# Full-text archive search, autocomplete, and title similarity matching.
# ------------------------------------------------------------------------------
elasticsearch:
  # Enable Elasticsearch search integration.
  # Default: false
  enable: false

  # Enable dedicated similarity matching index.
  # Default: false
  enable_match: false

  # Elasticsearch cluster HTTP base URL.
  # Default: 'http://localhost:9200/'
  url: "http://localhost:9200/"

  # Maximum result window limit for deep pagination (index.max_result_window).
  # Default: 10000
  max_result_window: 10000

  # Automatically force index refresh after indexing archives (leave false in production).
  # Default: false
  auto_refresh: false

  # Automatically force index refresh after indexing upstream galleries.
  # Default: false
  auto_refresh_gallery: false

  # Main Elasticsearch index name for local archives.
  # Default: 'viewer'
  index_name: viewer

  # Elasticsearch index name for upstream gallery metadata.
  # Default: 'viewer_gallery'
  gallery_index_name: viewer_gallery

  # Elasticsearch index name for fast title similarity matching.
  # Default: 'viewer_match'
  match_index_name: viewer_match

  # Only index public archives, hiding private archives from search.
  # Default: false
  only_index_public: false

  # Request timeout (in seconds) for Elasticsearch queries.
  # Default: 20
  timeout: 20

# ------------------------------------------------------------------------------
# External Downloader (gallery-dl)
# External downloader integration for expanded site support.
# ------------------------------------------------------------------------------
gallery_dl:
  # gallery-dl binary name.
  # Default: 'gallery-dl'
  executable_name: gallery-dl

  # Custom path to gallery-dl binary (leave blank to resolve from system PATH).
  # Default: ''
  executable_path: ''

  # Path to gallery-dl configuration file (JSON/YAML).
  # Default: ''
  config_file: ''

  # Additional CLI arguments passed to gallery-dl.
  # Default: ''
  extra_arguments: ''

# ------------------------------------------------------------------------------
# Metadata Auto-Updater
# Periodically queries upstream providers to update tags, ratings, and titles.
# ------------------------------------------------------------------------------
autoupdater:
  # Enable automatic metadata updating.
  # Default: false
  enable: false

  # Start the autoupdater on worker runner launch.
  # Default: false
  startup: false

  # List of providers enabled for metadata updating.
  # Default: ['panda']
  providers:
    - panda

  # Cycle interval (in seconds) between update checks.
  # Default: 2.0
  cycle_timer: 2.0

  # Number of days in the past from current date to filter galleries for updating.
  # Default: 8
  buffer_back: 8

  # Number of days after/offset to filter galleries for updating.
  # Default: 0
  buffer_after: 0

# ------------------------------------------------------------------------------
# Matching Parameters
# Settings for matching local archives against upstream provider galleries.
# ------------------------------------------------------------------------------
match_params:
  # Re-evaluate archives that already have a match entry if their current match type is in rematch_file_list.
  # Default: false
  rematch_file: false

  # List of match_types eligible for rematching.
  # Default: ['non-match']
  rematch_file_list:
    - non-match

  # Recompute CRC32 checksums for local archives during matching.
  # Default: false
  rehash_files: false

  # If an archive with the same CRC32 checksum is found, copy its matching metadata directly.
  # Default: true
  copy_match_file: true

# ------------------------------------------------------------------------------
# Filesystem Storage Locations
# Absolute directories for media/static files and relative subfolders for downloads.
# ------------------------------------------------------------------------------
locations:
  # Absolute filesystem directory where media (archives, extracted pages, covers) is stored.
  # If left empty, MEDIA_ROOT defaults to '' unless specified via environment variable.
  # Default: ''
  media_root: ''

  # Absolute filesystem directory where Django collects static files.
  # If left empty, Django automatically defaults to '<BASE_DIR>/static/'.
  # Default: ''
  static_root: ''

  # Relative subdirectory under media_root where direct archive HTTP downloads are stored.
  # Default: galleries/archive_dls
  archive_dl_folder: galleries/archive_dls

  # Relative subdirectory under media_root where completed torrent downloads are placed.
  # Default: galleries/torrent_dls
  torrent_dl_folder: galleries/torrent_dls

  # Custom absolute path for the application log file. If blank or commented, defaults to '<config_dir>/viewer.log'.
  # log_location: /home/panda/crawler.log

# ------------------------------------------------------------------------------
# Database Settings
# Connection parameters for PostgreSQL.
# ------------------------------------------------------------------------------
database:
  # PostgreSQL backend credentials (used when db_engine is 'postgresql')
  postgresql_name: pandaweb
  postgresql_user: pandaadmin
  postgresql_password: pandaadmin
  postgresql_host: localhost
  postgresql_port: '5432'

# ------------------------------------------------------------------------------
# Torrent Client Integration
# Connection settings for Transmission or qBittorrent WebUI.
# ------------------------------------------------------------------------------
torrent:
  # Torrent client engine: 'transmission' or 'qbittorrent'.
  # Default: transmission
  client: transmission

  # WebUI / RPC authentication username.
  # Default: panda
  user: panda

  # WebUI / RPC authentication password.
  # Default: panda
  pass: panda

  # RPC endpoint URL (Transmission: 'http://localhost/transmission/rpc', qBittorrent: 'http://localhost:8080/').
  # Default: 'http://localhost/transmission/rpc'
  address: "http://localhost/transmission/rpc"

  # WebUI / RPC port.
  # Default: 9091
  port: 9091

  # Directory path where the torrent client saves completed downloads.
  # Default: /home/panda/downloads/torrents
  download_dir: /home/panda/downloads/torrents

  # Skip SSL/TLS certificate validation (useful for self-signed certificates).
  # Default: false
  no_certificate_check: false

# ------------------------------------------------------------------------------
# Remote FTPS Configuration
# Used when download_handler is 'remote' to transfer files from a seedbox.
# ------------------------------------------------------------------------------
ftps:
  # FTPS authentication username.
  user: panda

  # FTPS authentication password.
  passwd: panda

  # FTPS server address (hostname or IP).
  address: remote.server.com

  # Directory on the remote server where completed torrent downloads are located.
  remote_torrent_dir: /home/pandaremote/downloads/torrent

  # Optional local network interface IP to bind to when opening FTPS connections.
  # bind_host: 192.168.1.200

  # Optional local port to bind to when opening FTPS connections (0 for ephemeral).
  # bind_port: 0

  # Skip SSL/TLS certificate verification on remote FTPS server.
  # Default: false
  no_certificate_check: false

# ------------------------------------------------------------------------------
# Web Server Configuration
# WSGI web server hosting options for Gunicorn (production) or CherryPy.
# ------------------------------------------------------------------------------
webserver:
  # WSGI server engine: 'gunicorn' (recommended for production) or 'cherrypy'.
  # Default: gunicorn
  backend: gunicorn

  # IP address for the web server to listen on ('127.0.0.1' for proxy, '0.0.0.0' for all interfaces).
  # Default: 0.0.0.0
  bind_address: 0.0.0.0

  # TCP listening port.
  # Default: 8090
  bind_port: 8090

  # UNIX domain socket file path to bind to instead of a TCP port (e.g. '/tmp/panda.sock').
  # Default: null
  socket_file: null

  # Enable direct HTTPS/SSL termination in the web server.
  # Default: false
  enable_ssl: false

  # Path to SSL certificate (.crt or .pem file), required if enable_ssl is true.
  ssl_certificate: /home/panda/certs/panda.crt

  # Path to SSL private key (.key file), required if enable_ssl is true.
  ssl_private_key: /home/panda/certs/panda.key

  # Write HTTP access logs to file.
  # Default: false
  write_access_log: false

  # Output web server logs to console stdout.
  # Default: false
  log_to_screen: false

  # Socket listen backlog queue size for incoming TCP connections.
  # Default: 5
  socket_queue_size: 5

  # Thread pool size for handling concurrent requests (CherryPy only).
  # Default: 10
  thread_pool: 10

  # Number of worker processes (Gunicorn only).
  # Default: 4
  workers: 4

  # Number of worker threads per process (Gunicorn only).
  # Default: 1
  threads: 1

  # Worker request timeout in seconds before restarting unresponsive workers (Gunicorn only).
  # Default: 60
  timeout: 60

# ------------------------------------------------------------------------------
# URL Routing, Reverse Proxy & Security Headers
# ------------------------------------------------------------------------------
urls:
  # Base URL prefix for serving user media and archives.
  # Default: /media/
  media_url: /media/

  # Base URL prefix for serving static CSS and JavaScript assets.
  # Default: /static/
  static_url: /static/

  # Custom root URL prefix for the viewer web application.
  # Default: ''
  viewer_main_url: ''

  # Set to true if running behind a reverse proxy (Nginx, Traefik, Caddy) to fix absolute URL generation.
  # Default: false
  behind_proxy: false

  # Enable the /submit URL for unauthenticated public visitors to submit gallery URLs.
  # Default: false
  enable_public_submit: false

  # Enable public access to the global repository statistics page.
  # Default: false
  enable_public_stats: false

  # Enable public display of similarity marks on archive detail pages.
  # Default: false
  enable_public_marks: false

  # Mark reason identifiers visible to public viewers when enable_public_marks is true.
  # Default: ['images_sha1_similarity', 'images_phash_similarity']
  public_mark_reasons:
    - images_sha1_similarity
    - images_phash_similarity

  # Base URL of an external CDN or media server used to offload heavy archive downloads.
  # Default: ''
  external_media_server: ''

  # Direct primary download links to the external media server URL instead of the local server.
  # Default: false
  external_as_main_download: false

  # Canonical public-facing URL (scheme + domain) used to construct absolute URLs in background tasks.
  # Default: ''
  main_webserver_url: ''

  # Route standard search queries directly to Elasticsearch endpoints.
  # Default: false
  elasticsearch_as_main_urls: false

  # Elasticsearch archive query endpoint name.
  # Default: es-index
  elasticsearch_archive_url: es-index

  # Elasticsearch upstream gallery query endpoint name.
  # Default: es-gallery-index
  elasticsearch_gallery_url: es-gallery-index

  # List of allowed origin domains for Cross-Origin Resource Sharing (CORS).
  # Default: []
  cors_allowed_origins: []

  # Allow all origins to access API endpoints (Access-Control-Allow-Origin: *).
  # Default: false
  cors_allow_all_origins: false

# ------------------------------------------------------------------------------
# Remote Site Synchronization
# Credentials for synchronizing with another Panda Backup instance.
# ------------------------------------------------------------------------------
remote_site:
  # Remote Panda Backup instance JSON API endpoint URL.
  # Default: 'https://panda.chaika.moe/jsonapi'
  api_url: "https://panda.chaika.moe/jsonapi"

  # Long-lived user authentication token generated on the remote server.
  user_token: asdf1234

  # Directory on remote server where synchronized archives are located.
  # Default: /media/galleries/archives
  remote_folder: /media/galleries/archives

# ------------------------------------------------------------------------------
# Monitored Links Subsystem
# Controls background polling workers tracking new additions on subscribed URLs.
# ------------------------------------------------------------------------------
monitored_links:
  # Enable the monitored links polling worker.
  # Default: false
  enable: false

# ------------------------------------------------------------------------------
# Per-Provider Settings
# Individual configurations, credentials, API limits, and directories per provider.
# All provider settings must be nested under the 'providers' key.
# ------------------------------------------------------------------------------
providers:

  # E-Hentai & ExHentai ('panda')
  panda:
    general:
      # Compare GP cost between H@H and direct archive downloads, proceeding only if the current method has the lowest or equal cost.
      # Default: false
      proceed_with_download_if_lowest_cost: false

      # Maximum page number to crawl when browsing gallery listings.
      # Default: 5
      stop_page_number: 5

      # Accepted categories reported by upstream RSS. Unmatched categories are ignored.
      # Possible categories: Doujinshi, Manga, Artist CG Sets, Game CG Sets, Western, Image Sets, Non-H, Cosplay, Asian Porn, Misc, Private
      accepted_rss_categories:
        - [Doujinshi]
        - [Manga]
        - [Artist CG Sets]
        - [Game CG Sets]
        - [Image Sets]
        - [Non-H]
        - [Cosplay]
        - [Asian Porn]
        - [Misc]
        - [Private]

      # Use ExHentai instead of E-Hentai for region-blocked/fjord galleries.
      # Default: false
      use_ex_for_fjord: false

      # Automatically crawl and download newer versions when crawling gallery chains.
      # Default: false
      auto_process_newer: false

      # Automatically download the first original version in a gallery revision chain.
      # Default: false
      auto_process_first: false

      # Automatically download the direct parent gallery.
      # Default: false
      auto_process_parent: false

      # Create review marks linking discovered parent and child gallery relationships.
      # Default: false
      mark_relationships: false

      # Verify downloaded files against provider file lists upon completion.
      # Default: false
      confirm_files_after_download: false

      # Maximum Gallery Points (GP) allowable for an archive download (0 = free only; null = disabled).
      # Default: null
      maximum_gp_cost_for_dls: null

      # Maximum GP per Megabyte ratio allowable for downloads (null = disabled).
      # Default: null
      maximum_gp_mb_ratio_for_dls: null

    locations:
      # Relative location under media_root where H@H downloads will be stored.
      # Default: galleries/hath_dls
      hath_dl_folder: galleries/hath_dls

      # Local directory where the local H@H client stores completed files (if using local handler).
      # Default: /home/panda/hh/download
      local_hath_folder: /home/panda/hh/download

      # Directory on remote FTPS server where H@H downloads are located (if using remote handler).
      # Default: /hh/download
      remote_hath_dir: /hh/download

      # Provider-specific override for direct archive download storage folder.
      # Default: '' (uses general locations.archive_dl_folder)
      archive_dl_folder: ''

      # Provider-specific override for torrent download storage folder.
      # Default: '' (uses general locations.torrent_dl_folder)
      torrent_dl_folder: ''

    # Upstream API rate limits
    api:
      # Maximum concurrent API calls (upstream recommendation: 4-5).
      # Default: 4
      concurrent_limit: 4

      # Wait time (in seconds) between API calls to prevent IP bans (upstream recommendation: 5.0+).
      # Default: 5.5
      wait_limit: 5.5

    # ExHentai authentication cookies.
    # Log in to ExHentai in your browser, copy cookies from developer tools, and paste values here.
    # 'uconfig' is necessary if you use custom display filters on every request.
    # Archive download on your account must be configured for auto-download / auto-accept.
    cookies:
      ipb_member_id: ''
      ipb_pass_hash: ''
      yay: ''
      uconfig: ''

  # FAKKU ('fakku')
  fakku:
    general:
      # Extract gallery posted date from RSS feed entries.
      # Default: false
      get_posted_date_from_feed: false

      # Query Wayback Machine (Internet Archive) for historical release dates.
      # Default: false
      get_posted_date_from_wayback: false

    # Session cookie for FAKKU. Required for accessing subscription and controversial galleries.
    cookies:
      fakku_sid: ''

  # Mugimugi ('mugimugi')
  mugimugi:
    general:
      # Mugimugi API key for metadata lookups.
      # Default: ''
      api_key: ''

    wanted:
      # Text filter automatically assigned to unwanted_title on generated wanted galleries.
      # Default: ''
      unwanted_title: ''

      # Regular expression pattern to identify unwanted titles.
      # Default: ''
      regexp_unwanted_title: ''

      # Case-insensitive regular expression pattern to identify unwanted titles.
      # Default: ''
      regexp_unwanted_title_icase: ''

  # NHentai ('nhentai')
  nhentai:
    # Session cookie for NHentai ('sessionid'). Required for authenticated torrent downloads.
    cookies:
      sessionid: ''

    locations:
      # Provider-specific torrent download folder override.
      # Default: '' (uses general locations.torrent_dl_folder)
      torrent_dl_folder: ''

  # Chaika.moe ('chaika')
  chaika:
    general:
      # Base URL for the Chaika metadata provider.
      # Default: 'https://panda.chaika.moe'
      url: "https://panda.chaika.moe"

      # Metadata lookup endpoint URL.
      # Default: 'https://panda.chaika.moe'
      metadata_url: "https://panda.chaika.moe"

      # RSS/Atom feed URL for latest gallery additions.
      # Default: 'https://panda.chaika.moe/feed/'
      feed_url: "https://panda.chaika.moe/feed/"

    locations:
      # Provider-specific archive download folder override.
      # Default: '' (uses general locations.archive_dl_folder)
      archive_dl_folder: ''

  # Mega.nz ('mega')
  mega:
    general:
      # Custom path to megatools/megadl binary.
      # Default: ''
      megadl_executable_path: ''

      # Executable name for megatools.
      # Default: megatools
      megadl_executable_name: megatools

      # Pipe-separated ('|') additional CLI arguments passed to megatools.
      # Default: ''
      extra_megadl_arguments: ''

    locations:
      # Provider-specific archive download folder override.
      # Default: '' (uses general locations.archive_dl_folder)
      archive_dl_folder: ''

  # Twitter / X ('twitter')
  twitter:
    general:
      # Twitter OAuth access token.
      # Default: ''
      token: ''

      # Twitter OAuth token secret.
      # Default: ''
      token_secret: ''

      # Twitter consumer API application key.
      # Default: ''
      consumer_key: ''

      # Twitter consumer API application secret.
      # Default: ''
      consumer_secret: ''

    wanted:
      # Mark Twitter-discovered galleries as public.
      # Default: false
      add_as_public: false

      # Title filter for unwanted items.
      # Default: ''
      unwanted_title: ''

      # Treat unwanted_title as a regular expression.
      # Default: false
      regexp_unwanted_title: false

      # Case-insensitive regular expression for unwanted_title.
      # Default: false
      regexp_unwanted_title_icase: false

    # List of Twitter screen names (@handles) to crawl and archive.
    # Default: []
    enabled_handles: []

  # Nyaa ('nyaa')
  nyaa:
    locations:
      # Provider-specific torrent download folder override.
      # Default: '' (uses general locations.torrent_dl_folder)
      torrent_dl_folder: ''

  # Nexus ('nexus')
  nexus:
    locations:
      # Provider-specific archive download folder override.
      # Default: '' (uses general locations.archive_dl_folder)
      archive_dl_folder: ''

  # Generic Provider ('generic')
  generic:
    locations:
      # Provider-specific archive download folder override.
      # Default: ''
      archive_dl_folder: ''

      # Provider-specific torrent download folder override.
      # Default: ''
      torrent_dl_folder: ''

  # Irodori ('irodori')
  irodori:
    general:
      # Politeness wait timer (in seconds) between requests.
      wait_timer: 6
      # Outbound HTTP timeout (in seconds).
      timeout_timer: 25

  # J18 ('j18')
  j18:
    general:
      wait_timer: 6
      timeout_timer: 25

  # 2D Market ('twodmarket')
  twodmarket:
    general:
      wait_timer: 6
      timeout_timer: 25

  # Wani Magazine ('wanimagazine')
  wanimagazine:
    general:
      wait_timer: 6
      timeout_timer: 25
"""


def save_default_yaml_file(path: str = "default.yaml") -> None:
    """Write the documented default configuration content to disk."""
    content = generate_default_yaml_content()
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info(f"Updated default configuration file at {path}")
