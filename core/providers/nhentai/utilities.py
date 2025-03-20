import typing

from . import constants

if typing.TYPE_CHECKING:
    from viewer.models import Gallery


def resolve_url(gallery: "Gallery") -> str:
    return "{}{}/".format(constants.gallery_container_url, gallery.gid.replace("nh-", ""))
