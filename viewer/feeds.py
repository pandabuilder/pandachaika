from collections.abc import Iterable
from typing import Any

from datetime import datetime
from django.contrib.syndication.views import Feed
from django.core.paginator import Paginator
from django.http import HttpRequest

from viewer.models import Archive
from viewer.views.head import filter_archives_simple, archive_filter_keys


class LatestArchivesFeed(Feed):
    title = "Latest submitted archives"
    link = "/"
    description = "Latest added archives to the Backup."

    def get_object(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpRequest:
        return request

    def items(self, request: HttpRequest) -> Iterable[Archive]:

        args = request.GET.copy()

        params = {
            "sort": "create_date",
            "asc_desc": "desc",
        }

        for k, v in args.items():
            if isinstance(v, str):
                params[k] = v

        for k in archive_filter_keys:
            if k not in params:
                params[k] = ""

        results = filter_archives_simple(params, authenticated=request.user.is_authenticated)

        if not request.user.is_authenticated:
            results = results.filter(public=True).order_by("-public_date")

        results = results.prefetch_related("tags")

        try:
            page = int(request.GET.get("page", "1"))
        except ValueError:
            page = 1

        if page <= 1:
            return results[:50]

        offset = (page - 1) * 50
        archives = list(results[offset : offset + 50])
        if not archives:
            paginator = Paginator(results, 50)
            return paginator.page(paginator.num_pages)

        return archives

    def item_title(self, item: Archive) -> str:
        return item.title or item.title_jpn or ""

    def item_description(self, item: Archive) -> str:
        return item.tags_str()

    def item_pubdate(self, item: Archive) -> datetime | None:
        return item.public_date
