import typing

if typing.TYPE_CHECKING:
    from viewer.models import Gallery


def resolve_url(gallery: "Gallery") -> str:
    return "{}".format(gallery.gid)
