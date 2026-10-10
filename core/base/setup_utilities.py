import base64
import logging
import threading
import typing
from typing import Union, Optional, Any

import requests

from core.base import setup

if typing.TYPE_CHECKING:
    from core.base.types import GalleryData

logger = logging.getLogger(__name__)


# The idea of this class is to contain certain methods, to not have to pass arguments that are global.
class GeneralUtils:

    def __init__(self, global_settings: "setup.Settings") -> None:
        self.settings = global_settings

    def discard_by_gallery_data(
        self,
        tag_list: Optional[list[str]],
        uploader: Optional[str] = None,
        force_check: bool = False,
        gallery_data: Optional["GalleryData"] = None,
    ) -> tuple[bool, list[str]]:

        from viewer.utils.elasticsearch import (
            add_gallery_data_to_match_index,
            match_expression_to_wanted_index,
            remove_gallery_from_match_index,
        )

        discarded = False
        reasons: list[str] = []

        if not force_check and self.settings.update_metadata_mode:
            return discarded, reasons
        if tag_list is not None and self.settings.banned_tags:
            found_tags = set(self.settings.banned_tags).intersection(tag_list)
            if found_tags:
                discarded = True
                reasons.append("Banned tags: {}".format(", ".join(found_tags)))
        if uploader is not None and uploader != "" and self.settings.banned_uploaders:
            found_uploader = uploader in set(self.settings.banned_uploaders)
            if found_uploader:
                discarded = True
                reasons.append("Banned uploader: {}".format(uploader))

        if not discarded and gallery_data is not None and self.settings.banned_search_queries:
            index_uuid = add_gallery_data_to_match_index(gallery_data)
            if index_uuid:
                for banned_query in self.settings.banned_search_queries:
                    match_result = match_expression_to_wanted_index(banned_query, index_uuid)
                    if match_result:
                        discarded = True
                        reasons.append("Banned search query: {}".format(banned_query))
                        break

                remove_gallery_from_match_index(index_uuid)

        return discarded, reasons

    def get_torrent(
        self, torrent_url: str, cookies: dict[str, Any], convert_to_base64: bool = False
    ) -> Union[str, bytes]:

        r = requests.get(
            torrent_url, cookies=cookies, headers=self.settings.requests_headers, timeout=self.settings.timeout_timer
        )

        if not convert_to_base64:
            return r.content
        else:
            return base64.encodebytes(r.content).decode("utf-8")


def get_thread_status() -> list[tuple[tuple[str, str, str], bool]]:
    info_list = []
    thread_names = set()

    thread_list = threading.enumerate()
    scheduler_statuses: dict[str, bool] = {}
    active_tasks: set[str] = set()
    worker_running = False
    try:
        from workers.models import SchedulerState, TaskQueue, WorkerProcess
        scheduler_statuses = SchedulerState.get_all_statuses()
        active_tasks = set(
            TaskQueue.objects.filter(status=TaskQueue.Status.PROCESSING).values_list("task_type", flat=True)
        )
        worker_running = WorkerProcess.is_any_worker_running()
    except Exception:
        pass

    alias_map = {
        "post_downloader": "timed_downloader",
        "timed_downloader": "post_downloader",
        "foldercrawler": "folder_crawler",
        "folder_crawler": "foldercrawler",
        "webcrawler": "web_crawler",
        "web_crawler": "webcrawler",
        "fileinfo_worker": "recalc_all_file_info",
        "thumbnails_worker": "regenerate_all_thumbs",
    }

    for thread_info in setup.GlobalInfo.worker_threads:
        tname = thread_info[0]
        is_local = any([tname == thread.name for thread in thread_list])
        alias = alias_map.get(tname, "")
        is_active = (
            is_local
            or scheduler_statuses.get(tname, False)
            or scheduler_statuses.get(alias, False)
            or (tname == "web_queue" and worker_running)
            or (tname in active_tasks)
            or (alias in active_tasks)
        )
        info_list.append((thread_info, is_active))
        thread_names.add(tname)

    for thread_data in thread_list:
        if thread_data.name not in thread_names:
            info_list.append(((thread_data.name, "None", "other"), thread_data.is_alive()))

    return info_list


def get_thread_status_bool() -> dict[str, bool]:
    info_dict = {}

    thread_list = threading.enumerate()
    scheduler_statuses: dict[str, bool] = {}
    active_tasks: set[str] = set()
    worker_running = False
    try:
        from workers.models import SchedulerState, TaskQueue, WorkerProcess
        scheduler_statuses = SchedulerState.get_all_statuses()
        active_tasks = set(
            TaskQueue.objects.filter(status=TaskQueue.Status.PROCESSING).values_list("task_type", flat=True)
        )
        worker_running = WorkerProcess.is_any_worker_running()
    except Exception:
        pass

    alias_map = {
        "post_downloader": "timed_downloader",
        "timed_downloader": "post_downloader",
        "foldercrawler": "folder_crawler",
        "folder_crawler": "foldercrawler",
        "webcrawler": "web_crawler",
        "web_crawler": "webcrawler",
        "fileinfo_worker": "recalc_all_file_info",
        "thumbnails_worker": "regenerate_all_thumbs",
    }

    for thread_info in setup.GlobalInfo.worker_threads:
        tname = thread_info[0]
        is_local = any([tname == thread.name for thread in thread_list])
        alias = alias_map.get(tname, "")
        info_dict[tname] = (
            is_local
            or scheduler_statuses.get(tname, False)
            or scheduler_statuses.get(alias, False)
            or (tname == "web_queue" and worker_running)
            or (tname in active_tasks)
            or (alias in active_tasks)
        )

    if "post_downloader" in info_dict:
        info_dict["timed_downloader"] = info_dict["post_downloader"]

    return info_dict


def check_for_running_threads() -> bool:
    thread_list = threading.enumerate()
    for thread_name in setup.GlobalInfo.worker_threads:
        if any([thread_name[0] == thread.name for thread in thread_list]):
            return True
    return False
