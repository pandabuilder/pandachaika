import typing
from datetime import datetime
from typing import List
from xml.etree import ElementTree

from core.base.utilities import translate_tag
from core.base.types import GalleryData
from . import constants

if typing.TYPE_CHECKING:
    from viewer.models import Gallery


def resolve_url(gallery: 'Gallery') -> str:
    return '{}/book/{}/'.format(constants.main_page, gallery.gid.replace('mugi-B', ''))


def translate_language_code(code: str) -> str:
    lang_table = {
        1: 'Unknown',
        2: 'English',
        3: 'Japanese',
        4: 'Chinese',
        5: 'Korean',
        6: 'French',
        7: 'German',
        8: 'Spanish',
        9: 'Italian',
        10: 'Russian',
    }
    return lang_table[int(code)]


def convert_api_response_text_to_gallery_dicts(text: str) -> List[GalleryData]:
    galleries: List[GalleryData] = []
    # Based on: https://www.doujinshi.org/API_MANUAL.txt
    xml_root = ElementTree.fromstring(text)
    error = xml_root.find('ERROR')
    if error:
        return galleries
    for book in xml_root.findall('BOOK'):
        integer_id = int(book.get('ID').replace('B', ''))
        gallery = GalleryData('mugi-' + book.get('ID'))
        gallery.link = constants.main_page + '/book/' + str(integer_id)
        gallery.tags = []
        gallery.provider = constants.provider_name
        gallery.title = book.find('NAME_EN').text or ''
        gallery.title_jpn = book.find('NAME_JP').text or ''
        gallery.comment = ''
        gallery.category = ''
        gallery.filesize = 0
        gallery.filecount = int(book.find('DATA_PAGES').text)
        gallery.uploader = ''
        gallery.thumbnail_url = 'https://img.doujinshi.org/big/{}/{}.jpg'.format(int(integer_id / 2000), integer_id)
        gallery.queries = int(xml_root.find('USER').find('Queries').text)

        # Check if we get the 0000-00-00 date
        if book.find('DATE_RELEASED') is not None:
            date_components = book.find('DATE_RELEASED').text.split("-")
            if date_components[0] != '0000' and date_components[1] != '00' and date_components[2] != '00':
                gallery.posted = datetime.strptime(book.find('DATE_RELEASED').text + ' +0000', '%Y-%m-%d %z')

        for item in book.find('LINKS'):
            item_type = item.get('TYPE')
            item_name_en = item.find('NAME_EN')
            if item_type == 'author':
                item_type = 'artist'
            elif item_type == 'circle':
                item_type = 'group'
            elif item_type == 'type':
                gallery.category = item_name_en.text
                continue
            elif item_type is None or item_type == '':
                if item_name_en is not None and not (item_name_en.text == '' or item_name_en.text is None):
                    gallery.tags.append(translate_tag(item_name_en.text))
                continue
            if item_name_en is not None and not (item_name_en.text == '' or item_name_en.text is None):
                gallery.tags.append(translate_tag(item_type + ":" + item_name_en.text))
        if book.find('DATA_LANGUAGE') is not None and not (book.find('DATA_LANGUAGE').text == '' or book.find('DATA_LANGUAGE').text is None):
            gallery.tags.append(translate_tag("language:" + translate_language_code(book.find('DATA_LANGUAGE').text)))

        galleries.append(gallery)

    return galleries
