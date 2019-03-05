import typing
from datetime import datetime, timezone

import re
from html.parser import HTMLParser

from bs4 import BeautifulSoup

from core.base.types import DataDict, GalleryData
from core.base.utilities import unescape, translate_tag_list
from . import constants

if typing.TYPE_CHECKING:
    from viewer.models import Gallery


def map_external_gallery_data_to_internal(gallery_data: DataDict) -> GalleryData:
    internal_gallery_data = GalleryData(
        gallery_data['gid'],
        token=gallery_data['token'],
        archiver_key=gallery_data['archiver_key'],
        title=unescape(gallery_data['title']),
        title_jpn=unescape(gallery_data['title_jpn']),
        thumbnail_url=gallery_data['thumb'],
        category=gallery_data['category'],
        provider=constants.provider_name,
        uploader=gallery_data['uploader'],
        posted=datetime.fromtimestamp(int(gallery_data['posted']), timezone.utc),
        filecount=gallery_data['filecount'],
        filesize=gallery_data['filesize'],
        expunged=gallery_data['expunged'],
        rating=gallery_data['rating'],
        tags=translate_tag_list(gallery_data['tags']),
    )
    m = re.search(constants.default_fjord_tags, ",".join(internal_gallery_data.tags))
    if m:
        internal_gallery_data.fjord = True
    if internal_gallery_data.thumbnail_url and constants.ex_thumb_url in internal_gallery_data.thumbnail_url:
        internal_gallery_data.thumbnail_url = internal_gallery_data.thumbnail_url.replace(constants.ex_thumb_url, constants.ge_thumb_url)
    return internal_gallery_data


def link_from_gid_token_fjord(gid: str, token: str, fjord: bool = False) -> str:
    if fjord:
        return '{}/g/{}/{}/'.format(constants.ex_page, gid, token)
    else:
        return '{}/g/{}/{}/'.format(constants.ge_page, gid, token)


def get_gid_token_from_link(link: str) -> typing.Tuple[str, str]:
    m = re.search(r".*?/g/(\w+)/(\w+)", link)

    if m:
        return m.group(1), m.group(2)
    else:
        return '', ''


def fjord_gid_token_from_link(link: str) -> typing.Tuple[typing.Optional[str], typing.Optional[str], typing.Optional[str]]:
    m = re.search(r'(.+)/g/(\d+)/(\w+)', link)
    if m:
        return m.group(1), m.group(2), m.group(3)
    else:
        return None, None, None


def resolve_url(gallery: 'Gallery') -> str:
    if gallery.fjord:
        return '{}/g/{}/{}/'.format(constants.ex_page, gallery.gid, gallery.token)
    else:
        return '{}/g/{}/{}/'.format(constants.ge_page, gallery.gid, gallery.token)


def request_data_from_gid_token_iterable(api_token_iterable: typing.Iterable[typing.Tuple[str, str]]) -> typing.Dict[str, typing.Any]:
    return {
        'method': 'gdata',
        'namespace': '1',
        'gidlist': api_token_iterable
    }


AttrList = typing.List[typing.Tuple[str, str]]


# TODO: This parsers should be migrated to bs4, they were written before using bs4 in the project.
class SearchHTMLParser(HTMLParser):

    def error(self, message: str) -> None:
        pass

    def handle_starttag(self, tag: str, attrs: AttrList) -> None:
        if tag == 'a' and self.stop_at_favorites != 1:
            for attr in attrs:
                if(attr[0] == 'href'
                        and (constants.ex_page + '/g/' in attr[1] or constants.ge_page + '/g/' in attr[1])):
                    self.galleries.add(attr[1])
        else:
            self.stop_at_favorites: int = 0

    def handle_data(self, data: str) -> None:
        if data == 'Popular Right Now':
            self.stop_at_favorites = 1

    def __init__(self) -> None:
        HTMLParser.__init__(self)
        self.galleries: typing.Set[str] = set()
        self.stop_at_favorites: int = 0


class GalleryHTMLParser(HTMLParser):

    def error(self, message: str) -> None:
        pass

    def handle_starttag(self, tag: str, attrs: AttrList) -> None:
        if tag == 'a' and self.stop_at_found != 1:
            self.found_gallery_link = 0
            for attr in attrs:
                if attr[0] == 'href' and attr[1] == '#':
                    self.found_gallery_link = 1
                elif(self.found_non_final_gallery == 1
                     and attr[0] == 'href'
                     and '/g/' in attr[1]):
                    self.non_final_gallery = attr[1]
                elif(self.found_parent_gallery == 1
                     and attr[0] == 'href'
                     and '/g/' in attr[1]):
                    self.parent_gallery = attr[1]
                    self.found_parent_gallery: int = 0
                elif(self.found_gallery_link == 1
                     and attr[0] == 'onclick'
                     and 'gallerytorrents.php' in attr[1]):
                    m = re.search(r'\'(.+)\'', attr[1])
                    if m:
                        self.torrent_link = m.group(1)
        if(tag == 'textarea'
                and attrs[0][0] == 'name'
                and attrs[0][1] == 'commenttext'):
            self.stop_at_found: int = 1
            return
        if tag == 'p' and self.found_non_final_gallery == 1:
            for attr in attrs:
                if attr[0] == 'class' and attr[1] == 'ip':
                    self.found_non_final_gallery: int = 2

    def handle_data(self, data: str) -> None:
        if 'The uploader has made available \
        newer versions of this gallery:' in data:
            self.found_non_final_gallery = 1
        elif 'Parent:' == data:
            self.found_parent_gallery = 1

    def __init__(self) -> None:
        HTMLParser.__init__(self, convert_charrefs=True)
        self.torrent_link = ''
        self.stop_at_found: int = 0
        self.found_non_final_gallery: int = 0
        self.parent_gallery: str = ''
        self.found_parent_gallery: int = 0
        self.found_gallery_link: int = 0
        self.non_final_gallery: str = ''


class TorrentHTMLParser(HTMLParser):

    def error(self, message: str) -> None:
        pass

    torrent_root_url = '/ehtracker.org/'

    def handle_starttag(self, tag: str, attrs: AttrList) -> None:
        if tag == 'a':
            for attr in attrs:
                if(attr[0] == 'href'
                        and self.torrent_root_url in attr[1]
                        and self.seeds > 0):
                    self.torrent = attr[1]

    def handle_data(self, data: str) -> None:
        if 'Seeds:' == data:
            self.found_seed_data = 1
        elif 'Peers:' == data:
            self.found_seed_data = 0
        elif self.found_seed_data == 1:
            m = re.search(r'(\d+)', data)
            if m:
                self.seeds = int(m.group(1))

        if 'Posted:' == data:
            self.found_posted_data = 1
        elif 'Size:' == data:
            self.found_posted_data = 0
        elif self.found_posted_data == 1:
            m = re.search(r'^ (.+)', data)
            if m:
                self.posted_date = m.group(1) + ' +0000'

    def __init__(self) -> None:
        HTMLParser.__init__(self, convert_charrefs=True)
        self.torrent = ''
        self.found_seed_data = 0
        self.found_posted_data = 0
        self.posted_date = ''
        self.seeds = 0


ARCHIVE_ROOT_URL = '/archive/'


def contains_archive_root(href: str) -> bool:
    return ARCHIVE_ROOT_URL in href


def get_archive_link_from_html_page(page_text: str) -> str:
    soup = BeautifulSoup(page_text, 'html.parser')
    archive_link = soup.find("a", href=contains_archive_root)

    if not archive_link:
        return ''

    return archive_link.href
