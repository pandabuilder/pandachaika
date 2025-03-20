import re
import typing

from . import constants

if typing.TYPE_CHECKING:
    from viewer.models import Gallery


def resolve_url(gallery: "Gallery") -> str:
    return "{}/view/{}".format(constants.main_page, gallery.gid)


def clean_title(title: str) -> str:
    # Add missing space
    title = re.sub(r"](\w)", "] \1", title)
    # Remove non characters, keep spaces
    title = " ".join(str(re.sub(r"\W", "", word) for word in title.split()))
    # Remove extra whitespace
    title = re.sub(r" +", " ", title)
    # Remove starting whitespace
    title = re.sub(r"^ ", "", title)
    # Remove ending whitespace
    title = re.sub(r" $", "", title)
    # ARBITRARY: remove words 3 or less characters long
    title = " ".join(word for word in title.split() if len(word) > 3)
    return title
