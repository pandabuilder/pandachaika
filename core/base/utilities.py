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
import fnmatch
from datetime import timedelta, datetime
from difflib import SequenceMatcher
from itertools import tee, islice, chain
from tempfile import mkdtemp
from typing import Union, Optional, Any

from core.base.types import GalleryData

try:
    import rarfile
except ImportError:
    rarfile = None

try:
    import py7zr
except ImportError:
    py7zr = None  # type: ignore

import requests

from core.base import setup

if typing.TYPE_CHECKING:
    from core.workers.schedulers import BaseScheduler
    from core.base.types import ProviderSettings

logger = logging.getLogger(__name__)

PUSHOVER_API_URL = 'https://api.pushover.net/1/messages.json'

ZIP_CONTAINER_REGEX = re.compile(r'(\.zip|\.cbz)$', re.IGNORECASE)
IMAGES_REGEX = re.compile(r'(\.jpeg|\.jpg|\.png|\.gif)$', re.IGNORECASE)
ZIP_CONTAINER_EXTENSIONS = [".zip", ".cbz"]


# The idea of this class is to contain certain methods, to not have to pass arguments that are global.
class GeneralUtils:

    def __init__(self, global_settings: 'setup.Settings') -> None:
        self.settings = global_settings

    def discard_by_tag_list(self, tag_list: Optional[list[str]], force_check: bool = False) -> Optional[list[str]]:

        if not force_check and self.settings.update_metadata_mode:
            return None
        if tag_list is None:
            return None
        found_tags = set(self.settings.discard_tags).intersection(tag_list)
        if found_tags:
            return list(found_tags)
        return None

    def get_torrent(self, torrent_url: str, cookies: dict[str, Any], convert_to_base64: bool = False) -> Union[str, bytes]:

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


def discard_by_tag_list(tag_list: list[str], discard_tags: list[str]):
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
    # Update: Limit should be 256 bytes length (ext4).
    try:
        formatted_path = filepath.encode('utf-8')[0:251].decode('utf-8')
    except UnicodeDecodeError:
        formatted_path = filepath[0:251]
    return formatted_path


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
    with open(filename, "rb") as file_to_check:
        for eachLine in file_to_check:
            prev = zlib.crc32(eachLine, prev)
    return "%X" % (prev & 0xFFFFFFFF)


def sha1_from_file_object(fp: Union[typing.IO, str], close_object: bool = False):

    if isinstance(fp, str):
        file_object: Union[typing.IO, typing.BinaryIO] = open(fp, "rb")
    else:
        file_object = fp

    block_size = 65536
    hasher = hashlib.sha1()
    buf = file_object.read(block_size)
    while len(buf) > 0:
        hasher.update(buf)
        buf = file_object.read(block_size)
    if close_object or isinstance(fp, str):
        file_object.close()
    return hasher.hexdigest()


T = typing.TypeVar('T')


def chunks(sequence: typing.Sequence[T], n: int):
    """ Yield successive n-sized chunks from l."""
    for i in range(0, len(sequence), n):
        yield sequence[i:i + n]


def zfillrepl(matchobj: typing.Match):
    return matchobj.group(0).zfill(3)


def zfill_to_three(namefile: str) -> str:
    filename = os.path.splitext(os.path.basename(namefile))[0]

    filename = re.sub(r'\d+', zfillrepl, filename)

    return filename.lower()


def accept_images_only(name: str) -> Optional[str]:
    r = re.compile(r'(\.jpeg|\.jpg|\.png|\.gif)$', re.IGNORECASE)
    if r.search(name) and '__MACOSX' not in name:
        return name
    else:
        return None


def discard_zipfile_extra_files(name: str) -> Optional[str]:
    if '__MACOSX' not in name:
        return name
    else:
        return None


def accept_images_only_info(fileinfo: zipfile.ZipInfo) -> Optional[zipfile.ZipInfo]:
    r = re.compile(r'(\.jpeg|\.jpg|\.png|\.gif)$', re.IGNORECASE)
    if r.search(fileinfo.filename) and '__MACOSX' not in fileinfo.filename:
        return fileinfo
    else:
        return None


def discard_zipfile_extra_files_info(fileinfo: zipfile.ZipInfo) -> Optional[zipfile.ZipInfo]:
    if '__MACOSX' not in fileinfo.filename:
        return fileinfo
    else:
        return None


# Allows 1 nested zip level, returns tuple: filename, containing zip(nested), extracted_name (adds nested zipfile)
def get_images_from_zip(current_zip: zipfile.ZipFile) -> list[tuple[str, Optional[str], str]]:
    filtered_files = list(filter(discard_zipfile_extra_files, sorted(current_zip.namelist(), key=zfill_to_three)))

    nested_files: list[tuple[str, Optional[str], str]] = []

    for current_file in filtered_files:
        if IMAGES_REGEX.search(current_file):
            nested_files.append((current_file, None, current_file))
        elif ZIP_CONTAINER_REGEX.search(current_file):
            with current_zip.open(current_file) as current_nested_zip_file:
                try:
                    nested_zip = zipfile.ZipFile(current_nested_zip_file, 'r')
                except zipfile.BadZipFile:
                    continue
                nested_filtered_files = list(filter(accept_images_only, sorted(nested_zip.namelist(), key=zfill_to_three)))
                found_files = [
                    (x, current_file, "{}_{}".format(os.path.splitext(current_file)[0], x)) for x in nested_filtered_files
                ]
                nested_files.extend(found_files)
                nested_zip.close()

    return nested_files


def filecount_in_zip(filepath: str) -> int:
    try:
        my_zip = zipfile.ZipFile(filepath, 'r')
    except zipfile.BadZipFile:
        return 0

    total_count = 0

    filtered_files = list(filter(discard_zipfile_extra_files, my_zip.namelist()))

    for current_file in filtered_files:
        if IMAGES_REGEX.search(current_file):
            total_count += 1
        elif ZIP_CONTAINER_REGEX.search(current_file):
            with my_zip.open(current_file) as current_nested_zip_file:
                try:
                    nested_zip = zipfile.ZipFile(current_nested_zip_file, 'r')
                except zipfile.BadZipFile:
                    continue
                nested_files = list(filter(accept_images_only, nested_zip.namelist()))
                total_count += len(nested_files)
                nested_zip.close()

    my_zip.close()

    return total_count


def get_zip_filesize(filepath: str) -> int:
    try:
        my_zip = zipfile.ZipFile(filepath, 'r')
    except zipfile.BadZipFile:
        return -1

    total_size = 0

    filtered_files = list(filter(discard_zipfile_extra_files_info, sorted(my_zip.infolist(), key=lambda x: x.filename)))

    for current_file_info in filtered_files:
        if IMAGES_REGEX.search(current_file_info.filename):
            total_size += int(current_file_info.file_size)
        elif ZIP_CONTAINER_REGEX.search(current_file_info.filename):
            with my_zip.open(current_file_info.filename) as current_nested_zip_file:
                try:
                    nested_zip = zipfile.ZipFile(current_nested_zip_file, 'r')
                except zipfile.BadZipFile:
                    continue
                nested_files = list(filter(accept_images_only_info, sorted(nested_zip.infolist(), key=lambda x: x.filename)))
                total_size += sum([x.file_size for x in nested_files])
                nested_zip.close()

    my_zip.close()

    return total_size


def convert_rar_to_zip(filepath: str) -> int:
    if not rarfile:
        return -1
    try:
        file_name = os.path.splitext(filepath)[0]
        temp_rar_file = file_name + ".tar"
        os.rename(filepath, temp_rar_file)
        my_rar = rarfile.RarFile(temp_rar_file, 'r')

    except (rarfile.BadRarFile, rarfile.NotRarFile):
        return -1

    new_zipfile = zipfile.ZipFile(filepath, 'w')
    dirpath = mkdtemp()

    filtered_files = list(filter(discard_zipfile_extra_files, sorted(my_rar.namelist())))
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


def convert_7z_to_zip(filepath: str) -> int:
    if not py7zr:
        return -1
    try:
        file_name = os.path.splitext(filepath)[0]
        temp_7z_file = file_name + ".t7z"
        os.rename(filepath, temp_7z_file)
        my_7z = py7zr.SevenZipFile(temp_7z_file, 'r')

    except py7zr.Bad7zFile:
        return -1

    new_zipfile = zipfile.ZipFile(filepath, 'w')
    dirpath = mkdtemp()

    filtered_files = list(filter(discard_zipfile_extra_files, sorted(my_7z.getnames())))

    my_7z.extract(targets=filtered_files, path=dirpath)

    for filename in filtered_files:
        new_zipfile.write(
            os.path.join(dirpath, filename.replace('\\', '/')),
            arcname=os.path.basename(filename)
        )

    my_7z.close()
    new_zipfile.close()

    os.remove(temp_7z_file)
    shutil.rmtree(dirpath, ignore_errors=True)
    return 0


def check_and_convert_to_zip(filepath: str) -> tuple[str, int]:
    try:
        zipfile.ZipFile(filepath, 'r')
        return 'zip', 0
    except zipfile.BadZipFile as e:
        if str(e) != 'File is not a zip file':
            return 'zip', 1
    try:
        rarfile.RarFile(filepath, 'r')
        convert_rar_to_zip(filepath)
        return 'rar', 2
    except rarfile.NotRarFile:
        pass
    try:
        py7zr.SevenZipFile(filepath, 'r')
        convert_7z_to_zip(filepath)
        return '7z', 2
    except py7zr.exceptions.Bad7zFile as e:
        if str(e) != 'not a 7z file':
            return 'unknown', 1
    return 'unknown', 1
    # zipfile.BadZipFile: File is not a zip file
    # rarfile.NotRarFile: Not a RAR file
    # py7zr.exceptions.Bad7zFile: not a 7z file


def get_zip_fileinfo(filepath: str) -> tuple[int, int]:
    try:
        my_zip = zipfile.ZipFile(filepath, 'r')
    except zipfile.BadZipFile:
        return -1, -1

    total_size = 0
    total_count = 0

    filtered_files = list(filter(discard_zipfile_extra_files_info, sorted(my_zip.infolist(), key=lambda x: x.filename)))

    for current_file_info in filtered_files:
        if IMAGES_REGEX.search(current_file_info.filename):
            total_size += int(current_file_info.file_size)
            total_count += 1
        elif ZIP_CONTAINER_REGEX.search(current_file_info.filename):
            with my_zip.open(current_file_info.filename) as current_nested_zip_file:
                try:
                    nested_zip = zipfile.ZipFile(current_nested_zip_file, 'r')
                except zipfile.BadZipFile:
                    continue
                nested_files = list(filter(accept_images_only_info, sorted(nested_zip.infolist(), key=lambda x: x.filename)))
                total_count += len(nested_files)
                total_size += sum([x.file_size for x in nested_files])
                nested_zip.close()

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


def translate_tag_list(tags: list[str]) -> list[str]:
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
    adjusted_title = re.sub(r'\s+\(.+?\)$', r'', re.sub(r'\[.+?\]\s*', r'', title)).replace("_", "")
    # Remove non words, non whitespace
    # adjusted_title = re.sub(r'[^\w\s]', r' ', adjusted_title)
    return adjusted_title


def format_title_to_wanted_search(title: str) -> str:
    words_and_ws = re.sub(r'\W', ' ', title)
    words_and_ws_once = re.sub(r'\s+', ' ', words_and_ws)
    return words_and_ws_once


def file_matches_any_filter(title: str, filters: list[str]) -> bool:
    for filename_filter in filters:
        if fnmatch.fnmatch(title, filename_filter):
            return True
    return False


def artist_from_title(title: str) -> str:
    m = re.match(r'\[(.+?)\]\s*', title)
    if m:
        artist = m.group(1)
    else:
        artist = ''
    return artist


def str_to_int(number: Optional[str]) -> Union[str, int]:
    return number or 0


def timestamp_or_zero(posted: Optional[datetime]) -> float:
    if posted:
        return posted.timestamp()
    else:
        return 0.0


def timestamp_or_null(posted: Optional[datetime]) -> Optional[int]:
    if posted:
        return int(posted.timestamp())
    else:
        return None


def compare_search_title_with_strings(original_title: str, titles: list[str]) -> bool:
    if not original_title:
        return False
    pattern = '.*?{}.*?'.format(re.sub(r'\\\s+', '.+', re.escape(original_title.lower())))
    re_object = re.compile(pattern)
    for title in titles:
        # # This search is too simple:
        # if to_full_width(original_title.lower()) in to_full_width(title.lower()):
        #     return True
        match = re_object.match(title.lower())
        if match:
            return True
    return False


def get_scored_matches(word: str, possibilities: list[str], n: int = 3, cutoff: float = 0.6) -> list[tuple[float, str]]:
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
    'public', 'provider', 'gallery_container_gid', 'magazine_gid',
    'status', 'origin', 'reason',
    'parent_gallery_gid', 'first_gallery_gid', 'provider_metadata'
]


def get_dict_allowed_fields(gallery_data: GalleryData) -> dict[str, Any]:

    gallery_dict = {}

    for key in needed_keys:
        if hasattr(gallery_data, key) and getattr(gallery_data, key) is not None:
            gallery_dict[key] = getattr(gallery_data, key)

    return gallery_dict


def previous_and_next(some_iterable: typing.Sequence[Optional[T]]) -> typing.Iterator[tuple[Optional[T], Optional[T], Optional[T]]]:
    prevs, items, nexts = tee(some_iterable, 3)
    prevs = chain([None], prevs)
    nexts = chain(islice(nexts, 1, None), [None])
    return zip(prevs, items, nexts)


def unescape(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    else:
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


def get_thread_status() -> list[tuple[tuple[str, str, str], bool]]:
    info_list = []
    thread_names = set()

    thread_list = threading.enumerate()
    for thread_info in setup.GlobalInfo.worker_threads:
        info_list.append((thread_info, any([thread_info[0] == thread.name for thread in thread_list])))
        thread_names.add(thread_info[0])

    for thread_data in thread_list:
        if thread_data.name not in thread_names:
            info_list.append(((thread_data.name, 'None', 'other'), thread_data.is_alive()))

    return info_list


def get_schedulers_status(schedulers: typing.Sequence[Optional['BaseScheduler']]) -> list[tuple[str, bool, Optional[datetime], str, Optional[datetime]]]:
    info_list = []

    for scheduler in schedulers:
        if scheduler:
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
        url: str, request_dict: dict[str, Any],
        post: bool = False, retries: int = 3) -> Optional[requests.models.Response]:
    for retry_count in range(retries):
        try:
            if post:
                r = requests.post(url, **request_dict)
            else:
                r = requests.get(url, **request_dict)
            return r

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if retry_count < retries - 1:
                logger.warning("Request failed, attempt: {} of {}: {}".format(retry_count + 1, retries, str(e)))
                continue
            else:
                logger.error("Request failed, attempt: {} of {}: Unreachable URL: {}".format(retry_count + 1, retries, url))
                return None
    return None


def construct_request_dict(settings: 'setup.Settings', own_settings: 'ProviderSettings') -> dict[str, Any]:
    request_dict = {
        'headers': settings.requests_headers,
        'cookies': own_settings.cookies,
        'timeout': own_settings.timeout_timer,
    }
    if own_settings.proxies:
        request_dict['proxies'] = own_settings.proxies
    return request_dict


def get_filename_from_cd(cd: typing.Optional[str] = None):
    """
    Get filename from content-disposition
    """
    if not cd:
        return None
    fname = re.findall('filename=(.+)', cd)
    if len(fname) == 0:
        return None
    return fname[0]


def clamp(n, minn, maxn):
    return max(min(maxn, n), minn)


def remove_archive_extensions(filename: str):
    for extension in ZIP_CONTAINER_EXTENSIONS:
        filename = filename.replace(extension, "")
    return filename
