import os
import re
import zipfile
from typing import Optional, List

import requests
import time

from core.base.matchers import Matcher
from core.base.types import MatchesValues, DataDict
from core.base.utilities import (
    filecount_in_zip,
    get_zip_filesize,
    sha1_from_file_object, clean_title, construct_request_dict, get_images_from_zip)
from core.providers.panda.utilities import link_from_gid_token_fjord, get_gid_token_from_link, SearchHTMLParser, \
    GalleryHTMLParser
from . import constants


class ImageMatcher(Matcher):

    name = 'image'
    provider = constants.provider_name
    type = 'image'
    time_to_wait_after_compare = 10
    default_cutoff = 0

    def format_to_search_title(self, file_name: str) -> str:
        return os.path.join(self.settings.MEDIA_ROOT, file_name)

    def format_to_compare_title(self, file_name: str) -> str:
        return self.get_title_from_path(file_name)

    def search_method(self, title_to_search: str) -> bool:
        return self.compare_by_image(title_to_search, False)

    def create_closer_matches_values(self, zip_path: str, cutoff: Optional[float] = None, max_matches: int = 20) -> List[MatchesValues]:

        self.values_array = []
        results: List[MatchesValues] = []

        if self.search_method(self.format_to_search_title(zip_path)):
            if self.time_to_wait_after_compare > 0:
                time.sleep(self.time_to_wait_after_compare)
            galleries_data = self.get_metadata_after_matching()
            if galleries_data:
                galleries_data = [x for x in galleries_data if not self.general_utils.discard_by_tag_list(x.tags)]
                # We don't call get_list_closer_gallery_titles_from_dict
                # because we assume that a image match is correct already
                if galleries_data:
                    self.values_array = galleries_data
                    results = [(gallery.title or gallery.title_jpn or '', gallery, 1) for gallery in galleries_data]
        return results

    def format_match_values(self) -> Optional[DataDict]:

        if not self.match_values:
            return None

        self.match_gid = self.match_values.gid
        values = {
            'title': self.match_title,
            'title_jpn': self.match_values.title_jpn,
            'zipped': self.file_path,
            'crc32': self.crc32,
            'match_type': self.found_by,
            'filesize': get_zip_filesize(os.path.join(self.settings.MEDIA_ROOT, self.file_path)),
            'filecount': filecount_in_zip(os.path.join(self.settings.MEDIA_ROOT, self.file_path)),
            'source_type': self.provider
        }
        return values

    def compare_by_image(self, zip_path: str, only_cover: bool) -> bool:

        if os.path.splitext(zip_path)[1] != '.zip':
            self.gallery_links = []
            return False

        try:
            my_zip = zipfile.ZipFile(zip_path, 'r')
        except (zipfile.BadZipFile, NotImplementedError):
            self.gallery_links = []
            return False

        filtered_files = get_images_from_zip(my_zip)

        if not filtered_files:
            self.gallery_links = []
            return False

        first_file = filtered_files[0]

        if first_file[1] is None:
            with my_zip.open(first_file[0]) as current_img:
                first_file_sha1 = sha1_from_file_object(current_img)
        else:
            with my_zip.open(first_file[1]) as current_zip:
                with zipfile.ZipFile(current_zip) as my_nested_zip:
                    with my_nested_zip.open(first_file[0]) as current_img:
                        first_file_sha1 = sha1_from_file_object(current_img)

        payload = {'f_shash': first_file_sha1,
                   'fs_from': os.path.basename(first_file[0]),
                   'fs_covers': 1 if only_cover else 0,
                   'fs_similar': 0}

        request_dict = construct_request_dict(self.settings, self.own_settings)
        request_dict['params'] = payload

        r = requests.get(
            constants.ex_page,
            **request_dict
        )

        my_zip.close()

        parser = SearchHTMLParser()
        parser.feed(r.text)

        self.gallery_links = list(parser.galleries)

        if len(self.gallery_links) > 0:
            self.found_by = self.name
            return True
        else:
            return False


class CoverMatcher(ImageMatcher):

    name = 'cover'

    def search_method(self, title_to_search: str) -> bool:
        return self.compare_by_image(title_to_search, True)


class TitleMatcher(Matcher):

    name = 'title'
    provider = constants.provider_name
    type = 'title'
    time_to_wait_after_compare = 10
    default_cutoff = 0.7

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
        values = {
            'title': self.match_title,
            'title_jpn': self.match_values.title_jpn,
            'zipped': self.file_path,
            'crc32': self.crc32,
            'match_type': self.found_by,
            'filesize': get_zip_filesize(os.path.join(self.settings.MEDIA_ROOT, self.file_path)),
            'filecount': filecount_in_zip(os.path.join(self.settings.MEDIA_ROOT, self.file_path)),
            'source_type': self.provider
        }
        return values

    def compare_by_title(self, image_title: str) -> bool:

        filters = {'f_search': '"' + image_title + '"'}

        request_dict = construct_request_dict(self.settings, self.own_settings)
        request_dict['params'] = filters

        r = requests.get(
            constants.ex_page,
            **request_dict
        )

        parser = SearchHTMLParser()
        parser.feed(r.text)

        self.gallery_links = list(parser.galleries)

        if len(self.gallery_links) > 0:
            self.found_by = self.name
            return True
        else:
            return False


class TitleGoogleMatcher(TitleMatcher):
    name = 'google'

    def search_method(self, title_to_search: str) -> bool:
        return self.compare_by_title_google(title_to_search)

    def compare_by_title_google(self, title: str) -> bool:

        payload = {'q': 'site:e-hentai.org ' + title}

        request_dict = construct_request_dict(self.settings, self.own_settings)
        request_dict['params'] = payload

        r = requests.get(
            "https://www.google.com/search",
            **request_dict
        )

        matches_links = set()

        m = re.finditer(r'(ex|g\.e-|e-)hentai\.org/g/(\d+)/(\w+)', r.text)

        if m:
            for match in m:
                matches_links.add(
                    self.get_final_link_from_link(
                        link_from_gid_token_fjord(match.group(2), match.group(3), False)
                    )
                )

        m2 = re.finditer(
            r'(ex|g\.e-|e-)hentai\.org/gallerytorrents\.php\?gid=(\d+)&t=(\w+)/', r.text)

        if m2:
            for match in m2:
                matches_links.add(
                    self.get_final_link_from_link(
                        link_from_gid_token_fjord(match.group(2), match.group(3), False)
                    )
                )

        self.gallery_links = list(matches_links)
        if len(self.gallery_links) > 0:
            self.found_by = self.name
            return True

        else:
            return False

    def get_final_link_from_link(self, link: str) -> str:

        time.sleep(self.own_settings.wait_timer)
        gallery_gid, gallery_token = get_gid_token_from_link(link)
        gallery_link = link_from_gid_token_fjord(gallery_gid, gallery_token, True)

        request_dict = construct_request_dict(self.settings, self.own_settings)

        gallery_page_text = requests.get(
            gallery_link,
            **request_dict
        ).text

        if 'Gallery Not Available' in gallery_page_text:
            return gallery_link
        else:
            gallery_parser = GalleryHTMLParser()
            gallery_parser.feed(gallery_page_text)
            if gallery_parser.found_non_final_gallery == 2 and gallery_parser.non_final_gallery:
                return self.get_final_link_from_link(gallery_parser.non_final_gallery)
        return gallery_link


API = (
    CoverMatcher,
    ImageMatcher,
    TitleMatcher,
    TitleGoogleMatcher,
)
