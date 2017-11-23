from django.contrib.syndication.views import Feed
from django.core.paginator import EmptyPage, Paginator

from viewer.views.head import filter_archives_simple, archive_filter_keys


class LatestArchivesFeed(Feed):
    title = "Latest submitted archives"
    link = "/"
    description = "Latest added archives to the Backup."

    def get_object(self, request, *args, **kwargs):
        return request

    def items(self, request):

        args = request.GET.copy()

        params = {
            'sort': 'create_date',
            'asc_desc': 'desc',
        }

        for k, v in args.items():
            params[k] = v

        for k in archive_filter_keys:
            if k not in params:
                params[k] = ''

        results = filter_archives_simple(params)

        if not request.user.is_authenticated:
            results = results.filter(public=True).order_by('-public_date')

        paginator = Paginator(results, 50)
        try:
            page = int(request.GET.get("page", '1'))
        except ValueError:
            page = 1
        try:
            archives = paginator.page(page)
        except EmptyPage:
            # If page is out of range (e.g. 9999), deliver last page of results.
            archives = paginator.page(paginator.num_pages)

        return archives

    def item_title(self, item):
        return item.title

    def item_description(self, item):
        return item.tags_str()

    def item_pubdate(self, item):
        return item.public_date
