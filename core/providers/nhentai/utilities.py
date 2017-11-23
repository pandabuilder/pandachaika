from . import constants


def resolve_url(gallery):
    return '{}{}/'.format(constants.gallery_container_url, gallery.gid.replace('nh-', ''))
