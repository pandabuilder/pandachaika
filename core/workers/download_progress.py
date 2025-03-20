import logging
import os
from itertools import groupby

import django.utils.timezone as django_tz
from django.db import close_old_connections

from core.base.setup import Settings
from core.downloaders.postdownload import PostDownloader
from core.downloaders.torrent import get_torrent_client
from core.workers.schedulers import BaseScheduler
from viewer.models import DownloadEvent

logger = logging.getLogger(__name__)


def mark_completed_and_failed_for_indirect_downloads(download_events):
    events_completed = [x for x in download_events if x.archive and x.archive.crc32]
    # If there's no archive, we assume it was deleted and considered a failed download for indirect methods
    events_failed = [x for x in download_events if not x.archive or x.archive.is_recycled()]
    for download_event in events_completed:
        download_event.finish_download()
        download_event.save()
    for download_event in events_failed:
        download_event.set_as_failed()
        download_event.save()


class DownloadProgressChecker(BaseScheduler):

    thread_name = "download_progress_checker"
    TORRENT_METHODS = ["torrent", "torrent_api"]
    HATH_METHODS = ["hath"]
    ARCHIVE_METHODS = ["archive", "archive_js", "gallerydl"]

    def __init__(self, settings: Settings, web_queue=None, timer=1, pk=None):
        super().__init__(settings, web_queue, timer, pk)

    @staticmethod
    def timer_to_seconds(timer: float) -> float:
        return timer

    def job(self) -> None:
        while not self.stop.is_set():

            seconds_to_wait = self.wait_until_next_run()
            if self.stop.wait(timeout=seconds_to_wait):
                return

            close_old_connections()

            download_events_all = DownloadEvent.objects.in_progress().select_related("archive")

            for download_method, download_events_iter in groupby(
                download_events_all.order_by("download_id"), lambda x: x.method
            ):
                download_events = list(download_events_iter)
                if download_method in self.TORRENT_METHODS:
                    mark_completed_and_failed_for_indirect_downloads(download_events)
                    events_to_check = [(x.download_id, x) for x in download_events if not x.completed]
                    if events_to_check:
                        client = get_torrent_client(self.settings.torrent)
                        if client:
                            client.connect()
                            download_progresses = client.get_download_progress(events_to_check)
                            for download_event, download_progress in download_progresses:
                                download_event.progress = download_progress
                                if download_progress >= 100:
                                    download_event.finish_download()
                                download_event.save()
                elif download_method in self.HATH_METHODS:
                    mark_completed_and_failed_for_indirect_downloads(download_events)
                    archives_to_check = [(x.archive, x) for x in download_events if x.archive and not x.completed]
                    if archives_to_check:
                        post_downloader = PostDownloader(self.settings)
                        post_download_progresses = post_downloader.check_download_progress_archives(archives_to_check)
                        for download_event, download_progress in post_download_progresses.items():
                            download_event.progress = download_progress * 100
                            if download_event.progress >= 100:
                                download_event.finish_download()
                            download_event.save()
                elif download_method in self.ARCHIVE_METHODS:
                    for download_event in download_events:
                        if download_event.archive:
                            download_event.finish_download()
                            download_event.save()
                        elif (
                            download_event.total_size > 0
                            and download_event.download_id
                            and os.path.isfile(download_event.download_id)
                        ):
                            file_stats = os.stat(download_event.download_id)
                            filesize = file_stats.st_size
                            download_progress = 100 * filesize / download_event.total_size
                            download_event.progress = download_progress
                            if download_progress >= 100:
                                download_event.finish_download()
                            download_event.save()

            self.update_last_run(django_tz.now())
