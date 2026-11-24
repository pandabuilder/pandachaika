from typing import Final

provider_name: Final[str] = "doujinshi_mirror"
friendly_name = "doujinshi.org mirror"
home_page = "https://www.doujinshi.org"
description = (
    "Mirror of the doujinshi.org (MugiMugi) database, served by a doujinshi-browser instance "
    "(providers.doujinshi_mirror.general.url)."
)

# The site the data comes from. Its book URLs are accepted as links, and used for galleries when no mirror is set.
original_page = "https://www.doujinshi.org"
original_book_urls = ["doujinshi.org/book/", "mugimugi.org/book/"]
# Images are stored with their URL on the original image server (the old mugimugi provider's too), which doesn't
# depend on the mirror's address. They're fetched from the same path under the mirror: <url>images/<size>/...
original_image_url = "https://img.doujinshi.org/{size}/{bucket}/{id}.jpg"
images_path = "images/"

# Book pages on the mirror's web frontend: <url>/#/book/<id>. Search pages: <url>/#/books?<filters>.
book_page_path = "#/book/"
search_page_path = "#/books"

api_path = "api/v1/"
books_path = api_path + "books"
match_path = api_path + "books/match"

# Gids of the books, shared with the old mugimugi provider.
gid_prefix = "mugi-B"

# Books per request when fetching by id, and per page when searching.
ids_per_request = 100
page_size = 50
# Candidates asked to the fuzzy title matcher.
match_limit = 20

# The mirror has no rate limits. Used when general.wait_timer isn't set.
default_wait_timer = 1

# Pages crawled from a search page link when general.stop_page_number isn't set.
default_search_pages = 10
