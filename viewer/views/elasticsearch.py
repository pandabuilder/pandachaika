import json
from typing import Any

from urllib.parse import urlencode
from copy import deepcopy
from datetime import datetime

from django.conf import settings
from django.http import HttpResponse, QueryDict, HttpRequest
from django.views.generic.base import TemplateView


from math import ceil

from elasticsearch_dsl.response import AggResponse

from core.base.types import DataDict

es_client = settings.ES_CLIENT
max_result_window = settings.MAX_RESULT_WINDOW
es_index_name = settings.ES_INDEX_NAME

if es_client:
    import elasticsearch
    from elasticsearch_dsl import Search


class ESHomePageView(TemplateView):

    template_name = "viewer/elasticsearch.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:

        if not settings.ES_ENABLED or not es_client:
            return {'message': 'Elasticsearch is disabled for this instance.'}

        s = Search(using=es_client, index=es_index_name)

        message = None
        count_result = 0

        if 'clear' in self.request.GET:
            self.request.GET = QueryDict('')

        s = self.gen_es_query(self.request, s)

        s.aggs.bucket('tags__full', 'terms', field='tags.full', size=100)
        s.aggs.bucket('source_type', 'terms', field='source_type', size=20)
        s.aggs.bucket('reason', 'terms', field='reason', size=20)

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
        per_page = 48

        context = super(ESHomePageView, self).get_context_data(**kwargs)

        if count_result > 0:
            es_pagination = self.gen_pagination(self.request, count_result, per_page)

            if es_pagination['search']['to'] > max_result_window:
                es_pagination['search']['from'] = max_result_window - per_page
                es_pagination['search']['to'] = max_result_window
                message = "Refine your search, can't go that far back (limit: {}).".format(max_result_window)

            # Sort
            possible_sorts = (
                'public_date',
                'create_date',
                'original_date',
                'size',
                'image_count',
            )
            sort = self.request.GET.get('sort', '')
            order = self.request.GET.get('order', 'desc')

            if not sort and (sort not in possible_sorts):
                if not self.request.user.is_authenticated:
                    sort = 'public_date'
                else:
                    sort = 'create_date'
            if order == 'desc':
                sort = '-' + sort

            s = s.sort(sort)

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

    def prepare_facet_data(self, aggregations: AggResponse, get_args: QueryDict) -> dict[str, list[dict[str, str]]]:
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
            if field_name in ('page', 'q', 'order', 'sort', 'metrics'):
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


# Not being used client-side
def autocomplete_view(request: HttpRequest) -> HttpResponse:
    if not settings.ES_ENABLED or not es_client:
        return HttpResponse({})

    query = request.GET.get('q', '')

    s = Search(using=es_client, index=es_index_name)

    resp = s.suggest(
        'title_complete',
        query,
        completion={
            "field": 'title_complete',
        }
    )
    options = resp['title_complete'][0]['options']
    data = json.dumps(
        [{'id': i['_id'], 'value': i['text']} for i in options]
    )
    mime_type = 'application/json; charset=utf-8'
    return HttpResponse(data, mime_type)


# Not being used client-side
def title_suggest_view(request: HttpRequest) -> HttpResponse:
    query = request.GET.get('q', '')
    s = Search(using=es_client, index=es_index_name) \
        .source(['title']) \
        .query("match", title_suggest={'query': query, 'operator': 'and', 'fuzziness': 'AUTO'})
    response = s.execute()

    data = json.dumps(
        [{'id': i.meta.id, 'value': i.title} for i in response]
    )
    mime_type = 'application/json; charset=utf-8'
    return HttpResponse(data, mime_type)
