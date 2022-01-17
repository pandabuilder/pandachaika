import typing
from datetime import datetime
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


def convert_api_response_text_to_gallery_dicts(text: str) -> list[GalleryData]:
    galleries: list[GalleryData] = []
    # Based on: https://www.doujinshi.org/API_MANUAL.txt
    xml_root = ElementTree.fromstring(text)
    error = xml_root.find('ERROR')
    if error:
        return galleries
    for book in xml_root.findall('BOOK'):

        book_id = book.get('ID')

        if book_id is None:
            continue

        integer_id = int(book_id.replace('B', ''))
        gallery = GalleryData('mugi-' + book_id, constants.provider_name)
        gallery.link = constants.main_page + '/book/' + str(integer_id)
        gallery.tags = []
        found_en_title = book.find('NAME_EN')
        if found_en_title is not None:
            gallery.title = found_en_title.text or ''
        found_jp_title = book.find('NAME_JP')
        if found_jp_title is not None:
            gallery.title_jpn = found_jp_title.text or ''
        gallery.comment = ''
        gallery.category = ''
        gallery.filesize = 0
        found_data_pages = book.find('DATA_PAGES')
        if found_data_pages is not None and found_data_pages.text:
            gallery.filecount = int(found_data_pages.text)
        else:
            gallery.filecount = 0
        gallery.uploader = ''
        gallery.thumbnail_url = 'https://img.doujinshi.org/big/{}/{}.jpg'.format(int(integer_id / 2000), integer_id)
        found_user = xml_root.find('USER')
        if found_user is not None:
            found_user_queries = found_user.find('Queries')
            if found_user_queries is not None and found_user_queries.text:
                gallery.queries = int(found_user_queries.text)
            else:
                gallery.queries = 0
        else:
            gallery.queries = 0

        # Check if we get the 0000-00-00 date
        found_date_released = book.find('DATE_RELEASED')
        if found_date_released is not None and found_date_released.text:
            date_components = found_date_released.text.split("-")
            if len(date_components) >= 3:
                if date_components[0] != '0000' and date_components[1] != '00' and date_components[2] != '00':
                    gallery.posted = datetime.strptime(found_date_released.text + ' +0000', '%Y-%m-%d %z')

        found_links = book.find('LINKS')

        if found_links is not None:
            for item in found_links:
                item_type = item.get('TYPE')
                item_name_en = item.find('NAME_EN')
                if item_type == 'author':
                    item_type = 'artist'
                elif item_type == 'circle':
                    item_type = 'group'
                elif item_type == 'type' and item_name_en is not None:
                    gallery.category = item_name_en.text
                    continue
                elif item_type is None or item_type == '':
                    if item_name_en is not None and not (item_name_en.text == '' or item_name_en.text is None):
                        gallery.tags.append(translate_tag(item_name_en.text))
                    continue
                if item_name_en is not None and not (item_name_en.text == '' or item_name_en.text is None):
                    gallery.tags.append(translate_tag(item_type + ":" + item_name_en.text))

        found_data_language = book.find('DATA_LANGUAGE')

        if found_data_language is not None and not (found_data_language.text == '' or found_data_language.text is None):
            gallery.tags.append(translate_tag("language:" + translate_language_code(found_data_language.text)))

        # Add non-h as a tag.
        found_age = book.find('DATA_AGE')
        if found_age is not None and not (found_age.text == '' or found_age.text is None) and found_age.text == '0':
            gallery.tags.append(translate_tag('non-h'))

        galleries.append(gallery)

    return galleries
