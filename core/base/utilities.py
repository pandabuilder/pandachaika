import base64
import hashlib
import heapq
import html.entities
import logging
import os
import re
import shutil
import threading
import typing
import zipfile
import zlib
from datetime import timedelta, datetime
from difflib import SequenceMatcher
from itertools import tee, islice, chain
from tempfile import mkdtemp
from typing import Union, Optional, Tuple, List, Dict, Any

from core.base.types import GalleryData, OptionalLogger

try:
    import rarfile
except ImportError:
    rarfile = None

import requests

from core.base import setup

if typing.TYPE_CHECKING:
    from core.workers.schedulers import BaseScheduler


PUSHOVER_API_URL = 'https://api.pushover.net/1/messages.json'


# The idea of this class is to contain certain methods, to not have to pass arguments that are global.
class GeneralUtils:

    def __init__(self, global_settings: 'setup.Settings') -> None:
        self.settings = global_settings

    def discard_by_tag_list(self, tag_list: Optional[List[str]]):

        if self.settings.update_metadata_mode:
            return False
        if tag_list is None:
            return False
        if any(x in self.settings.discard_tags for x in tag_list):
            return True
        return False

    def get_torrent(self, torrent_url: str, cookies: Dict[str, Any], convert_to_base64: bool = False) -> Union[str, bytes]:

        r = requests.get(
            torrent_url,
            cookies=cookies,
            headers=self.settings.requests_headers,
            timeout=self.settings.timeout_timer
        )

        if not convert_to_base64:
            return r.content
        else:
            return base64.encodebytes(r.content).decode('utf-8')


def discard_string_by_cutoff(base: str, compare: str, cutoff: float) -> bool:

    s: SequenceMatcher = SequenceMatcher()
    s.set_seq2(base)
    s.set_seq1(compare)
    if s.real_quick_ratio() >= cutoff and s.quick_ratio() >= cutoff and s.ratio() >= cutoff:
        return True
    return False


def discard_by_tag_list(tag_list: List[str], discard_tags: List[str]):
    if any(x in discard_tags for x in tag_list):
        return True
    return False


def replace_illegal_name(filepath: str) -> str:
    delete_chars = r'\/:*?"<>|'

    for c in delete_chars:
        filepath = filepath.replace(c, '')

    # Don't end with dot, might confuse some languages/OS.
    if filepath.endswith(".") and len(filepath) > 1:
        filepath = filepath[:-1]

    # Limit to 255 characters, 251 plus .zip
    return filepath[0:251]


def replace_illegal_win32_with_unicode_full_width(filepath: str) -> str:
    replace_chars = (
        ('\\', '＼'),
        ('/', '／'),
        (':', '：'),
        ('*', '＊'),
        ('?', '？'),
        ('"', '＂'),
        ('<', '＜'),
        ('>', '＞'),
        ('|', '｜')
    )

    for c in replace_chars:
        filepath = filepath.replace(c[0], c[1])

    # Don't end with dot, might confuse some languages/OS.
    if filepath.endswith(".") and len(filepath) > 1:
        filepath = filepath[:-1]

    # Limit to 255 characters, 251 plus .zip
    return filepath[0:251]


def to_full_width(title: str) -> str:
    replace_chars = (
        ('\\', '＼'),
        ('/', '／'),
        (':', '：'),
        ('*', '＊'),
        ('?', '？'),
        ('"', '＂'),
        ('<', '＜'),
        ('>', '＞'),
        ('|', '｜')
    )

    for c in replace_chars:
        title = title.replace(c[0], c[1])
    return title


def calc_crc32(filename: str) -> str:
    prev = 0
    for eachLine in open(filename, "rb"):
        prev = zlib.crc32(eachLine, prev)
    return "%X" % (prev & 0xFFFFFFFF)


def sha1_from_file_object(file_object: typing.IO[bytes]):
    block_size = 65536
    hasher = hashlib.sha1()
    buf = file_object.read(block_size)
    while len(buf) > 0:
        hasher.update(buf)
        buf = file_object.read(block_size)
    file_object.close()
    return hasher.hexdigest()


T = typing.TypeVar('T')


def chunks(l: typing.Sequence[T], n: int):
    """ Yield successive n-sized chunks from l."""
    for i in range(0, len(l), n):
        yield l[i:i + n]


def zfillrepl(matchobj: typing.Match):
    return matchobj.group(0).zfill(3)


def zfill_to_three(namefile: str) -> str:
    filename = os.path.splitext(os.path.basename(namefile))[0]

    filename = re.sub(r'\d+', zfillrepl, filename)

    return filename.lower()


def discard_zipfile_contents(name: str) -> Optional[str]:
    r = re.compile(r'(\.jpeg|\.jpg|\.png|\.gif)$', re.IGNORECASE)
    if r.search(name) and '__MACOSX' not in name:
        return name
    else:
        return None


def filecount_in_zip(filepath: str) -> int:
    try:
        my_zip = zipfile.ZipFile(filepath, 'r')
    except zipfile.BadZipFile:
        return 0

    filtered_files = list(filter(discard_zipfile_contents, sorted(my_zip.namelist(), key=zfill_to_three)))

    my_zip.close()

    return len(filtered_files)


def get_zip_filesize(filepath: str) -> int:
    try:
        my_zip = zipfile.ZipFile(filepath, 'r')
    except zipfile.BadZipFile:
        return -1

    info_list = my_zip.infolist()
    total_size = 0

    for info in info_list:
        if not info.filename.lower().endswith(
            ('.jpeg', '.jpg', '.png', '.gif')
        ):
            continue
        if '__macosx' in info.filename.lower():
            continue
        total_size += int(info.file_size)

    my_zip.close()

    return total_size


def convert_rar_to_zip(filepath: str) -> int:
    if not rarfile:
        return -1
    try:
        file_name = os.path.splitext(filepath)[0]
        temp_rar_file = file_name + ".rar"
        os.rename(filepath, temp_rar_file)
        my_rar = rarfile.RarFile(temp_rar_file, 'r')

    except (rarfile.BadRarFile, rarfile.NotRarFile):
        return -1

    new_zipfile = zipfile.ZipFile(filepath, 'w')
    dirpath = mkdtemp()

    filtered_files = list(filter(discard_zipfile_contents, sorted(my_rar.namelist())))
    for filename in filtered_files:
        my_rar.extract(filename, path=dirpath)
        new_zipfile.write(
            os.path.join(dirpath, filename.replace('\\', '/')),
            arcname=os.path.basename(filename)
        )

    my_rar.close()
    new_zipfile.close()

    os.remove(temp_rar_file)
    shutil.rmtree(dirpath, ignore_errors=True)
    return 0


def get_zip_fileinfo(filepath: str) -> Tuple[int, int]:
    try:
        my_zip = zipfile.ZipFile(filepath, 'r')
    except zipfile.BadZipFile:
        return -1, -1

    total_size = 0
    total_count = 0

    for info in my_zip.infolist():
        if not info.filename.lower().endswith(
                ('.jpeg', '.jpg', '.png', '.gif')
        ):
            continue
        if '__macosx' in info.filename.lower():
            continue
        total_size += int(info.file_size)
        total_count += 1

    my_zip.close()

    return total_size, total_count


def available_filename(root: str, filename: str) -> str:
    filename_full = os.path.join(root, filename)

    extra_text = 2
    if os.path.isfile(filename_full):
        while (
            os.path.isfile(
                os.path.splitext(filename_full)[0] + "-" + str(extra_text)
                + os.path.splitext(filename_full)[1])
        ):
            extra_text += 1
        return (
            os.path.splitext(filename)[0] + "-" + str(extra_text) + os.path.splitext(filename)[1]
        )
    else:
        return filename


def get_base_filename_string_from_gallery_data(gallery_data: GalleryData) -> str:
    if gallery_data.title:
        return gallery_data.title
    elif gallery_data.title_jpn:
        return gallery_data.title_jpn
    elif gallery_data.link:
        return gallery_data.link
    else:
        return ''


def translate_tag(tag: str) -> str:
    tag = tag.lower()
    tag = tag.replace(" ", "_")
    tag = tag.replace("characters:", "character:")
    tag = tag.replace("artists:", "artist:")
    tag = tag.replace("groups:", "group:")
    tag = tag.replace("parodies:", "parody:")
    tag = tag.replace("languages:", "language:")
    return tag


def translate_tag_list(tags: List[str]) -> List[str]:
    for i in range(len(tags)):
        tags[i] = tags[i].lower()
        tags[i] = tags[i].replace(" ", "_")
        tags[i] = tags[i].replace("characters:", "character:")
        tags[i] = tags[i].replace("artists:", "artist:")
        tags[i] = tags[i].replace("groups:", "group:")
        tags[i] = tags[i].replace("parodies:", "parody:")
        tags[i] = tags[i].replace("languages:", "language:")

    return tags


def clean_title(title: str) -> str:
    # Remove parenthesis
    adjusted_title = re.sub(r'\s+\(.+?\)$', r'', re.sub(r'\[.+?\]\s*', r'', title))
    # Remove non words, non whitespace
    # adjusted_title = re.sub(r'[^\w\s]', r' ', adjusted_title)
    return adjusted_title


def format_title_to_wanted_search(title: str) -> str:
    words_and_ws = re.sub(r'\W', ' ', title)
    words_and_ws_once = re.sub(r'\s+', ' ', words_and_ws)
    return words_and_ws_once


def artist_from_title(title: str) -> str:
    m = re.match(r'\[(.+?)\]\s*', title)
    if m:
        artist = m.group(1)
    else:
        artist = ''
    return artist


def str_to_int(number: str) -> Union[str, int]:
    return number or 0


def timestamp_or_zero(posted: datetime) -> float:
    if posted:
        return posted.timestamp()
    else:
        return 0.0


def compare_search_title_with_strings(original_title: str, titles: List[str]) -> bool:
    if not original_title:
        return False
    pattern = '.*?{}.*?'.format(re.sub(r'\s+', '.+', original_title.lower()))
    re_object = re.compile(pattern)
    for title in titles:
        # # This search is too simple:
        # if to_full_width(original_title.lower()) in to_full_width(title.lower()):
        #     return True
        match = re_object.match(title.lower())
        if match:
            return True
    return False


def get_scored_matches(word: str, possibilities: List[str], n: int = 3, cutoff: float = 0.6) -> List[Tuple[float, str]]:
    if not n > 0:
        raise ValueError("n must be > 0: %r" % (n,))
    if not (0.0 <= cutoff <= 1.0):
        raise ValueError("cutoff must be in [0.0, 1.0]: %r" % (cutoff,))
    result = []
    s: SequenceMatcher = SequenceMatcher()
    s.set_seq2(word)
    for x in possibilities:
        if not x:
            continue
        s.set_seq1(x)
        if s.real_quick_ratio() >= cutoff and s.quick_ratio() >= cutoff and s.ratio() >= cutoff:
            result.append((s.ratio(), x))

    # Move the best scorers to head of list
    result = heapq.nlargest(n, result)
    # Strip scores for the best n matches
    return result


def get_title_from_path(path: str) -> str:
    return re.sub('[_]', ' ', os.path.splitext(os.path.basename(path))[0])


needed_keys = [
    'gid', 'token', 'title', 'title_jpn',
    'category', 'uploader', 'posted', 'filecount',
    'filesize', 'expunged', 'rating', 'fjord',
    'hidden', 'dl_type', 'comment', 'thumbnail_url',
    'public', 'provider', 'gallery_container_gid',
    'status', 'origin', 'reason'
]


def get_dict_allowed_fields(gallery_data: GalleryData) -> Dict[str, Any]:

    gallery_dict = {}

    for key in needed_keys:
        if hasattr(gallery_data, key) and getattr(gallery_data, key) is not None:
            gallery_dict[key] = getattr(gallery_data, key)

    return gallery_dict


def previous_and_next(some_iterable: typing.Sequence[Optional[T]]) -> typing.Iterator[Tuple[Optional[T], Optional[T], Optional[T]]]:
    prevs, items, nexts = tee(some_iterable, 3)
    prevs = chain([None], prevs)
    nexts = chain(islice(nexts, 1, None), [None])
    return zip(prevs, items, nexts)


def unescape(text: str) -> str:
    def fixup(m: typing.Match):
        in_text = m.group(0)
        if in_text[:2] == "&#":
            # character reference
            try:
                if in_text[:3] == "&#x":
                    return chr(int(in_text[3:-1], 16))
                else:
                    return chr(int(in_text[2:-1]))
            except ValueError:
                pass
        else:
            # named entity
            try:
                in_text = chr(html.entities.name2codepoint[in_text[1:-1]])
            except KeyError:
                pass
        return in_text  # leave as is

    return re.sub(r"&#?\w+;", fixup, text)


def get_thread_status() -> List[Tuple[Tuple[str, str, str], bool]]:
    info_list = []

    thread_list = threading.enumerate()
    for thread_info in setup.GlobalInfo.worker_threads:
        info_list.append((thread_info, any([thread.name == thread_info[0] for thread in thread_list])))

    return info_list


def get_schedulers_status(schedulers: List['BaseScheduler']) -> List[Tuple[str, bool, Optional[datetime], str, Optional[datetime]]]:
    info_list = []

    for scheduler in schedulers:

        if scheduler.last_run:
            next_run: Optional[datetime] = scheduler.last_run + timedelta(seconds=scheduler.timer)
        else:
            next_run = None

        info_list.append(
            (
                scheduler.thread_name,
                scheduler.is_running(),
                scheduler.last_run,
                str(scheduler.timer) + ", " + str(scheduler.timer / 60),
                next_run
            )
        )

    return info_list


def get_thread_status_bool() -> Dict[str, bool]:
    info_dict = {}

    thread_list = threading.enumerate()
    for thread_info in setup.GlobalInfo.worker_threads:
        if any([thread.name == thread_info[0] for thread in thread_list]):
            info_dict[thread_info[0]] = True
        else:
            info_dict[thread_info[0]] = False

    return info_dict


def check_for_running_threads() -> bool:
    thread_list = threading.enumerate()
    for thread_name in setup.GlobalInfo.worker_threads:
        if any([thread.name == thread_name[0] for thread in thread_list]):
            return True
    return False


def thread_exists(thread_name: str) -> bool:
    thread_list = threading.enumerate()
    for thread in thread_list:
        if thread_name == thread.name:
            return True

    return False


def module_exists(module_name: str) -> bool:
    try:
        __import__(module_name)
    except ImportError:
        return False
    else:
        return True


class StandardFormatter(logging.Formatter):
    def formatException(self, exc_info: Any) -> str:
        result = super(StandardFormatter, self).formatException(exc_info)
        return result

    def format(self, record: logging.LogRecord) -> str:
        s = super(StandardFormatter, self).format(record)
        s += '[0m'
        return s


def send_pushover_notification(user_key: str, token: str, message: str, title: str = "Alert", sound: str = '', device: str = '', attachment: str = '') -> bool:

    payload = {
        'token': token,
        'user': user_key,
        'message': message,
        'title': title,
        'sound': sound,
        'device': device
    }

    if attachment:
        files = {'attachment': open(attachment, 'rb')}
        r = requests.post(PUSHOVER_API_URL, data=payload, files=files)
    else:
        r = requests.post(PUSHOVER_API_URL, data=payload)

    if r.status_code == 200:
        return True
    else:
        return False


def request_with_retries(
        url: str, request_dict: Dict[str, Any],
        post: bool = False, retries: int = 3,
        logger: OptionalLogger = None) -> Optional[requests.models.Response]:
    for retry_count in range(retries):
        try:
            if post:
                r = requests.post(url, **request_dict)
            else:
                r = requests.get(url, **request_dict)
            return r

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if retry_count < retries - 1:
                if logger:
                    logger.warning("Request failed, retry: {} of {}: {}".format(retry_count, retries, str(e)))
                continue
            else:
                if logger:
                    logger.error("Failed to reach URL: {}".format(url))
                return None
    return None


def get_filename_from_cd(cd: str):
    """
    Get filename from content-disposition
    """
    if not cd:
        return None
    fname = re.findall('filename=(.+)', cd)
    if len(fname) == 0:
        return None
    return fname[0]
