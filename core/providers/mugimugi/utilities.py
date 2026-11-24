import typing

from . import constants

if typing.TYPE_CHECKING:
    from viewer.models import Gallery


def resolve_url(gallery: "Gallery") -> str:
    return "{}/book/{}/".format(constants.main_page, gallery.gid.replace("mugi-B", ""))
