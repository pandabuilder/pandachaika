import re
import typing
from urllib.parse import urljoin
from . import constants

if typing.TYPE_CHECKING:
    from viewer.models import Gallery


def download_link_from_view_link(url: str) -> str:
    return re.sub(r"/view/(\d+)", r"/download/\1.torrent", url)


def view_link_from_download_link(url: str) -> str:
    return re.sub(r"/download/(\d+)\.torrent", r"/view/\1", url)


def resolve_url(gallery: 'Gallery') -> str:
    return "{}/{}".format(urljoin(constants.base_url, constants.view_path), gallery.gid)
