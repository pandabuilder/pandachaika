import json
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
        str(gallery_data['gid']),
        constants.provider_name,
        token=gallery_data['token'],
        title=unescape(gallery_data['title']),
        title_jpn=unescape(gallery_data['title_jpn']),
        thumbnail_url=gallery_data['thumb'],
        category=gallery_data['category'],
        uploader=unescape(gallery_data['uploader']),
        posted=datetime.fromtimestamp(int(gallery_data['posted']), timezone.utc),
        filecount=gallery_data['filecount'],
        filesize=gallery_data['filesize'],
        expunged=gallery_data['expunged'],
        rating=gallery_data['rating'],
        tags=translate_tag_list(gallery_data['tags']),
    )

    if gallery_data['uploader'] == '(Disowned)':
        internal_gallery_data.disowned = True
        internal_gallery_data.uploader = None

    internal_gallery_data.provider_metadata = json.dumps(gallery_data)

    possible_extra_keys = ('parent_gid', 'parent_key', 'first_gid', 'first_key', 'current_gid', 'current_key')

    internal_gallery_data.extra_data['torrents'] = gallery_data['torrents']
    internal_gallery_data.extra_data['torrentcount'] = gallery_data['torrentcount']

    for possible_key in possible_extra_keys:
        if possible_key in gallery_data:
            internal_gallery_data.extra_data[possible_key] = gallery_data[possible_key]

    internal_gallery_data.extra_provider_data = list()

    if 'parent_gid' in gallery_data:
        internal_gallery_data.parent_gallery_gid = gallery_data['parent_gid']
        if 'parent_key' in gallery_data:
            parent_link = link_from_gid_token_fjord(gallery_data['parent_gid'], gallery_data['parent_key'])
            internal_gallery_data.extra_provider_data.append(('parent', 'text', parent_link))

    if 'first_gid' in gallery_data:
        internal_gallery_data.first_gallery_gid = gallery_data['first_gid']
        if 'first_key' in gallery_data:
            parent_link = link_from_gid_token_fjord(gallery_data['first_gid'], gallery_data['first_key'])
            internal_gallery_data.extra_provider_data.append(('first', 'text', parent_link))

    if 'current_gid' in gallery_data and 'current_key' in gallery_data:
        current_link = link_from_gid_token_fjord(gallery_data['current_gid'], gallery_data['current_key'])
        internal_gallery_data.extra_provider_data.append(('current', 'text', current_link))

    internal_gallery_data.root = constants.ge_page

    m = re.search(constants.default_fjord_tags, ",".join(internal_gallery_data.tags))
    if m:
        internal_gallery_data.fjord = True
    else:
        internal_gallery_data.fjord = False

    if internal_gallery_data.thumbnail_url and constants.ex_thumb_url in internal_gallery_data.thumbnail_url:
        internal_gallery_data.thumbnail_url = internal_gallery_data.thumbnail_url.replace(constants.ex_thumb_url, constants.ge_thumb_url)
    return internal_gallery_data


def link_from_gid_token_fjord(gid: str, token: str, fjord: bool = False) -> str:
    if fjord:
        return '{}/g/{}/{}/'.format(constants.ex_page, gid, token)
    else:
        return '{}/g/{}/{}/'.format(constants.ge_page, gid, token)
    # return '{}/g/{}/{}/'.format(constants.ge_page, gid, token)


def get_gid_token_from_link(link: str) -> tuple[str, str]:
    m = re.search(r".*?/g/(\w+)/(\w+)", link)

    if m:
        return m.group(1), m.group(2)
    else:
        return '', ''


def root_gid_token_from_link(link: str) -> tuple[typing.Optional[str], typing.Optional[str], typing.Optional[str]]:
    m = re.search(r'(.+)/g/(\d+)/(\w+)', link)
    if m:
        return m.group(1), m.group(2), m.group(3)
    else:
        return None, None, None


def resolve_url(gallery: 'Gallery') -> str:
    return '{}/g/{}/{}/'.format(constants.ge_page, gallery.gid, gallery.token)


def request_data_from_gid_token_iterable(api_token_iterable: typing.Iterable[tuple[str, str]]) -> dict[str, typing.Any]:
    return {
        'method': 'gdata',
        'namespace': '1',
        'gidlist': api_token_iterable
    }


AttrList = list[tuple[str, typing.Optional[str]]]


# TODO: This parsers should be migrated to bs4, they were written before using bs4 in the project.
class SearchHTMLParser(HTMLParser):

    def __init__(self) -> None:
        HTMLParser.__init__(self)
        self.galleries: typing.Set[str] = set()
        self.stop_at_favorites: int = 0

    def error(self, message: str) -> None:
        pass

    def handle_starttag(self, tag: str, attrs: AttrList) -> None:
        if tag == 'a' and self.stop_at_favorites != 1:
            for attr in attrs:
                if (attr[0] == 'href'
                        and (constants.ex_page + '/g/' in str(attr[1]) or constants.ge_page + '/g/' in str(attr[1]))):
                    self.galleries.add(str(attr[1]))
        else:
            self.stop_at_favorites = 0

    def handle_data(self, data: str) -> None:
        if data == 'Popular Right Now':
            self.stop_at_favorites = 1


class TorrentHTMLParser(HTMLParser):

    def __init__(self) -> None:
        HTMLParser.__init__(self, convert_charrefs=True)
        self.torrent = ''
        self.found_seed_data = 0
        self.found_posted_data = 0
        self.posted_date = ''
        self.seeds = 0

    def error(self, message: str) -> None:
        pass

    torrent_root_url = '/ehtracker.org/'

    def handle_starttag(self, tag: str, attrs: AttrList) -> None:
        if tag == 'a':
            for attr in attrs:
                if (attr[0] == 'href' and self.torrent_root_url in str(attr[1]) and self.seeds > 0):
                    self.torrent = str(attr[1])

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


ARCHIVE_ROOT_URL = '/archive/'


def contains_archive_root(href: str) -> bool:
    return ARCHIVE_ROOT_URL in href


def get_archive_link_from_html_page(page_text: str) -> str:
    soup = BeautifulSoup(page_text, 'html.parser')
    archive_link = soup.find("a", href=contains_archive_root)

    if not archive_link:
        return ''

    return str(archive_link.get('href'))
