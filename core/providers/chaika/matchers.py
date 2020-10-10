import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional, List
from urllib.parse import urljoin

from core.base.matchers import Matcher
from core.base.types import MatchesValues, DataDict, GalleryData
from core.base.utilities import (
    filecount_in_zip,
    get_zip_filesize, clean_title,
    request_with_retries, construct_request_dict, calc_crc32)
from . import constants
from core.base.comparison import get_gallery_closer_title_from_gallery_values

logger = logging.getLogger(__name__)


class BaseExactMatcher(Matcher):

    def format_to_search_title(self, file_name: str) -> str:
        return os.path.join(self.settings.MEDIA_ROOT, file_name)

    def format_to_compare_title(self, file_name: str) -> str:
        return self.get_title_from_path(file_name)

    def get_closer_match(self, file_path: str) -> int:

        title_to_search = self.format_to_search_title(file_path)

        if self.search_method(title_to_search):
            if self.time_to_wait_after_compare > 0:
                time.sleep(self.time_to_wait_after_compare)
            galleries_data = self.values_array
            self.values_array = []
            if not galleries_data:
                return 0
            galleries_data = [x for x in galleries_data if not self.general_utils.discard_by_tag_list(x.tags)]
            if not galleries_data:
                return 0
            result = get_gallery_closer_title_from_gallery_values(
                self.format_to_compare_title(file_path), galleries_data, self.default_cutoff)
            if result.match_title == '':
                return 0
            self.match_link = result.match_link
            self.match_count = len(self.gallery_links)
            self.match_title = result.match_title
            self.match_values = result.match_values
            return 1
        else:
            return 0

    def create_closer_matches_values(self, zip_path: str, cutoff: Optional[float] = None, max_matches: int = 20) -> List[MatchesValues]:

        self.values_array = []
        results: List[MatchesValues] = []

        if self.search_method(self.format_to_search_title(zip_path)):
            if self.time_to_wait_after_compare > 0:
                time.sleep(self.time_to_wait_after_compare)
            galleries_data = self.values_array
            self.values_array = []
            if galleries_data:
                galleries_data = [x for x in galleries_data if not self.general_utils.discard_by_tag_list(x.tags)]
                # We don't call get_list_closer_gallery_titles_from_dict
                # because we assume that a hash match is correct already
                if galleries_data:
                    self.values_array = galleries_data
                    results = [(gallery.title or gallery.title_jpn or '', gallery, 1) for gallery in galleries_data]
        return results

    def format_match_values(self) -> Optional[DataDict]:

        if not self.match_values:
            return None
        self.match_gid = self.match_values.gid
        self.match_provider = self.match_values.provider
        values = {
            'title': self.match_values.title,
            'title_jpn': self.match_values.title_jpn,
            'zipped': self.file_path,
            'crc32': self.crc32,
            'match_type': self.found_by,
            'filesize': get_zip_filesize(os.path.join(self.settings.MEDIA_ROOT, self.file_path)),
            'filecount': filecount_in_zip(os.path.join(self.settings.MEDIA_ROOT, self.file_path)),
            'source_type': self.match_provider or self.provider
        }

        return values


class HashMatcher(BaseExactMatcher):

    name = 'hash'
    provider = constants.provider_name
    type = 'hash'
    time_to_wait_after_compare = 0
    default_cutoff = 0.0

    def search_method(self, title_to_search: str) -> bool:
        return self.compare_by_hash(title_to_search)

    def compare_by_hash(self, zip_path: str) -> bool:

        if not os.path.isfile(zip_path):
            return False

        crc32 = calc_crc32(zip_path)

        api_url = urljoin(self.own_settings.url, constants.api_path)
        logger.info("Querying URL: {}".format(api_url))

        request_dict = construct_request_dict(self.settings, self.own_settings)
        request_dict['params'] = {'match': True, 'crc32': crc32}

        response = request_with_retries(
            api_url,
            request_dict,
            post=False, retries=3
        )

        if not response:
            logger.info("Got no response from server")
            return False

        response_data = response.json()

        matches_links = set()

        if 'error' in response_data:
            logger.info("Got error from server: {}".format(response_data['error']))
            return False

        for gallery in response_data:
            if 'link' in gallery:
                matches_links.add(gallery['link'])
                if 'gallery_container' in gallery and gallery['gallery_container']:
                    if self.settings.gallery_model:
                        gallery_container = self.settings.gallery_model.objects.filter(
                            gid=gallery['gallery_container'], provider=gallery['provider']
                        )
                        if gallery_container:
                            gallery['gallery_container_gid'] = gallery_container.first().gid
                if 'posted' in gallery:
                    if gallery['posted'] != 0:
                        gallery['posted'] = datetime.fromtimestamp(int(gallery['posted']), timezone.utc)
                    else:
                        gallery['posted'] = None
                self.values_array.append(GalleryData(**gallery))

        self.gallery_links = list(matches_links)
        if len(self.gallery_links) > 0:
            self.found_by = self.name
            return True
        else:
            return False


class TitleMatcher(Matcher):

    name = 'title'
    provider = constants.provider_name
    type = 'title'
    time_to_wait_after_compare = 0
    default_cutoff = 0.6

    def get_metadata_after_matching(self) -> List[GalleryData]:
        return self.values_array

    def format_to_search_title(self, file_name: str) -> str:
        if file_name.endswith('.zip'):
            return clean_title(self.get_title_from_path(file_name))
        else:
            return clean_title(file_name)

    def format_to_compare_title(self, file_name: str) -> str:
        if file_name.endswith('.zip'):
            return clean_title(self.get_title_from_path(file_name))
        else:
            return clean_title(file_name)

    def search_method(self, title_to_search: str) -> bool:
        return self.compare_by_title(title_to_search)

    def format_match_values(self) -> Optional[DataDict]:

        if not self.match_values:
            return None
        self.match_gid = self.match_values.gid
        self.match_provider = self.match_values.provider
        values = {
            'title': self.match_values.title,
            'title_jpn': self.match_values.title_jpn,
            'zipped': self.file_path,
            'crc32': self.crc32,
            'match_type': self.found_by,
            'filesize': get_zip_filesize(os.path.join(self.settings.MEDIA_ROOT, self.file_path)),
            'filecount': filecount_in_zip(os.path.join(self.settings.MEDIA_ROOT, self.file_path)),
            'source_type': self.match_provider or self.provider
        }

        return values

    def compare_by_title(self, gallery_title: str) -> bool:

        api_url = urljoin(self.own_settings.metadata_url, constants.api_path)
        logger.info("Querying URL: {}".format(api_url))

        request_dict = construct_request_dict(self.settings, self.own_settings)
        request_dict['params'] = {'match': True, 'title': gallery_title}

        response = request_with_retries(
            api_url,
            request_dict,
            post=False, retries=3
        )

        if not response:
            logger.info("Got no response from server")
            return False

        response_data = response.json()

        matches_links = set()

        if 'error' in response_data:
            logger.info("Got error from server: {}".format(response_data['error']))
            return False

        for gallery in response_data:
            if 'link' in gallery:
                matches_links.add(gallery['link'])
                if 'gallery_container' in gallery and gallery['gallery_container']:
                    if self.settings.gallery_model:
                        gallery_container = self.settings.gallery_model.objects.filter(
                            gid=gallery['gallery_container'], provider=gallery['provider']
                        )
                        if gallery_container:
                            gallery['gallery_container_gid'] = gallery_container.first().gid
                if 'posted' in gallery:
                    if gallery['posted'] != 0:
                        gallery['posted'] = datetime.fromtimestamp(int(gallery['posted']), timezone.utc)
                    else:
                        gallery['posted'] = None
                self.values_array.append(GalleryData(**gallery))

        self.gallery_links = list(matches_links)
        if len(self.gallery_links) > 0:
            self.found_by = self.name
            return True
        else:
            return False


class FileSizeMatcher(BaseExactMatcher):

    name = 'size'
    provider = constants.provider_name
    type = 'size'
    time_to_wait_after_compare = 0
    default_cutoff = 0.0

    def search_method(self, title_to_search: str) -> bool:
        return self.compare_by_size(title_to_search)

    def compare_by_size(self, zip_path: str) -> bool:

        if not os.path.isfile(zip_path):
            return False

        filesize = get_zip_filesize(zip_path)

        api_url = urljoin(self.own_settings.metadata_url, constants.api_path)
        logger.info("Querying URL: {}".format(api_url))

        request_dict = construct_request_dict(self.settings, self.own_settings)
        request_dict['params'] = {'match': True, 'filesize_from': filesize, 'filesize_to': filesize}

        response = request_with_retries(
            api_url,
            request_dict,
            post=False, retries=3
        )

        if not response:
            logger.info("Got no response from server")
            return False

        response_data = response.json()

        matches_links = set()

        if 'error' in response_data:
            logger.info("Got error from server: {}".format(response_data['error']))
            return False

        for gallery in response_data:
            if 'link' in gallery:
                matches_links.add(gallery['link'])
                if 'gallery_container' in gallery and gallery['gallery_container']:
                    if self.settings.gallery_model:
                        gallery_container = self.settings.gallery_model.objects.filter(
                            gid=gallery['gallery_container'], provider=gallery['provider']
                        )
                        if gallery_container:
                            gallery['gallery_container_gid'] = gallery_container.first().gid
                if 'posted' in gallery:
                    if gallery['posted'] != 0:
                        gallery['posted'] = datetime.fromtimestamp(int(gallery['posted']), timezone.utc)
                    else:
                        gallery['posted'] = None
                self.values_array.append(GalleryData(**gallery))

        self.gallery_links = list(matches_links)
        if len(self.gallery_links) > 0:
            self.found_by = self.name
            return True
        else:
            return False


API = (
    HashMatcher,
    TitleMatcher,
    FileSizeMatcher,
)
