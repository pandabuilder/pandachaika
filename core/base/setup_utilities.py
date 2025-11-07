import base64
import logging
import threading
from typing import Union, Optional, Any

import requests

from core.base import setup

logger = logging.getLogger(__name__)


# The idea of this class is to contain certain methods, to not have to pass arguments that are global.
class GeneralUtils:

    def __init__(self, global_settings: "setup.Settings") -> None:
        self.settings = global_settings

    def discard_by_gallery_data(
        self, tag_list: Optional[list[str]], uploader: Optional[str] = None, force_check: bool = False
    ) -> tuple[bool, list[str]]:

        discarded = False
        reasons: list[str] = []

        if not force_check and self.settings.update_metadata_mode:
            return discarded, reasons
        if tag_list is not None:
            found_tags = set(self.settings.banned_tags).intersection(tag_list)
            if found_tags:
                discarded = True
                reasons.append("Banned tags: {}".format(", ".join(found_tags)))
        if uploader is not None and uploader != "" and self.settings.banned_uploaders:
            found_uploader = uploader in set(self.settings.banned_uploaders)
            if found_uploader:
                discarded = True
                reasons.append("Banned uploader: {}".format(uploader))
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
    for thread_info in setup.GlobalInfo.worker_threads:
        info_list.append((thread_info, any([thread_info[0] == thread.name for thread in thread_list])))
        thread_names.add(thread_info[0])

    for thread_data in thread_list:
        if thread_data.name not in thread_names:
            info_list.append(((thread_data.name, "None", "other"), thread_data.is_alive()))

    return info_list


def get_thread_status_bool() -> dict[str, bool]:
    info_dict = {}

    thread_list = threading.enumerate()
    for thread_info in setup.GlobalInfo.worker_threads:
        if any([thread_info[0] == thread.name for thread in thread_list]):
            info_dict[thread_info[0]] = True
        else:
            info_dict[thread_info[0]] = False

    return info_dict


def check_for_running_threads() -> bool:
    thread_list = threading.enumerate()
    for thread_name in setup.GlobalInfo.worker_threads:
        if any([thread_name[0] == thread.name for thread in thread_list]):
            return True
    return False
