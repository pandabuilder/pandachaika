import json
import logging
from typing import Any

from urllib.parse import urlencode
from copy import deepcopy
from datetime import datetime

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse, QueryDict, HttpRequest
from django.views.generic.base import TemplateView
from django.views import View


from math import ceil

from core.base.types import DataDict
from viewer.utils.functions import galleries_update_metadata

es_client = settings.ES_CLIENT
max_result_window = settings.MAX_RESULT_WINDOW
es_index_name = settings.ES_INDEX_NAME

logger = logging.getLogger(__name__)

if es_client:
    import elasticsearch
    from elasticsearch_dsl import Search, Q
    from elasticsearch_dsl.response import AggResponse


ES_SKIP_FIELDS = ('page', 'q', 'order', 'sort', 'metrics', 'show_url', 'count', 'no_agg', 'view')


class ESHomePageView(TemplateView):

    template_name = "viewer/elasticsearch.html"
    index_name = es_index_name
    page_title = 'Archive Search'

    possible_sorts = [
        'public_date',
        'create_date',
        'original_date',
        'size',
        'image_count',
    ]

    aggs_bucket_fields = [
        ('tags__full', 'tags.full', 100),
        ('source_type', 'source_type', 20),
        ('reason', 'reason', 20)
    ]

    public_sort_field = 'public_date'

    accepted_per_page = [24, 48, 100, 200, 300]

    extra_view_options = ("list", "extended")
    view_parameters_name = 'parameters'

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:

        if not settings.ES_ENABLED or not es_client:
            return {'message': 'Elasticsearch is disabled for this instance.', 'page_title': self.page_title}

        if not es_client.indices.exists(index=self.index_name):
            return {'message': 'Expected index does not exist.', 'page_title': self.page_title}

        s = Search(using=es_client, index=self.index_name)

        message = None
        count_result = 0

        if 'clear' in self.request.GET:
            self.request.GET = QueryDict('')

        s = self.gen_es_query(self.request, s)

        for bucket_name, field, size in self.aggs_bucket_fields:
            s.aggs.bucket(bucket_name, 'terms', field=field, size=size)
        # s.aggs.bucket('tags__full', 'terms', field='tags.full', size=100)
        # s.aggs.bucket('source_type', 'terms', field='source_type', size=20)
        # s.aggs.bucket('reason', 'terms', field='reason', size=20)

        if 'metrics' in self.request.GET:
            s.aggs.metric('avg_size', 'avg', field='size')
            s.aggs.metric('sum_size', 'sum', field='size')
            s.aggs.metric('max_size', 'max', field='size')
            s.aggs.metric('min_size', 'min', field='size')
            s.aggs.metric('avg_count', 'avg', field='image_count')
            s.aggs.metric('sum_count', 'sum', field='image_count')
            s.aggs.metric('max_count', 'max', field='image_count')
            s.aggs.metric('min_count', 'min', field='image_count')

        try:
            count_result = s.count()
        except elasticsearch.exceptions.RequestError:
            message = 'Error parsing the query string, check your syntax. This characters must be escaped: + - = && || > < ! ( ) { } [ ] ^ " ~ * ? : \\ /'

        try:
            per_page = int(self.request.GET.get("count", '48'))
            if per_page not in self.accepted_per_page:
                per_page = 48
        except ValueError:
            per_page = 48

        context = super(ESHomePageView, self).get_context_data(**kwargs)

        if count_result > 0:
            # Sort
            sort = self.request.GET.get('sort', '')
            order = self.request.GET.get('order', 'desc')

            if not sort and (sort not in self.possible_sorts):
                if not self.request.user.is_authenticated:
                    sort = self.public_sort_field
                else:
                    sort = 'create_date'
            if order == 'desc':
                sort = '-' + sort

            s = s.sort(sort)

            if 'recall_api' in self.request.POST and self.request.user.has_perm('viewer.update_metadata'):

                if not self.request.GET.get("q", ''):
                    return {'message': 'You should use a filter term when queueing galleries for update.', 'page_title': self.page_title}

                s = s[0:max_result_window]

                search_result = s.execute()

                results_gallery = [
                    self.convert_hit_to_template(c) for c in search_result
                ]

                gallery_links = [x['source_url'] for x in results_gallery]
                gallery_providers = list(set([x['provider'] for x in results_gallery]))

                context['count_result'] = count_result
                context['gallery_links'] = gallery_links
                context['gallery_providers'] = gallery_providers

            else:
                es_pagination = self.gen_pagination(self.request, count_result, per_page)

                if es_pagination['search']['to'] > max_result_window:
                    es_pagination['search']['from'] = max_result_window - per_page
                    es_pagination['search']['to'] = max_result_window
                    message = "Refine your search, can't go that far back (limit: {}).".format(max_result_window)

                # Pagination
                s = s[es_pagination['search']['from']:es_pagination['search']['to']]

                search_result = s.execute()

                context['hits'] = [
                    self.convert_hit_to_template(c) for c in search_result
                ]

                context['results'] = {
                    'from': es_pagination['search']['from'] + 1,
                    'to': es_pagination['search']['from'] + len(context['hits'])
                }

                context['aggregations'] = self.prepare_facet_data(
                    search_result.aggregations,
                    self.request.GET
                )
                context['paginator'] = es_pagination

        if self.extra_view_options:
            context['extra_view_options'] = self.extra_view_options

        if self.view_parameters_name in self.request.session:
            parameters = self.request.session[self.view_parameters_name]
        else:
            parameters = {}

        view_option = self.request.GET.get("view", '')

        if view_option:
            parameters['view'] = view_option

        # Fill default parameters
        if 'view' not in parameters or parameters['view'] == '':
            parameters['view'] = 'list'

        self.request.session[self.view_parameters_name] = parameters

        context['view_parameters'] = parameters
        context['q'] = self.request.GET.get("q", '')
        context['sort'] = self.request.GET.get("sort", '')
        context['order'] = self.request.GET.get("order", 'desc')
        context['message'] = message
        context['page_title'] = self.page_title

        return context

    @staticmethod
    def num_pages(count: int, per_page: int) -> int:
        """
        Returns the total number of pages.
        """
        if count == 0:
            return 0
        hits = max(1, count)
        return int(ceil(hits / float(per_page)))

    @staticmethod
    def convert_hit_to_template(hit):
        hit.pk = hit.meta.id
        # 2014-09-04T21:34:00+00:00
        hit.create_date_c = datetime.strptime(hit.create_date.replace("+00:00", "+0000"), '%Y-%m-%dT%H:%M:%S%z')
        if hit.public_date:
            hit.public_date_c = datetime.strptime(hit.public_date.replace("+00:00", "+0000"), '%Y-%m-%dT%H:%M:%S%z')
        else:
            hit.public_date_c = None
        if hit.original_date:
            hit.original_date_c = datetime.strptime(hit.original_date.replace("+00:00", "+0000"), '%Y-%m-%dT%H:%M:%S%z')
        else:
            hit.original_date_c = None
        return hit

    @staticmethod
    def facet_url_args(url_args, field_name, field_value):
        is_active = False
        if url_args.get(field_name):
            base_list = url_args[field_name].split(',')
            if field_value in base_list:
                del base_list[base_list.index(field_value)]
                is_active = True
            else:
                base_list.append(field_value)
            url_args[field_name] = ','.join(base_list)
        else:
            url_args[field_name] = field_value
        return url_args, is_active

    def prepare_facet_data(self, aggregations: 'AggResponse', get_args: QueryDict) -> dict[str, list[dict[str, str]]]:
        resp: DataDict = {}
        for area, agg in aggregations.to_dict().items():
            resp[area] = []
            if 'value' in agg:  # if the aggregation has the value key, it comes from the metrics.
                resp[area] = agg['value']
                continue
            for item in aggregations[area].buckets:
                url_args, is_active = self.facet_url_args(
                    url_args=deepcopy(get_args.dict()),
                    field_name=area,
                    field_value=str(item.key)
                )
                resp[area].append({
                    'url_args': urlencode(url_args),
                    'name': item.key,
                    'count': item.doc_count,
                    'is_active': is_active
                })
        return resp

    @staticmethod
    def gen_es_query(request, s):
        req_dict = deepcopy(request.GET.dict())
        if not request.user.is_authenticated:
            s = s.filter('term', public=True)
        if not req_dict:
            return s.filter('match_all')
        q = req_dict.get('q', '')
        if q:
            s = s.query('query_string', query=q, fields=['title', 'title_jpn', 'tags.full'])
        else:
            s = s.query('match_all')
        for field_name in req_dict.keys():
            if field_name in ES_SKIP_FIELDS:
                continue
            if '__' in field_name:
                filter_field_name = field_name.replace('__', '.')
            else:
                filter_field_name = field_name
            for field_value in req_dict[field_name].split(','):
                if not field_value:
                    continue
                if field_value.startswith('-'):
                    s = s.exclude('term', **{filter_field_name: field_value.replace('-', '')})
                else:
                    s = s.filter('term', **{filter_field_name: field_value})
        return s

    def gen_pagination(self, request: HttpRequest, count: int, per_page: int) -> dict[str, Any]:

        paginator: dict[str, Any] = {}

        try:
            page = int(request.GET.get("page", '1'))
            if page < 1:
                page = 1
        except ValueError:
            page = 1

        num_pages = self.num_pages(count, per_page)

        number = page
        if number > num_pages:
            number = num_pages

        paginator['count'] = count
        paginator['number'] = number
        paginator['num_pages'] = num_pages
        paginator['page_range'] = list(
            range(
                max(1, number - 3 - max(0, number - (num_pages - 3))),
                min(num_pages + 1, number + 3 + 1 - min(0, number - 3 - 1))
            )
        )
        bottom = (number - 1) * per_page
        if bottom < 1:
            bottom = 0
        top = bottom + per_page
        if top >= count:
            top = count

        paginator['search'] = {'from': bottom, 'to': top}

        return paginator


class ESHomeGalleryPageView(ESHomePageView):

    template_name = "viewer/elasticsearch_gallery.html"
    index_name = settings.ES_GALLERY_INDEX_NAME
    page_title = 'Gallery Search'

    extra_view_options = ("list", "extended")

    possible_sorts = [
        'create_date',
        'posted_date',
        'size',
        'image_count',
        'provider',
    ]

    aggs_bucket_fields = [
        ('tags__full', 'tags.full', 100),
        ('category', 'category', 20),
        ('provider', 'provider', 20),
        ('reason', 'reason', 20)
    ]

    public_sort_field = 'posted_date'

    view_parameters_name = 'gallery_parameters'

    @staticmethod
    def convert_hit_to_template(hit):
        hit.pk = hit.meta.id
        # 2014-09-04T21:34:00+00:00
        hit.create_date_c = datetime.strptime(hit.create_date.replace("+00:00", "+0000"), '%Y-%m-%dT%H:%M:%S%z')
        if hit.posted_date:
            hit.posted_date_c = datetime.strptime(hit.posted_date.replace("+00:00", "+0000"), '%Y-%m-%dT%H:%M:%S%z')
        else:
            hit.posted_date_c = None
        return hit

    def post(self, request, *args, **kwargs):
        context = self.get_context_data(**kwargs)

        if 'recall_api' in request.POST and request.user.has_perm('viewer.update_metadata'):
            if 'count_result' in context:
                p = request.POST

                message = 'Recalling API for {} galleries'.format(context['count_result'])
                logger.info(message)
                messages.success(request, message)

                if 'reason' in p and p['reason'] != '':
                    reason = p['reason']
                else:
                    reason = ''

                galleries_update_metadata(
                    context['gallery_links'], context['gallery_providers'], request.user, reason, settings.CRAWLER_SETTINGS
                )
            else:
                context['message'] = 'No galleries updated.'

        return self.render_to_response(context)


# TODO: This method doesn't seem to be returning (response object)
# Not being used client-side
def autocomplete_view(request: HttpRequest) -> HttpResponse:
    if not settings.ES_ENABLED or not es_client:
        return HttpResponse({})
    if not es_client.indices.exists(index=es_index_name):
        return HttpResponse({})

    query = request.GET.get('q', '')

    s = Search(using=es_client, index=es_index_name)

    response = s.suggest(
        'title_complete',
        query,
        completion={
            "field": 'title_complete',
        }
    ).execute()

    options = response['title_complete'][0]['options']
    data = json.dumps(
        [{'id': i['_id'], 'title': i['text']} for i in options]
    )
    mime_type = 'application/json; charset=utf-8'
    http_response = HttpResponse(data, mime_type)
    return http_response


def title_suggest_view(request: HttpRequest) -> HttpResponse:
    if not settings.ES_ENABLED or not es_client:
        return HttpResponse({})
    if not es_client.indices.exists(index=es_index_name):
        return HttpResponse({})

    query = request.GET.get('q', '')
    s = Search(using=es_client, index=es_index_name) \
        .suggest(
        "title-suggestion", text=query,
        phrase={
            'field': 'title_suggest',
            "size": 5,
            # "gram_size": 10,
            # "confidence": 0.0,
            "direct_generator": [
                {
                    "field": "title_suggest",
                    "suggest_mode": "always",
                    "min_word_length": 3,
                }
            ]
        }
    )
    response = s.execute()

    suggestion_parts = [
        part['text'] for part in response['suggest']['title-suggestion'][0]['options']
    ]

    data = json.dumps(
        {'suggestions': suggestion_parts}
    )
    mime_type = 'application/json; charset=utf-8'
    http_response = HttpResponse(data, mime_type)
    return http_response


def title_suggest_archive_view(request: HttpRequest) -> HttpResponse:
    if not settings.ES_ENABLED or not es_client:
        return HttpResponse({})
    if not es_client.indices.exists(index=es_index_name):
        return HttpResponse({})

    query = request.GET.get('q', '')
    s = Search(using=es_client, index=es_index_name) \
        .source(['title', 'thumbnail']) \
        .query("match", title_suggest={'query': query, 'operator': 'and', 'fuzziness': 'AUTO'})
    response = s.execute()

    data = json.dumps(
        [{'id': i.meta.id, 'title': i.title, 'thumbnail': i.thumbnail} for i in response]
    )
    mime_type = 'application/json; charset=utf-8'
    http_response = HttpResponse(data, mime_type)
    return http_response


def title_pk_suggest_archive_view(request: HttpRequest) -> HttpResponse:
    if not settings.ES_ENABLED or not es_client:
        return HttpResponse({})
    if not es_client.indices.exists(index=es_index_name):
        return HttpResponse({})

    query = request.GET.get('q', '')
    s = Search(using=es_client, index=es_index_name) \
        .source(['title', 'thumbnail']) \
        .query(
        "bool",
        should=[
            Q("term", _id={'value': query, 'boost': 999}),
            Q("match", title_suggest={'query': query, 'operator': 'and', 'fuzziness': 'AUTO'})
        ]
    )
    response = s.execute()

    data = json.dumps(
        [{'id': i.meta.id, 'title': i.title, 'thumbnail': i.thumbnail} for i in response]
    )
    mime_type = 'application/json; charset=utf-8'
    http_response = HttpResponse(data, mime_type)
    return http_response


def archive_simple(request: HttpRequest) -> HttpResponse:
    if not settings.ES_ENABLED or not es_client:
        return HttpResponse({})
    if not es_client.indices.exists(index=es_index_name):
        return HttpResponse({})

    query = request.GET.get('q', '')
    s = Search(using=es_client, index=es_index_name) \
        .source(['title', 'thumbnail']) \
        .query("term", _id=query)
    response = s.execute()

    if len(response) > 0:
        i = response[0]
        data = json.dumps(
            {'id': i.meta.id, 'title': i.title, 'thumbnail': i.thumbnail}
        )
    else:
        data = json.dumps({})
    mime_type = 'application/json; charset=utf-8'
    http_response = HttpResponse(data, mime_type)
    return http_response


def archives_simple(request: HttpRequest) -> HttpResponse:
    if not settings.ES_ENABLED or not es_client:
        return HttpResponse({})
    if not es_client.indices.exists(index=es_index_name):
        return HttpResponse({})

    query = request.GET.getlist('q', '')
    s = Search(using=es_client, index=es_index_name) \
        .source(['title', 'thumbnail']) \
        .query("terms", _id=query)
    response = s.execute()

    data = json.dumps(
        [{'id': i.meta.id, 'title': i.title, 'thumbnail': i.thumbnail} for i in response]
    )
    mime_type = 'application/json; charset=utf-8'
    http_response = HttpResponse(data, mime_type)
    return http_response


class ESArchiveJSONView(View):

    index_name = es_index_name

    possible_sorts = [
        'public_date',
        'create_date',
        'original_date',
        'size',
        'image_count',
    ]

    aggs_bucket_fields = [
        ('tags__full', 'tags.full', 50),
        ('source_type', 'source_type', 20),
        ('reason', 'reason', 20)
    ]

    accepted_per_page = [24, 48, 100, 200, 300]

    public_sort_field = 'public_date'

    def get(self, request, *args, **kwargs):
        data = self.get_context_data()
        return HttpResponse(json.dumps(data), content_type="application/json; charset=utf-8")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:

        if not settings.ES_ENABLED or not es_client:
            return {'message': 'Elasticsearch is disabled for this instance.'}

        if not es_client.indices.exists(index=self.index_name):
            return {'message': 'Expected index does not exist.'}

        s = Search(using=es_client, index=self.index_name)

        message = None
        count_result = 0

        if 'clear' in self.request.GET:
            self.request.GET = QueryDict('')

        s = self.gen_es_query(self.request, s)

        if 'no_agg' not in self.request.GET:
            for bucket_name, field, size in self.aggs_bucket_fields:
                s.aggs.bucket(bucket_name, 'terms', field=field, size=size)
            # s.aggs.bucket('tags__full', 'terms', field='tags.full', size=100)
            # s.aggs.bucket('source_type', 'terms', field='source_type', size=20)
            # s.aggs.bucket('reason', 'terms', field='reason', size=20)

        if 'metrics' in self.request.GET:
            s.aggs.metric('avg_size', 'avg', field='size')
            s.aggs.metric('sum_size', 'sum', field='size')
            s.aggs.metric('max_size', 'max', field='size')
            s.aggs.metric('min_size', 'min', field='size')
            s.aggs.metric('avg_count', 'avg', field='image_count')
            s.aggs.metric('sum_count', 'sum', field='image_count')
            s.aggs.metric('max_count', 'max', field='image_count')
            s.aggs.metric('min_count', 'min', field='image_count')

        try:
            count_result = s.count()
        except elasticsearch.exceptions.RequestError:
            message = 'Error parsing the query string, check your syntax. This characters must be escaped: + - = && || > < ! ( ) { } [ ] ^ " ~ * ? : \\ /'

        try:
            per_page = int(self.request.GET.get("count", '48'))
            if per_page not in self.accepted_per_page:
                per_page = 48
        except ValueError:
            per_page = 48

        context: dict[str, Any] = {}

        if count_result > 0:
            es_pagination = self.gen_pagination(self.request, count_result, per_page)

            if es_pagination['search']['to'] > max_result_window:
                es_pagination['search']['from'] = max_result_window - per_page
                es_pagination['search']['to'] = max_result_window
                message = "Refine your search, can't go that far back (limit: {}).".format(max_result_window)

            # Sort

            sort = self.request.GET.get('sort', '')
            order = self.request.GET.get('order', 'desc')

            if not sort and (sort not in self.possible_sorts):
                if not self.request.user.is_authenticated:
                    sort = self.public_sort_field
                else:
                    sort = 'create_date'
            if order == 'desc':
                sort = '-' + sort

            s = s.sort(sort)

            # Pagination
            s = s[es_pagination['search']['from']:es_pagination['search']['to']]

            search_result = s.execute()

            context['hits'] = [
                self.convert_hit_to_template(c, self.request) for c in search_result
            ]

            context['results'] = {
                'from': es_pagination['search']['from'] + 1,
                'to': es_pagination['search']['from'] + len(context['hits'])
            }

            if 'no_agg' not in self.request.GET:
                context['aggregations'] = self.prepare_facet_data(
                    search_result.aggregations,
                    self.request.GET
                )
            context['paginator'] = es_pagination

        context['q'] = self.request.GET.get("q", '')
        context['sort'] = self.request.GET.get("sort", '')
        context['order'] = self.request.GET.get("order", 'desc')
        context['message'] = message

        return context

    @staticmethod
    def num_pages(count: int, per_page: int) -> int:
        """
        Returns the total number of pages.
        """
        if count == 0:
            return 0
        hits = max(1, count)
        return int(ceil(hits / float(per_page)))

    @staticmethod
    def convert_hit_to_template(hit, request):
        hit.pk = hit.meta.id
        hit_as_dict = hit.to_dict()
        if 'doc' in hit_as_dict:
            del hit_as_dict['doc']
        del hit_as_dict['title_suggest']
        del hit_as_dict['title_complete']
        if not request.user.is_authenticated:
            del hit_as_dict['create_date']
        return hit_as_dict

    @staticmethod
    def facet_url_args(url_args, field_name, field_value):
        is_active = False
        if url_args.get(field_name):
            base_list = url_args[field_name].split(',')
            if field_value in base_list:
                del base_list[base_list.index(field_value)]
                is_active = True
            else:
                base_list.append(field_value)
            url_args[field_name] = ','.join(base_list)
        else:
            url_args[field_name] = field_value
        return url_args, is_active

    def prepare_facet_data(self, aggregations: 'AggResponse', get_args: QueryDict) -> dict[str, list[dict[str, str]]]:
        resp: DataDict = {}
        for area, agg in aggregations.to_dict().items():
            resp[area] = []
            if 'value' in agg:  # if the aggregation has the value key, it comes from the metrics.
                resp[area] = agg['value']
                continue
            for item in aggregations[area].buckets:
                url_args, is_active = self.facet_url_args(
                    url_args=deepcopy(get_args.dict()),
                    field_name=area,
                    field_value=str(item.key)
                )
                resp[area].append({
                    'url_args': urlencode(url_args),
                    'name': item.key,
                    'count': item.doc_count,
                    'is_active': is_active
                })
        return resp

    @staticmethod
    def gen_es_query(request, s):
        req_dict = deepcopy(request.GET.dict())
        if not request.user.is_authenticated:
            s = s.filter('term', public=True)
        if not req_dict:
            return s.filter('match_all')
        q = req_dict.get('q', '')
        if q:
            s = s.query('query_string', query=q, fields=['title', 'title_jpn', 'tags.full'])
        else:
            s = s.query('match_all')
        for field_name in req_dict.keys():
            if field_name in ('page', 'q', 'order', 'sort', 'metrics', 'show_url', 'count', 'no_agg'):
                continue
            if '__' in field_name:
                filter_field_name = field_name.replace('__', '.')
            else:
                filter_field_name = field_name
            for field_value in req_dict[field_name].split(','):
                if not field_value:
                    continue
                if field_value.startswith('-'):
                    s = s.exclude('term', **{filter_field_name: field_value.replace('-', '')})
                else:
                    s = s.filter('term', **{filter_field_name: field_value})
        return s

    def gen_pagination(self, request: HttpRequest, count: int, per_page: int) -> dict[str, Any]:

        paginator: dict[str, Any] = {}

        try:
            page = int(request.GET.get("page", '1'))
            if page < 1:
                page = 1
        except ValueError:
            page = 1

        num_pages = self.num_pages(count, per_page)

        number = page
        if number > num_pages:
            number = num_pages

        paginator['count'] = count
        paginator['number'] = number
        paginator['num_pages'] = num_pages
        paginator['page_range'] = list(
            range(
                max(1, number - 3 - max(0, number - (num_pages - 3))),
                min(num_pages + 1, number + 3 + 1 - min(0, number - 3 - 1))
            )
        )
        bottom = (number - 1) * per_page
        if bottom < 1:
            bottom = 0
        top = bottom + per_page
        if top >= count:
            top = count

        paginator['search'] = {'from': bottom, 'to': top}

        return paginator


class ESGalleryJSONView(ESArchiveJSONView):

    index_name = settings.ES_GALLERY_INDEX_NAME

    possible_sorts = [
        'create_date',
        'posted_date',
        'size',
        'image_count',
        'provider',
    ]

    aggs_bucket_fields = [
        ('tags__full', 'tags.full', 50),
        ('category', 'category', 20),
        ('provider', 'provider', 20),
        ('reason', 'reason', 20)
    ]

    public_sort_field = 'posted_date'
