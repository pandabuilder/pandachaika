import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from core.base.cancellation import TaskCancelledError, ping_activity
from core.base.errors import ProviderBannedError
from core.base.gallery_sync import sync_gallery_db_entry
from core.base.utilities import chunks

if TYPE_CHECKING:
    from core.base.parsers import BaseParser
    from core.base.setup import Settings
    from core.base.types import GalleryData

logger = logging.getLogger(__name__)

# Arguments that used to be sent to the web crawler to request a metadata update, now implicit.
IGNORED_LEGACY_ARGUMENTS = ("--update-mode",)


@dataclass
class UpdateSummary:
    requested: int = 0
    unsupported: list[str] = field(default_factory=list)
    fetched: int = 0
    updated: int = 0
    failed: int = 0


class GalleryMetadataUpdater:
    """Refresh the metadata of already known Galleries, and propagate it to their related Archives.

    Unlike ``WebCrawler``, this does not classify URLs, check wanted filters, or go through the downloaders.
    The only step doing network requests is ``BaseParser.fetch_gallery_data_for_update``, which uses the rate limited
    request functions of each provider (so limits are shared with any other task running at the same time).
    The results are written to the database chunk by chunk, so progress is not lost if the task is stopped.

    Related galleries found in the new metadata (parent, first, current...) are intentionally not queued. We don't
    know which downloaders the user would like to use for them.
    """

    def __init__(self, settings: "Settings") -> None:
        self.settings = settings
        self.settings.update_metadata_mode = True
        self.settings.keep_dl_type = True
        # Never retarget the downloaders for a gallery that is only being refreshed.
        self.settings.link_child = None
        self.settings.link_newer = None

    def update(self, arg_line: Iterable[str]) -> UpdateSummary:
        links = self.get_links(arg_line)
        summary = UpdateSummary(requested=len(links))

        if not links:
            logger.info("No galleries to update, Gallery Updater done.")
            return summary

        remaining_urls = set(links)

        for parser in self.settings.provider_context.get_parsers(self.settings):
            if not remaining_urls:
                break
            accepted_urls = parser.filter_accepted_urls(remaining_urls)
            if not accepted_urls:
                continue
            remaining_urls.difference_update(accepted_urls)

            if not parser.supports_gallery_update():
                logger.warning(
                    "Provider %s does not support updating galleries by link, skipping %d links.",
                    parser.name,
                    len(accepted_urls),
                )
                summary.unsupported.extend(accepted_urls)
                continue

            urls = parser.filter_update_urls(accepted_urls)
            skipped = len(accepted_urls) - len(urls)
            if skipped:
                logger.warning(
                    "Provider %s: skipping %d links that do not point to a single gallery.", parser.name, skipped
                )
            if urls:
                self.update_provider(parser, sorted(urls), summary)

        if remaining_urls:
            logger.warning("No provider accepts %d of the links to update, skipping.", len(remaining_urls))
            summary.unsupported.extend(sorted(remaining_urls))

        logger.info(
            "Gallery Updater done. Requested: %d, fetched: %d, updated: %d, failed: %d, unsupported: %d.",
            summary.requested,
            summary.fetched,
            summary.updated,
            summary.failed,
            len(summary.unsupported),
        )
        return summary

    @staticmethod
    def get_links(arg_line: Iterable[str]) -> list[str]:
        links: list[str] = []
        for arg in arg_line:
            if arg in IGNORED_LEGACY_ARGUMENTS:
                continue
            if arg.startswith("-"):
                logger.warning("Ignoring unsupported argument for the Gallery Updater: %s", arg)
                continue
            if arg and arg not in links:
                links.append(arg)
        return links

    def update_provider(self, parser: "BaseParser", urls: list[str], summary: UpdateSummary) -> None:
        logger.info("Updating %d galleries from provider %s.", len(urls), parser.name)

        activity_cb = getattr(self.settings, "activity_callback", None)
        is_cancelled = getattr(self.settings, "is_cancelled", None)
        total = len(urls)
        done = 0

        for url_chunk in chunks(urls, max(1, parser.update_fetch_chunk_size)):
            self.check_cancelled(is_cancelled, done, total)
            self.touch_activity(activity_cb)

            try:
                galleries_data = parser.fetch_gallery_data_for_update(url_chunk)
            except (TaskCancelledError, ProviderBannedError):
                raise
            except Exception:
                # One bad chunk (network error, parser failure) must not drop the remaining chunks and providers.
                logger.exception("Provider %s: could not fetch metadata for %d galleries.", parser.name, len(url_chunk))
                summary.failed += len(url_chunk)
                done += len(url_chunk)
                continue
            summary.fetched += len(galleries_data)
            summary.failed += max(0, len(url_chunk) - len(galleries_data))

            self.touch_activity(activity_cb)

            for gallery_data in galleries_data:
                self.check_cancelled(is_cancelled, done, total)
                if self.apply_gallery_data(parser, gallery_data):
                    summary.updated += 1
                else:
                    summary.failed += 1

            done += len(url_chunk)
            logger.info("Provider %s: processed %d of %d galleries.", parser.name, done, total)

    def apply_gallery_data(self, parser: "BaseParser", gallery_data: "GalleryData") -> bool:
        # Refreshing a known gallery must not change its visibility, so ``add_as_public`` (for newly added
        # galleries) is deliberately not applied here.
        try:
            gallery = sync_gallery_db_entry(
                self.settings,
                parser.general_utils,
                gallery_data,
                update_existing=True,
            )
        except Exception:
            logger.exception("Could not update the gallery %s (%s).", gallery_data.gid, gallery_data.link)
            parser.notify_gallery(None, gallery_data.link, "failed")
            return False

        if not gallery:
            parser.notify_gallery(None, gallery_data.link, "failed")
            return False

        try:
            parser.after_gallery_update(gallery, gallery_data)
            if self.settings.non_current_links_as_deleted and parser.is_current_link_non_current(gallery_data):
                logger.info("Link: %s detected as non-current, it will be marked as deleted.", gallery_data.link)
                gallery.mark_as_deleted()
        except Exception:
            logger.exception("Post update processing failed for gallery %s (%s).", gallery_data.gid, gallery_data.link)

        parser.notify_gallery(gallery, gallery_data.link, "success")
        return True

    @staticmethod
    def check_cancelled(is_cancelled, done: int, total: int) -> None:
        if callable(is_cancelled) and is_cancelled():
            raise TaskCancelledError(f"Task cancelled while updating galleries ({done} of {total} done)")

    @staticmethod
    def touch_activity(activity_cb) -> None:
        ping_activity(activity_cb)

