import re
import typing
from urllib.parse import unquote

from core.base.types import GalleryData
from . import constants

if typing.TYPE_CHECKING:
    from viewer.models import Gallery


def resolve_url(gallery: 'Gallery') -> str:
    return '{}/{}/'.format(constants.main_page, gallery.gid)


def clean_title(title: str) -> str:
    # Add missing space
    title = re.sub(r'](\w)', '] \1', title)
    # Remove non characters, keep spaces
    title = ' '.join(str(re.sub(r'\W', '', word) for word in title.split()))
    # Remove extra whitespace
    title = re.sub(r' +', ' ', title)
    # Remove starting whitespace
    title = re.sub(r'^ ', '', title)
    # Remove ending whitespace
    title = re.sub(r' $', '', title)
    # ARBITRARY: remove words 3 or less characters long
    title = ' '.join(word for word in title.split() if len(word) > 3)
    return title


def guess_gallery_read_url(gallery_page_url, gallery: GalleryData, underscore=True):
    # Some galleries are badly presented on cafe, we can try to guess based on this:
    # Example 1:
    # https://hentai.cafe/namonashi-unrelenting-perorist-prostration/
    # https://hentai.cafe/manga/read/unrelenting_perorist_prostration/en/0/1/page/1
    # Not working with:
    # Example 2:
    # https://hentai.cafe/nanao-yukiji-mitsuki-kuns-family-is-a-little-strange/
    # https://hentai.cafe/manga/read/mitsuki-kun-s-family-is-a-little-strange/en/0/1/page/1
    # Example 3:
    # https://hentai.cafe/koppori-nama-beer-meeting/
    # https://hentai.cafe/manga/read/custom_hiiragisan/en/0/1/page/1
    gallery_page_url = gallery_page_url.replace(constants.main_page, "")
    artists = [x.replace("artist:", "") for x in gallery.tags if x.startswith('artist:')]
    if artists:
        first_artist = artists[0]
        words_on_artist = first_artist.split('_')
        for word in words_on_artist:
            gallery_page_url = gallery_page_url.replace(word, "")
    gallery_page_url = re.sub(r'^/-+', '/', gallery_page_url)
    # Some newer URLs don't use the underscore!
    gallery_page_url = gallery_page_url.replace("-", "_")
    gallery_page_url = re.sub(r'\W', '', unquote(gallery_page_url))
    if underscore:
        gallery_page_url = gallery_page_url.replace("_", "-")
    gallery_page_url = "{}/manga/read/{}/en/0/1/page/1".format(constants.main_page, gallery_page_url)
    return gallery_page_url
