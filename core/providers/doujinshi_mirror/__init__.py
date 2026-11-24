"""A mirror of doujinshi.org (MugiMugi): the doujinshi-browser server, a read-only JSON API over a dump of the site.

Metadata only (like fakku): the parser and the wanted generator add galleries without archives, through the ``info``
(or ``fake``) downloader, and the title matcher links archives to its books.

The server has no fixed address, so it's configured with ``providers.doujinshi_mirror.general.url`` and nothing that is
stored depends on it: gids are the book ids (``mugi-B<id>``, the same as the old ``mugimugi`` provider, so a book is
recognised in both), gallery URLs are built from the current setting when shown, and thumbnails are stored with their
URL on the original image server (https://img.doujinshi.org/<size>/<bucket>/<id>.jpg), fetched from the current mirror
(``utilities.resolve_thumbnail_url``).
"""
