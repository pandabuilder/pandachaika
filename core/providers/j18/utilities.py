import re
import typing
from urllib.parse import urljoin

from . import constants

if typing.TYPE_CHECKING:
    from viewer.models import Gallery


def resolve_url(gallery: "Gallery") -> str:
    return urljoin(constants.main_url, "manga/" + gallery.gid)


def clean_title(title: str) -> str:
    # Remove parenthesis
    adjusted_title = re.sub(r"\s+\(.+?\)", r"", re.sub(r"\[.+?\]\s*", r"", title))
    # Remove non-words, non whitespace
    # adjusted_title = re.sub(r'[^\w\s]', r' ', adjusted_title)
    return adjusted_title
