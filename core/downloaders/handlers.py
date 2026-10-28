import copy
import logging
import os
import shutil
import subprocess
import typing
from tempfile import mkdtemp
from typing import Any

from core.base.setup_utilities import GeneralUtils
from core.base.utilities import (
    replace_illegal_name,
    get_base_filename_string_from_gallery_data,
    available_filename,
    get_zip_fileinfo_for_gallery,
    calc_crc32,
)
from core.base.types import GalleryData, TorrentClient, DataDict, ProviderSettings

if typing.TYPE_CHECKING:
    from core.base.setup import Settings
    from viewer.models import Gallery, Archive, WantedGallery, DownloadEvent

logger = logging.getLogger(__name__)


class Meta(type):
    type = ""
    provider = ""

    def __str__(self) -> str:
        return "{}_{}".format(self.provider, self.type)


class BaseDownloader[T_ProviderSettings: ProviderSettings](metaclass=Meta):

    type = ""
    provider = ""
    archive_only = False
    no_metadata = False
    mark_hidden_if_last = False
    direct_downloader = True

    def __init__(self, settings: "Settings", general_utils: GeneralUtils) -> None:
        self.settings = settings
        self.general_utils = general_utils
        self.own_settings: T_ProviderSettings = settings.providers[self.provider]
        self.fileDownloaded = 0
        self.return_code = 0
        self.gallery_db_entry: "Gallery" | None = None
        self.archive_db_entry: "Archive" | None = None
        self.crc32: str = ""
        self.original_gallery: GalleryData | None = None
        self.gallery: GalleryData | None = None
        self.download_id: str | None = None
        self.download_event: "DownloadEvent" | None = None
        self.queued_download_data: dict[str, Any] | None = None

    def __str__(self) -> str:
        return "{}_{}".format(self.provider, self.type)

    def is_generic(self) -> bool:
        return self.provider == "generic"

    @classmethod
    def valid_providers(cls) -> list[str]:
        return [cls.provider]

    def start_download(self) -> None:
        pass

    def update_archive_db(self, default_values: DataDict) -> "Archive | None":
        pass

    def update_gallery_db(self) -> None:
        if not self.original_gallery or not self.settings.gallery_model:
            return
        gallery_model = self.settings.gallery_model
        if self.settings.keep_dl_type and self.original_gallery.dl_type is not None:
            self.original_gallery.dl_type = None
        if self.type == "submit":
            self.original_gallery.origin = gallery_model.OriginChoices.ORIGIN_SUBMITTED
        if self.no_metadata:
            self.original_gallery.status = gallery_model.StatusChoices.NO_METADATA
        if self.settings.gallery_reason:
            self.original_gallery.reason = self.settings.gallery_reason
        self.gallery_db_entry = gallery_model.objects.update_or_create_from_values(self.original_gallery)
        if self.gallery_db_entry:
            self.gallery_db_entry.update_index()

            if self.settings.link_child is not None:
                child_gallery = gallery_model.objects.filter(
                    gid=self.settings.link_child, provider=self.gallery_db_entry.provider
                ).first()
                if child_gallery:
                    child_gallery.parent_gallery = self.gallery_db_entry
                    child_gallery.save()

            if self.settings.link_newer is not None:
                newer_gallery = gallery_model.objects.filter(
                    gid=self.settings.link_newer, provider=self.gallery_db_entry.provider
                ).first()
                if newer_gallery:
                    newer_gallery.first_gallery = self.gallery_db_entry
                    newer_gallery.save()

            # If we are updating a gallery's metadata, and we get a new tag that would have been rejected,
            # mark the archive. Do the same for a wanted match.
            wanted_invalidated: list["WantedGallery"] = []
            if self.settings.update_metadata_mode:
                banned_result, banned_reasons = self.general_utils.discard_by_gallery_data(
                    self.gallery_db_entry.tag_list(), force_check=True
                )
                if self.settings.recheck_wanted_on_update and self.settings.wanted_gallery_model:
                    wanted_found = self.settings.wanted_gallery_model.objects.filter(
                        foundgallery__gallery=self.gallery_db_entry
                    )

                    rematch_wanted = self.gallery_db_entry.match_against_wanted_galleries(
                        wanted_filters=wanted_found, skip_already_found=False
                    )

                    for single_wanted_found in wanted_found:
                        if single_wanted_found not in rematch_wanted:
                            wanted_invalidated.append(single_wanted_found)

            else:
                banned_result, banned_reasons = False, []
            for archive in self.gallery_db_entry.archive_set.all():
                if archive.gallery:
                    archive.set_titles_from_gallery(archive.gallery)

                    if self.settings.recheck_wanted_on_update and self.settings.archive_manage_entry_model:

                        for single_wanted_invalidated in wanted_invalidated:
                            mark_comment = (
                                "The refreshed Gallery metadata invalidates the "
                                "previously accepted WantedGallery: (special-link):({})({})"
                            ).format(single_wanted_invalidated.title, single_wanted_invalidated.get_absolute_url())

                            if not self.settings.archive_manage_entry_model.objects.filter(
                                archive=archive, mark_reason="invalidated_wanted", mark_comment=mark_comment
                            ).exists():
                                manager_entry, _ = self.settings.archive_manage_entry_model.objects.update_or_create(
                                    archive=archive,
                                    mark_reason="invalidated_wanted",
                                    mark_comment=mark_comment,
                                    defaults={
                                        "mark_priority": 5.0,
                                        "mark_check": True,
                                        "origin": self.settings.archive_manage_entry_model.ORIGIN_SYSTEM,
                                    },
                                )

                    archive_banned_result: bool = False

                    if banned_result:
                        archive_banned_result, archive_banned_reasons = self.general_utils.discard_by_gallery_data(
                            archive.tag_list(), force_check=True
                        )

                    archive.set_tags_from_gallery(archive.gallery)

                    if banned_result and not archive_banned_result and self.settings.archive_manage_entry_model:
                        mark_comment = "The refreshed Gallery has added banned data:\n{}".format(banned_reasons)

                        manager_entry, _ = self.settings.archive_manage_entry_model.objects.update_or_create(
                            archive=archive,
                            mark_reason="banned_data",
                            defaults={
                                "mark_comment": mark_comment,
                                "mark_priority": 5.0,
                                "mark_check": True,
                                "origin": self.settings.archive_manage_entry_model.ORIGIN_SYSTEM,
                            },
                        )

    def should_queue_archive_download(self, provider_settings: Any | None = None) -> bool:
        target_settings = provider_settings or self.own_settings
        if target_settings is not None and getattr(target_settings, "queue_archive_downloads", None) is not None:
            return bool(target_settings.queue_archive_downloads)
        if getattr(self.settings, "queue_archive_downloads", None) is not None:
            return bool(self.settings.queue_archive_downloads)
        return int(getattr(self.settings, "parallel_archive_downloaders", 0) or 0) > 0

    def enqueue_archive_download(
        self,
        download_url: str,
        filepath: str,
        request_dict: dict[str, Any] | None = None,
        rate_limit_provider: str | None = None,
        rate_limit_calls: int = 1,
        rate_limit_period: float | None = None,
        extra_options: dict[str, Any] | None = None,
    ) -> None:
        req_opts: dict[str, Any] = {}
        if request_dict:
            for k in ("headers", "cookies", "proxies", "timeout", "params", "data"):
                if k in request_dict and request_dict[k] is not None:
                    req_opts[k] = request_dict[k]
        provider_key = rate_limit_provider or self.provider
        if rate_limit_period is None:
            if self.own_settings and hasattr(self.own_settings, "wait_timer"):
                rate_limit_period = float(self.own_settings.wait_timer)
            else:
                rate_limit_period = float(getattr(self.settings, "wait_timer", 0))
        req_opts["rate_limit_provider"] = provider_key
        req_opts["rate_limit_name"] = provider_key if provider_key.endswith("_web") else f"{provider_key}_web"
        req_opts["rate_limit_calls"] = int(rate_limit_calls)
        req_opts["rate_limit_period"] = float(rate_limit_period)
        req_opts["mark_similar_new_archives"] = bool(getattr(self.settings, "mark_similar_new_archives", False))
        req_opts["preserve_extracted"] = bool(getattr(self.settings, "preserve_extracted", False))
        req_opts["archive_only"] = bool(self.archive_only)
        if getattr(self.settings, "archive_user", None):
            req_opts["archive_user_id"] = getattr(self.settings.archive_user, "id", None)
        if getattr(self.settings, "archive_reason", None):
            req_opts["archive_reason"] = getattr(self.settings, "archive_reason", "")
        if getattr(self.settings, "event_action", None):
            req_opts["event_action"] = getattr(self.settings, "event_action", "")
        if extra_options:
            req_opts.update(extra_options)

        self.queued_download_data = {
            "download_url": download_url,
            "filepath": filepath,
            "provider": provider_key,
            "request_options": req_opts,
        }
        self.crc32 = ""
        self.fileDownloaded = 1
        self.return_code = 1

    def create_download_event(
        self,
        name,
        method,
        download_id,
        archive: "Archive | None" = None,
        gallery: "Gallery | None" = None,
        progress: float = 0.0,
        total_size: int = 0,
        status: str | None = None,
        provider: str | None = None,
        download_url: str = "",
        request_options: dict[str, Any] | None = None,
    ) -> "DownloadEvent | None":
        if not self.settings.download_event_model:
            return None
        if not total_size and archive and archive.filesize:
            total_size = archive.filesize

        kwargs: dict[str, Any] = {
            "name": name,
            "method": method,
            "download_id": download_id,
            "archive": archive,
            "gallery": gallery,
            "progress": progress,
            "total_size": total_size,
            "provider": provider if provider is not None else self.provider,
        }
        if status is not None:
            kwargs["status"] = status
        if download_url:
            kwargs["download_url"] = download_url
        if request_options is not None:
            kwargs["request_options"] = request_options

        download_event = self.settings.download_event_model(**kwargs)

        download_event.save()

        return download_event
        # result, torrent_id

    def init_download(self, gallery: GalleryData, wanted_gallery_list: list["WantedGallery"] | None = None) -> None:

        self.fileDownloaded = 0
        self.return_code = 0
        self.crc32 = ""
        self.download_id = None
        self.download_event = None
        self.queued_download_data = None
        self.gallery_db_entry = None
        self.archive_db_entry = None

        self.original_gallery = copy.deepcopy(gallery)
        self.gallery = gallery

        self.original_gallery.dl_type = self.type
        activity_cb = getattr(self.settings, "activity_callback", None)
        if callable(activity_cb):
            try:
                activity_cb()
            except Exception:
                pass
        self.start_download()
        if callable(activity_cb):
            try:
                activity_cb()
            except Exception:
                pass

        if self.return_code == 0:
            return

        wanted_galleries_last_reason = None

        if wanted_gallery_list:
            for wanted_gallery in wanted_gallery_list:
                if wanted_gallery.reason:
                    wanted_galleries_last_reason = wanted_gallery.reason

        if wanted_galleries_last_reason:
            self.original_gallery.reason = wanted_galleries_last_reason

        if not self.archive_only:
            self.update_gallery_db()

        if self.fileDownloaded == 1:

            default_values: dict[str, Any] = {"match_type": self.type, "source_type": self.provider}

            if self.settings.archive_reason:
                default_values["reason"] = self.settings.archive_reason
            if self.settings.archive_details:
                default_values["details"] = self.settings.archive_details
            if self.settings.archive_source:
                default_values["source_type"] = self.settings.archive_source
            if self.settings.archive_user:
                default_values["user"] = self.settings.archive_user
            if self.settings.archive_origin:
                default_values["origin"] = self.settings.archive_origin

            if wanted_galleries_last_reason:
                default_values["reason"] = wanted_galleries_last_reason

            self.archive_db_entry = self.update_archive_db(default_values)

            linked_gallery = self.gallery_db_entry or (
                self.archive_db_entry.gallery if self.archive_db_entry else None
            )

            if self.queued_download_data is not None:
                total_size = int(self.gallery.filesize or 0) if self.gallery else 0
                event_name = (
                    self.gallery.link
                    if self.gallery and self.gallery.link
                    else self.queued_download_data["download_url"]
                )
                self.download_event = self.create_download_event(
                    event_name,
                    self.type,
                    self.queued_download_data["filepath"],
                    archive=self.archive_db_entry,
                    gallery=linked_gallery,
                    total_size=total_size,
                    status="queued",
                    provider=self.queued_download_data["provider"],
                    download_url=self.queued_download_data["download_url"],
                    request_options=self.queued_download_data["request_options"],
                )
                logger.info(
                    "Queued archive download event #%s for archive %s (provider: %s, url: %s)",
                    getattr(self.download_event, "pk", None),
                    getattr(self.archive_db_entry, "pk", None),
                    self.queued_download_data["provider"],
                    self.queued_download_data["download_url"],
                )
            elif self.direct_downloader and self.download_event:
                if self.archive_db_entry is not None:
                    self.download_event.archive = self.archive_db_entry
                if linked_gallery is not None:
                    self.download_event.gallery = linked_gallery
                self.download_event.finish_download()
                self.download_event.save()

            elif not self.direct_downloader and self.download_id is not None:
                self.create_download_event(
                    self.gallery.link,
                    self.type,
                    self.download_id,
                    archive=self.archive_db_entry,
                    gallery=linked_gallery,
                )

            if (
                self.archive_db_entry
                and self.settings.mark_similar_new_archives
                and self.queued_download_data is None
            ):
                self.archive_db_entry.create_marks_for_similar_archives()


class BaseInfoDownloader(BaseDownloader):

    type = "info"

    def start_download(self) -> None:

        logger.info("Adding {} gallery, without downloading an archive".format(self.provider))

        self.return_code = 1


class BaseFakeDownloader(BaseDownloader):

    type = "fake"

    def start_download(self) -> None:

        if not self.original_gallery:
            return

        logger.info("Adding {} gallery info to database, and faking the archive file".format(self.provider))

        self.fileDownloaded = 1
        self.return_code = 1

    def update_archive_db(self, default_values: DataDict) -> "Archive | None":

        if not self.gallery or not self.settings.archive_model:
            return None

        values: DataDict = {
            "zipped": "",
            "crc32": self.crc32,
        }
        if self.gallery.title is not None:
            values["title"] = self.gallery.title
        if self.gallery.title_jpn is not None:
            values["title_jpn"] = self.gallery.title_jpn
        if self.gallery.filesize is not None:
            values["filesize"] = self.gallery.filesize
        if self.gallery.filecount is not None:
            values["filecount"] = self.gallery.filecount
        default_values.update(values)
        return self.settings.archive_model.objects.create_by_values_and_gid(
            default_values,
            (self.gallery.gid, self.gallery.provider),
        )


class BaseTorrentDownloader(BaseDownloader):

    type = "torrent"
    direct_downloader = False

    def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super().__init__(*args, **kwargs)
        self.expected_torrent_name = ""
        self.expected_torrent_extension = ""

    def connect_and_download(self, client: TorrentClient, torrent_link: str) -> None:
        if not self.gallery:
            return None
        client.connect()
        if client.send_url:
            result, torrent_id = client.add_url(torrent_link, download_dir=os.path.join(self.settings.torrent["download_dir"], str(self.gallery.gid)))
        else:
            result, torrent_id = client.add_torrent(
                self.general_utils.get_torrent(
                    torrent_link, self.own_settings.cookies, convert_to_base64=client.convert_to_base64
                ),
                download_dir=os.path.join(self.settings.torrent["download_dir"], str(self.gallery.gid)),
            )
        if result:
            self.download_id = torrent_id
            if client.expected_torrent_name:
                self.expected_torrent_name = "{} [{}]".format(client.expected_torrent_name, self.gallery.gid)
            else:

                to_use_filename = get_base_filename_string_from_gallery_data(self.gallery)

                self.expected_torrent_name = "{} [{}]".format(replace_illegal_name(to_use_filename), self.gallery.gid)
            if client.expected_torrent_extension:
                self.expected_torrent_extension = client.expected_torrent_extension
            else:
                self.expected_torrent_extension = ".zip"

            self.fileDownloaded = 1
            self.return_code = 1
            if client.total_size > 0:
                self.gallery.filesize = client.total_size

            if not self.expected_torrent_name.endswith(self.expected_torrent_extension):
                final_name = replace_illegal_name(self.expected_torrent_name) + self.expected_torrent_extension
            else:
                final_name = replace_illegal_name(self.expected_torrent_name)

            self.gallery.filename = os.path.join(self.own_settings.torrent_dl_folder, final_name)
            logger.info(
                "Torrent added, expecting downloaded name: {}, local name: {}".format(
                    self.expected_torrent_name, self.gallery.filename
                )
            )
        else:
            self.return_code = 0
            logger.error(
                "There was an error adding the torrent to the client, torrent link: {}, error in client {}:".format(
                    torrent_link, client.error
                )
            )


class BaseGalleryDLDownloader(BaseDownloader):

    type = "gallerydl"

    def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super().__init__(*args, **kwargs)

    def start_download(self) -> None:

        if not self.gallery or not self.gallery.link:
            return

        if self.settings.gallery_dl.executable_path:
            exe_path_to_use = shutil.which(self.settings.gallery_dl.executable_path)
        else:
            exe_path_to_use = shutil.which(self.settings.gallery_dl.executable_name)

        if not exe_path_to_use:
            self.return_code = 0
            logger.error("The gallery-dl executable was not found")
            return

        directory_path = mkdtemp(dir=self.settings.temp_directory_path)
        try:
            arguments = ["--zip", "--dest", "{}".format(directory_path)]

            if self.own_settings.proxy:
                arguments.append("--proxy")
                arguments.append("{}".format(self.own_settings.proxy))

            if self.settings.gallery_dl.config_file:
                arguments.append("--config")
                arguments.append("{}".format(self.settings.gallery_dl.config_file))

            if self.settings.gallery_dl.extra_arguments:
                arguments.append("{}".format(self.settings.gallery_dl.extra_arguments))

            arguments.append("{}".format(self.gallery.link))

            logger.info("Calling gallery-dl: {}.".format(" ".join([exe_path_to_use, *arguments])))

            self.download_event = self.create_download_event(self.gallery.link, self.type, "")

            process_result = subprocess.run(
                [exe_path_to_use, *arguments], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True
            )

            if process_result.returncode != 0:
                self.return_code = 0
                if process_result.stderr:
                    logger.error("An error was captured when running gallery-dl: {}".format(process_result.stderr))
                else:
                    logger.error("Return code was not 0: {}".format(process_result.returncode))
                return

            if process_result.stderr:
                logger.warning("gallery-dl produced warnings: {}".format(process_result.stderr))

            # If we downloaded more than one file, get the latest one
            output_path = ""
            file_name = ""
            for dir_path, dir_names, filenames in os.walk(directory_path):
                for current_file in filenames:
                    file_name = current_file
                    output_path = os.path.join(dir_path, current_file)

            if not output_path or not os.path.isfile(output_path):
                self.return_code = 0
                logger.error("The resulting download file was not found: {}".format(file_name))
                return

            self.gallery.filename = available_filename(
                self.settings.MEDIA_ROOT, os.path.join(self.own_settings.archive_dl_folder, replace_illegal_name(file_name))
            )

            if self.download_event:
                self.download_event.name = self.gallery.filename
                self.download_event.save()

            self.gallery.title = os.path.splitext(file_name)[0]

            filepath = os.path.join(self.settings.MEDIA_ROOT, self.gallery.filename)

            shutil.move(output_path, filepath)
        finally:
            shutil.rmtree(directory_path, ignore_errors=True)

        self.gallery.filesize, self.gallery.filecount = get_zip_fileinfo_for_gallery(filepath)
        if self.gallery.filesize > 0:
            self.crc32 = calc_crc32(filepath)

            self.fileDownloaded = 1
            self.return_code = 1

        else:
            logger.error("Could not download archive")
            self.return_code = 0

    def update_archive_db(self, default_values: DataDict) -> "Archive | None":

        if not self.gallery or not self.settings.archive_model:
            return None

        values = {
            "title": self.gallery.title,
            "title_jpn": self.gallery.title_jpn,
            "zipped": self.gallery.filename,
            "crc32": self.crc32,
            "filesize": self.gallery.filesize,
            "filecount": self.gallery.filecount,
        }
        default_values.update(values)
        return self.settings.archive_model.objects.update_or_create_by_values_and_gid(
            default_values, (self.gallery.gid, self.gallery.provider), zipped=self.gallery.filename
        )
