# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.2.0] - 2026-11-13

### Added

- **Background Archive File Checks & Picker Widgets**: Replaced the synchronous `/archive-not-present/` view (which now redirects to `/archive-file-check/`) with a background `archive_file_check` worker job.
  - Supports checking all archives, media subfolders, explicit archive IDs/ranges, or filtered archive selections with live progress polling, cancellation, re-runs, and bulk/single issue actions (re-check, recalculate file info, queue download, clear CRC32, mark deleted, delete, dismiss).
  - Added optional ZIP integrity verification when a CRC32 mismatch occurs (enabled by default with CRC32 checks) to distinguish intact ZIPs with stale checksums from corrupted or unreadable archives, with issue filtering by test result.
  - Integrated the `admin-widgets` archive and media folder pickers along with `/api/archive-picker/` and JSON autocomplete endpoints for source, reason, provider, category, and mark reason.
- **Manga Image Contextual Extraction**: Integrated `manga-extractor` for archive and image contextual extraction, supporting configurable profiles, `ocr_space_threshold`, ordered `bubble_transcripts` dialogue lists, bearer `auth_token` configuration (with environment variable fallbacks), and background worker execution.
- **Scoped API Tokens & Expiry Management**: Added `read`, `write`, and `remote` scopes, optional expiration (`expire_date` nullable for non-expiring tokens), throttled `last_used_date` tracking, and per-user unique token names on `UserLongLivedToken`, exposed in the user profile UI, Django admin, and `user-management` CLI (`--token-expiry-days`, `--token-scopes`).
- **Separate Capture & Processing Frequencies for Monitored Links**: Added an opt-in `separate_frequencies` mode with `capture_frequency` on `MonitoredLink` (and `--capture-links-only` / `--skip-link-capture` crawler flags), allowing Panda link capture to queue pending `ProcessedLinks` with status/retry tracking separately from metadata processing and downloads.
- **Background Queues for Archive Operations & Gallery Processing**:
  - Dispatched single-item archive operations (`recalc`, `clone`, `split`, `convert`, `reduce`, `sha1`, `similar`, `matches`, `delete`) to `ArchiveWorker` background tasks so web requests do not time out.
  - Added dedicated `gallery_creator` and `gallery_updater` worker task queues to decouple gallery creation and metadata refreshes (`GalleryMetadataUpdater`) from the web crawler pipeline.
- **Web Crawler Bulk Input & UI Controls**: Added text file upload (`multipart/form-data`), link-group splitting (`split into groups` persisted in local storage), and a `Fast bulk mode (silent processing)` option to `/web-crawler/`, along with standardized summary logging for database checks, metadata evaluations, and discarded gallery counts across providers.
- **Provider Ban Cooldowns**: Added persistent temporary IP ban detection and dynamic cooldown tracking (`ProviderRateLimit.cooldown_until`) for Panda, pausing requests during cooldowns and raising `ProviderBannedError` so worker tasks re-queue with `not_before` without burning attempts.

### Changed

- **Unified Matching Engine & Shared Provider Rate Limiting**: Rebuilt online and offline archive matching (`core/matching`) on the shared provider rate limiter (`<provider>_web` buckets via `request_by_provider`) with deduplicated per-archive requests, `crc32` offline matching, `auto_link` support across views/API/workers, `--match-mode online|offline` in `foldercrawlerrun.py`, and shared rate-limited requests for parsers and wanted generators.
- **Consolidated Download Progress & Post-Download Pipeline**: Merged `TimedPostDownloader` into `DownloadProgressChecker` using a unified `TransferSource` abstraction (local folder / FTPS), running transfers in a bounded background pool (`parallel_post_downloaders`) with delayed retries, storing Transmission info hashes instead of session-scoped numeric IDs, and preventing finished torrents awaiting transfer from being purged by lifespan checks.
- **Database Indexes & ORM Query Optimization**:
  - Added PostgreSQL composite and single-column indexes on `Archive`, `ArchiveTag`, `Gallery`, and `Tag` for search and feed filtering.
  - Optimized search, archive/gallery detail views, `LatestArchivesFeed`, and bulk URL ingestion (`select_related` / `prefetch_related` / deferred text columns, SQL aggregation, avoiding unnecessary `distinct()` on negative-only tag filters, memoized `HistoricalRecords` `prev_record` lookups, and summarized logging for batches > 50 URLs).
- **Viewer UI & Tools Cleanup**:
  - Removed the redundant info badges row from archive and gallery headers.
  - Cleaned up `/tools/` by removing overlapping scheduler/worker controls already present on `/stats/workers/`, removing `-tmd` / "Transfer missing downloads", and changing "Restart Viewer" to "Restart Web Server" (`SIGHUP` to Gunicorn master).
- **Internal Architecture, Tooling & CI**:
  - Removed `Settings.set_models` and migrated `core/`, `workers/`, and `viewer/` to direct `viewer.models` imports.
  - Enabled mypy `possibly-undefined` and Ruff `F401` unused-import checks; fixed test state leaks in worker and parser tests; removed Python 3.13 from the GitLab CI test matrix.
  - Updated dependencies: `django-compressor` to 4.7.0, `psycopg` to 3.3.9, `python-dateutil` to 4.0.0, and `ruff` to 0.17.5.

### Removed

- Removed legacy `TimedPostDownloader` (`timed_downloader_startup` / `timed_downloader_cycle_timer` settings and `post_downloader` scheduler state).
- Removed obsolete matching helpers (`TitleGoogleMatcher`, `match_external`, `match_internal`, `search_for_archives_matches_web`, `match_archives_from_gallery_titles`).
- Removed the deprecated `extract_toggle` web view, URL route, `Archive.extract_toggle` method, and legacy API action (replaced by `extract` and `reduce`).
- Removed the `/ripper/` folder link from the navigation bar.

### Fixed

- **Archive File Check & CRC32 Consistency**:
  - Compared against uncompressed image size inside the ZIP rather than file size on disk when checking archive file sizes.
  - Normalized all stored `Archive.crc32` values to 8-digit uppercase hex (`%08X`) via data migration, `pre_save` signal, and API filter normalization, and compared CRC32 values numerically so legacy unpadded values do not trigger false mismatches or missed matches.
  - Added `dirs_only=1` support and fixed directory prefix boundary checks in `/dir-browser/`.
- **Download & Worker Queue Reliability**:
  - Prevented stalled download threads from corrupting re-queued attempts by using per-attempt `.part` filenames and claim-guarded database/bookkeeping writes.
  - Separated post-download processing from file transfer so post-processing failures do not re-download already transferred archives, and verified downloaded archive sizes against `Content-Length`.
  - Preserved streaming responses and restricted ban-page body inspection in `request_by_provider` to text-like responses so binary downloads are not decoded in full.
  - Serialized `ArchiveDownloadQueue.process_queue()` with a lock and prevented unreachable torrent clients from being mistaken for removed torrents.
  - Prevented duplicate gallery downloads and duplicate update work via task deduplication, atomic per-task-type worker exclusivity (`pg_advisory_xact_lock`), and `gallery_creator` re-validation; preserved `ChaikaGalleryData` fields across `gallery_creator` handover.
  - Added `TaskQueue.attempts` / `max_attempts` so crash-interrupted tasks stop re-queueing after exhausting retries (without counting clean shutdown reverts), ensured timed-out tasks receive cancel events and stop via `TaskCancelledError`, and raised explicit errors on missing parsers, unknown task types, and empty archive task selections (requiring explicit `{"all": true}` for library-wide `fileinfo`/`thumbnails` runs).
  - Kept `GalleryMetadataUpdater` processing remaining chunks when one chunk fails, stopped refreshes from forcing `public=True`, fixed `TimedDatabasePruner.job()` busy-looping when retention is disabled, preserved the newest `django-simple-history` revision per object during pruning, and logged errors/PID collisions in `WorkerProcess.cleanup_dead_workers()`.
- **Viewer, Matcher & Deprecation Fixes**:
  - Fixed `has_positive_tag_filter` unbound error in `filter_galleries`, percent-encoded `X-Accel-Redirect` paths in `archive_thumb`, fixed `user-management -dt` lookup field, fixed `nexus` `clean_title` returning empty strings, stopped `j18` matcher from mutating shared headers, handled unparseable responses in `mugimugi` matcher, and resolved `BadHeaderError` / BeautifulSoup `text=` deprecation warnings.

### Security

- Required authentication to view archive and image contextual extraction metrics.
- Enforced CSRF-protected POST-only token creation and revocation on the user profile page instead of mutating state on GET requests.
