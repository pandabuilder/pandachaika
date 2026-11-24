"""doujinshi.org (MugiMugi), shut down.

Only what the galleries added from it still need is kept: the provider registration (``constants``) and the URL of
each gallery (``utilities.resolve_url``). Its data lives on in the ``doujinshi_mirror`` provider, a JSON API over a
dump of the site, which gives its books the same gids (``mugi-B<id>``).
"""
