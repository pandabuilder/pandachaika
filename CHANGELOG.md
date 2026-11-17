# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Wanted Gallery Admin Actions**: "Add wanted tags" and "Remove wanted tags" actions on `/admin/viewer/wantedgallery/`, taking a comma separated list of tags (`scope:name` or `name`) in the action's text field. Adding creates missing tags; removing skips (and reports) tags that don't exist.

### Changed

- **Gallery and Archive Change Log**: Replaced `django-simple-history` (now removed) with a change log that stores only what changed: the tracked fields' old and new values, and the tags added or removed. Saves and provider refreshes that change nothing no longer write anything, and a refresh that changes fields and tags writes one entry instead of up to three full snapshots with a copy of every tag. Tracked gallery fields: title, title_jpn, category, uploader, comment, expunged, disowned, rating, fjord, public and tags. Tracked archive fields: title, title_jpn, source_type, reason, public_date, public, gallery, details and custom tags (tags copied from the gallery are already in the gallery's log). Fields that don't change upstream or are already covered by the activity log (e.g. posted, file count/size, status, crc32, binned) and `provider_metadata` are no longer kept in the history. The change log pages list the changes directly; the "Show object data" toggle is gone.
  - Migrations `0225`–`0228` convert the existing history, then drop the `viewer_historicalgallery`, `viewer_historicalgallery_tags` and `viewer_historicalarchive` tables. Identical consecutive snapshots are discarded and snapshots taken within 5 seconds of each other are merged. History of deleted galleries/archives and of deleted tags is not kept. On large tables the conversion can take a while; it prints its progress.
  - `history_days` retention now prunes change log rows by date. It no longer needs to keep the newest snapshot of each object, since the current values live in the gallery or archive itself.
- **Streamlined Submit Queue, Archive Matching, Activity Event Log, Downloads and Server Logs pages**: The five pages now share the look of the database maintenance, workers and file check pages (shared `css/dm-pages.css` / `js/dm-pages.js`): clickable summary tiles that act as quick filters (pending/approved/denied entries, unmatched archives and archives with candidates, queued/running/finished/failed downloads, errors/warnings in the last 24 hours), removable chips for the active filters, a pager with page jump and page size, relative times, and a bulk action bar that follows the scroll and enables its buttons once something is selected (shift-click selects a range, clicking a row toggles it).
  - Submit queue: status filter (`status=submitted|approved|denied|already_present`, the older checkboxes still work), thumbnails, wanted/archive/similar flags, resolution details. Gallery filters apply as soon as one has a value, without "Filter galleries".
  - Archive matching: candidates grouped per gallery with accuracy bars and image count/size comparison; "Link" and "Clear" work without reloading the page; matching worker status; provider, method, cutoff and auto-link in the action bar.
  - Activity event log: top actions and users of the last 7 days as quick filters, searchable action picker, user filter (`user=`, staff or `read_all_logs`), "all events of this object" links, expandable data. Title/tags search both galleries and archives when neither is chosen.
  - Downloads and server logs: live refresh with a Live/Paused toggle (downloads are live by default), selection kept across refreshes on downloads, collapsible tracebacks and a logger filter link on logs.
  - Bulk actions on the submit queue and archive matching redirect after posting, so reloading doesn't repeat them.

### Fixed

- **Missing Referer Errors**: Views that redirect back to the previous page no longer fail with a server error when the browser sends no `Referer` header (privacy settings, `Referrer-Policy: no-referrer`). They go to a sensible page instead (the archive, gallery, tools or list page), and a `Referer` pointing to another site is ignored.
- **Download Monitor and Server Logs Escaping**: Rows added by the auto-refresh now escape download names, error messages and log messages instead of inserting them as HTML.
- **Event Log and Submit Queue Date Filters**: The "to" date now includes the whole day.
- **wanimagazine Wanted Generator**: Fixed the wanimagazine parser after changes on the remote site. Feed subpaths in `wanted_params_<n>_subpath` may need to be updated.
- **Collaborator View Permissions**: Restored permission checks lost when the collaborator views were split into modules. `/match-archives/` requires `match_archive` again and `/archives-by-field/` requires `manage_archive` again. `/upload-archive/` no longer also needs `match_archive`, and `/galleries-by-field/` no longer also needs `manage_archive`.

### Security

- **Monitored Link Permissions**: Changing monitored links now needs Django's model permissions on top of `view_monitored_links`: `add_monitoredlink` to create, `change_monitoredlink` to edit, start, stop or force-run, and `delete_monitoredlink` to delete. Users who only have `view_monitored_links` can still see the list but no longer change links; give the other permissions to users or groups that should. Start, stop, force-run and the inline actions on `/monitored-links/` now only work with POST, and the list only shows the buttons the user can use.

## [1.3.0] - 2026-11-14

### Added

- **Admin Report**: New staff page at `/admin-report/` (Settings → Admin Report) summarizing a time window (last 24 hours, 7 or 30 days, or a custom range): archives and galleries added, event log activity, wanted gallery matches, finished and failed downloads, failed tasks, warning/error server logs grouped by similar message, submissions, and new archive file-check issues. It also shows the current state: download and task backlogs, unresponsive or overdue schedulers, stale worker processes, provider rate-limit cooldowns, disk usage of the media folders, and a "Needs attention" summary. Sections, minimum log level and list sizes are selectable. `?format=json` returns the same data, for staff sessions or bearer tokens with the `read` scope (see `docs/api/admin_report.md`).
- **Scheduled Admin Report Emails**: Each staff user can have the admin report emailed on their own schedule ("My report email" on the admin report page): every N hours, daily or weekly at a time in their own time zone, with their choice of sections, minimum log level and list size, and an option to skip the email when nothing needs attention. Each email covers the time since that user's last report, and links to the same report on the site; the page also gets a "Since my last report email" window and a "Send my report now" button. Sent by the new `timed_admin_reporter` scheduler, enabled with `admin_report.enable` (checks every `admin_report.check_interval_minutes`, default 15) and using the `mail_logging` SMTP settings. Adds the `AdminReportSubscription` model (migration `0224`).
- **SFTP Post-Download Transfers**: The `remote` download handler can now fetch finished torrent and H@H downloads over SFTP as well as FTPS. Set `protocol: sftp` in the `ftps` section or in a named `ftp_configs` entry (selected with `download_ftp_torrent` / `download_ftp_hath`). SFTP supports password or key authentication (`key_filename`, `key_passphrase`, falling back to the SSH agent and `~/.ssh` keys when neither is set), host key checks against `~/.ssh/known_hosts` plus an optional `known_hosts_file` (`no_certificate_check: true` accepts any host key), `port` (default 22), `bind_host` / `bind_port`, transfer progress reporting, and reconnect-and-retry on dropped connections. Adds the `paramiko` dependency.

### Changed

- **Settings Backed by the Config Models**: `Settings` attributes (`wait_timer`, `replace_metadata`, `MEDIA_ROOT`, ...) now read and write their Pydantic config section model instead of keeping a copy, so assignments are validated, and `settings.database` / `torrent` / `ftps` / `remote_site` are the section models (dict-style access still works). A `Settings` built without a config, or from a config missing a whole section, now uses the model defaults for that section (e.g. `convert_others_to_zip: true`, `archive_dl_folder: galleries/archive_dls`). Serialized task options no longer include the section models, which carried credentials such as `django_secret_key`; options queued by older versions still load.

### Fixed

- **Torrent Download Transfers**: Finished torrents are now found from the contents of their `<download dir>/<gid>` folder instead of a match on the expected torrent name, which could differ from the real name (e.g. the e-hentai API torrent name) and left finished downloads retrying forever. A single archive is transferred, a single subfolder (the torrent root) is followed into, and loose files are zipped; partially written files (`.!qB`, `.part`, `.incomplete`) are reported as incomplete, and ambiguous contents (several subfolders or archives) are logged and skipped. `.cbr` / `.cb7` files are now converted to zip like `.rar` / `.7z`.
- **Banned Tags & Uploaders Defaults**: A `general` section without `banned_tags` (`discard_tags`), `banned_uploaders` or `banned_search_queries` no longer bans the sample languages, the `random_guy` uploader and the sample query; these default to empty lists again, as before the move to Pydantic config models.
- **Settings Page Redaction**: The torrent client password (`pass`) is now redacted on the settings stats page.

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
