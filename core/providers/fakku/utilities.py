import re
from urllib.parse import urljoin

from . import constants


def resolve_url(gallery):
    return urljoin(constants.main_url, gallery.gid)


def clean_title(title):
    # Remove parenthesis
    adjusted_title = re.sub(r'\s+\(.+?\)', r'', re.sub(r'\[.+?\]\s*', r'', title))
    # Remove non words, non whitespace
    # adjusted_title = re.sub(r'[^\w\s]', r' ', adjusted_title)
    return adjusted_title
