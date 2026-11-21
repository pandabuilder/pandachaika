import json
import logging
from typing import Any

from urllib.parse import quote_plus, urlencode
from datetime import datetime

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse, QueryDict, HttpRequest, JsonResponse
from django.urls import reverse
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
    from elasticsearch.dsl import Search, Q
    from elasticsearch.dsl.response import AggResponse


ES_SKIP_FIELDS = ("page", "q", "order", "sort", "metrics", "show_url", "count", "no_agg", "view")

ES_QUERY_PARSE_ERROR_MSG = (
    'Error parsing the query string, check your syntax. This characters must be escaped: '
    '+ - = && || > < ! ( ) { } [ ] ^ " ~ * ? : \\ /'
)


def _parse_es_datetime(value: str | None) -> tuple[datetime | None, str]:
    if not value:
        return None, ""
    dt = datetime.fromisoformat(value)
    return dt, f"{dt.month:02d}/{dt.day:02d}/{dt.year:04d}"


def _extract_total_hits(search_result: Any) -> int:
    total = search_result.hits.total
    if isinstance(total, int):
        return total
    return int(total.value)


def _parse_page_and_per_page(get_args: QueryDict, accepted_per_page: list[int]) -> tuple[int, int]:
    try:
        per_page = int(get_args.get("count", "48"))
        if per_page not in accepted_per_page:
            per_page = 48
    except ValueError:
        per_page = 48

    try:
        page = int(get_args.get("page", "1"))
        if page < 1:
            page = 1
    except ValueError:
        page = 1

    return page, per_page


def _build_facet_data(
    aggregations: "AggResponse | None", get_args: QueryDict, *, include_url_args: bool = True
) -> dict[str, Any]:
    if not aggregations:
        return {}
    resp: DataDict = {}
    base_dict = get_args.dict() if include_url_args else None
    base_keys = list(base_dict.keys()) if base_dict is not None else []
    full_base_qs = urlencode(base_dict) if base_dict is not None else ""

    for area, agg in aggregations.to_dict().items():
        if "value" in agg:  # if the aggregation has the value key, it comes from the metrics.
            resp[area] = agg["value"]
            continue

        raw_area_val = get_args.get(area)
        base_list = raw_area_val.split(",") if raw_area_val else []
        active_set = set(base_list)
        buckets = aggregations[area].buckets

        if not include_url_args:
            resp[area] = [
                {
                    "name": item.key,
                    "count": item.doc_count,
                    "is_active": str(item.key) in active_set,
                }
                for item in buckets
            ]
            continue

        assert base_dict is not None
        encoded_area = quote_plus(area)
        if area in base_dict:
            idx = base_keys.index(area)
            prefix_qs = urlencode([(k, base_dict[k]) for k in base_keys[:idx]])
            suffix_qs = urlencode([(k, base_dict[k]) for k in base_keys[idx + 1 :]])
        else:
            prefix_qs = full_base_qs
            suffix_qs = ""
        encoded_base = [quote_plus(v) for v in base_list]

        items: list[dict[str, Any]] = []
        for item in buckets:
            val_str = str(item.key)
            if val_str in active_set:
                is_active = True
                remove_idx = base_list.index(val_str)
                new_encoded_val = "%2C".join(encoded_base[:remove_idx] + encoded_base[remove_idx + 1 :])
            else:
                is_active = False
                enc_val = quote_plus(val_str)
                new_encoded_val = "%2C".join((*encoded_base, enc_val)) if encoded_base else enc_val

            area_pair = f"{encoded_area}={new_encoded_val}"
            if prefix_qs and suffix_qs:
                url_args_str = f"{prefix_qs}&{area_pair}&{suffix_qs}"
            elif prefix_qs:
                url_args_str = f"{prefix_qs}&{area_pair}"
            elif suffix_qs:
                url_args_str = f"{area_pair}&{suffix_qs}"
            else:
                url_args_str = area_pair

            items.append(
                {
                    "url_args": url_args_str,
                    "name": item.key,
                    "count": item.doc_count,
                    "is_active": is_active,
                }
            )
        resp[area] = items
    return resp


class ESHomePageView(TemplateView):

    template_name = "viewer/elasticsearch.html"
    index_name = es_index_name
    page_title = "Archive Search"

    possible_sorts = [
        "public_date",
        "create_date",
        "original_date",
        "size",
        "image_count",
    ]

    aggs_bucket_fields = [
        ("tags__full", "tags.full", 100),
        ("source_type", "source_type", 20),
        ("reason", "reason", 20),
    ]

    list_source_fields = [
        "title",
        "title_jpn",
        "thumbnail",
        "image_count",
        "size",
        "public_date",
        "original_date",
    ]

    extended_source_fields = [
        "title",
        "title_jpn",
        "thumbnail",
        "image_count",
        "size",
        "public_date",
        "original_date",
        "tags",
        "category",
        "reason",
    ]

    recall_source_fields = ["source_url", "provider"]

    public_sort_field = "public_date"

    accepted_per_page = [24, 48, 100, 200, 300]

    extra_view_options = ("list", "extended")
    view_parameters_name = "parameters"
    view_cookie_name = "panda_view"
    item_url_name = "viewer:archive"

    def get_item_url_base(self) -> str:
        try:
            return reverse(self.item_url_name, args=[0])[:-2]
        except Exception:
            return f"/{self.item_url_name.split(':')[-1]}/"

    def get_source_fields(self, view_mode: str, is_authenticated: bool, is_recall_api: bool) -> list[str]:
        if is_recall_api:
            return list(self.recall_source_fields)
        fields = list(self.extended_source_fields if view_mode == "extended" else self.list_source_fields)
        if is_authenticated:
            fields.append("create_date")
        return fields

    def _resolve_sort(self) -> str:
        sort = self.request.GET.get("sort", "")
        order = self.request.GET.get("order", "desc")
        if not sort and (sort not in self.possible_sorts):
            if not self.request.user.is_authenticated:
                sort = self.public_sort_field
            else:
                sort = "create_date"
        if order == "desc":
            sort = "-" + sort
        return sort

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:

        if not settings.ES_ENABLED or not es_client:
            return {"message": "Elasticsearch is disabled for this instance.", "page_title": self.page_title}

        is_recall_api = "recall_api" in self.request.POST and self.request.user.has_perm("viewer.update_metadata")

        if "clear" in self.request.GET:
            self.request.GET = QueryDict("")

        if is_recall_api and not self.request.GET.get("q", ""):
            return {
                "message": "You should use a filter term when queueing galleries for update.",
                "page_title": self.page_title,
            }

        existing_parameters = self.request.session.get(self.view_parameters_name)
        parameters = dict(existing_parameters) if isinstance(existing_parameters, dict) else {}
        self._pending_view_cookie: tuple[str, str | None] | None = None

        if not self.request.user.is_authenticated:
            cookie_view = self.request.COOKIES.get(self.view_cookie_name, "")
            if cookie_view in self.extra_view_options:
                parameters["view"] = cookie_view

        view_option = self.request.GET.get("view", "")
        if view_option in self.extra_view_options:
            if self.request.user.is_authenticated:
                if view_option != parameters.get("view"):
                    parameters["view"] = view_option
                    self.request.session[self.view_parameters_name] = parameters
            else:
                parameters["view"] = view_option
                if view_option == "list":
                    if self.view_cookie_name in self.request.COOKIES:
                        self._pending_view_cookie = (self.view_cookie_name, None)
                elif self.request.COOKIES.get(self.view_cookie_name) != view_option:
                    self._pending_view_cookie = (self.view_cookie_name, view_option)

        if "view" not in parameters or parameters["view"] == "":
            parameters["view"] = "list"

        include_aggs = (not is_recall_api) and ("no_agg" not in self.request.GET)
        include_metrics = (not is_recall_api) and ("metrics" in self.request.GET)

        s = Search(using=es_client, index=self.index_name).extra(track_total_hits=True)
        s = self.gen_es_query(self.request, s)
        s = s.source(self.get_source_fields(parameters["view"], self.request.user.is_authenticated, is_recall_api))
        s = s.sort(self._resolve_sort())

        if include_aggs:
            for bucket_name, field, size in self.aggs_bucket_fields:
                s.aggs.bucket(bucket_name, "terms", field=field, size=size)

        if include_metrics:
            s.aggs.metric("avg_size", "avg", field="size")
            s.aggs.metric("sum_size", "sum", field="size")
            s.aggs.metric("max_size", "max", field="size")
            s.aggs.metric("min_size", "min", field="size")
            s.aggs.metric("avg_count", "avg", field="image_count")
            s.aggs.metric("sum_count", "sum", field="image_count")
            s.aggs.metric("max_count", "max", field="image_count")
            s.aggs.metric("min_count", "min", field="image_count")

        message = None
        context = super(ESHomePageView, self).get_context_data(**kwargs)
        item_url_base = self.get_item_url_base()

        if is_recall_api:
            try:
                search_result = s[0:max_result_window].execute()
            except elasticsearch.exceptions.NotFoundError:
                return {"message": "Expected index does not exist.", "page_title": self.page_title}
            except elasticsearch.exceptions.RequestError:
                message = ES_QUERY_PARSE_ERROR_MSG
                search_result = None

            if search_result is not None:
                count_result = _extract_total_hits(search_result)
                if count_result > 0:
                    hits_list = list(search_result)
                    context["count_result"] = count_result
                    context["gallery_links"] = [c.source_url for c in hits_list]
                    context["gallery_providers"] = list(set(c.provider for c in hits_list))
        else:
            page, per_page = _parse_page_and_per_page(self.request.GET, self.accepted_per_page)
            search_from = (page - 1) * per_page
            search_to = search_from + per_page
            if search_to > max_result_window:
                search_from = max(0, max_result_window - per_page)
                search_to = max_result_window

            try:
                search_result = s[search_from:search_to].execute()
            except elasticsearch.exceptions.NotFoundError:
                return {"message": "Expected index does not exist.", "page_title": self.page_title}
            except elasticsearch.exceptions.RequestError:
                message = ES_QUERY_PARSE_ERROR_MSG
                search_result = None

            if search_result is not None:
                count_result = _extract_total_hits(search_result)
                if count_result > 0:
                    es_pagination = self.gen_pagination(self.request, count_result, per_page)

                    if es_pagination["search"]["to"] > max_result_window:
                        es_pagination["search"]["from"] = max(0, max_result_window - per_page)
                        es_pagination["search"]["to"] = max_result_window
                        message = "Refine your search, can't go that far back (limit: {}).".format(max_result_window)

                    if es_pagination["search"]["from"] != search_from:
                        search_result = s[es_pagination["search"]["from"] : es_pagination["search"]["to"]].execute()

                    context["hits"] = [self.convert_hit_to_template(c, item_url_base) for c in search_result]

                    context["results"] = {
                        "from": es_pagination["search"]["from"] + 1,
                        "to": es_pagination["search"]["from"] + len(context["hits"]),
                    }

                    if (include_aggs or include_metrics) and hasattr(search_result, "aggregations"):
                        context["aggregations"] = self.prepare_facet_data(
                            search_result.aggregations, self.request.GET, include_url_args=False
                        )
                    context["paginator"] = es_pagination

        if self.extra_view_options:
            context["extra_view_options"] = self.extra_view_options

        context["view_parameters"] = parameters
        context["q"] = self.request.GET.get("q", "")
        context["sort"] = self.request.GET.get("sort", "")
        context["order"] = self.request.GET.get("order", "desc")
        context["message"] = message
        context["page_title"] = self.page_title

        return context

    def render_to_response(self, context: dict[str, Any], **response_kwargs: Any) -> HttpResponse:
        response = super().render_to_response(context, **response_kwargs)
        pending_cookie = getattr(self, "_pending_view_cookie", None)
        if pending_cookie is not None:
            cookie_name, cookie_value = pending_cookie
            if cookie_value is None:
                response.delete_cookie(cookie_name, samesite="Lax")
            else:
                response.set_cookie(cookie_name, cookie_value, max_age=30 * 24 * 3600, samesite="Lax")
        return response

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
    def convert_hit_to_template(hit, item_url_base: str = "/archive/"):
        hit.pk = hit.meta.id
        hit.url = f"{item_url_base}{hit.pk}/"
        hit.create_date_c, hit.create_date_formatted = _parse_es_datetime(getattr(hit, "create_date", None))
        hit.public_date_c, hit.public_date_formatted = _parse_es_datetime(getattr(hit, "public_date", None))
        hit.original_date_c, hit.original_date_formatted = _parse_es_datetime(getattr(hit, "original_date", None))
        return hit

    @staticmethod
    def facet_url_args(url_args, field_name, field_value):
        is_active = False
        if url_args.get(field_name):
            base_list = url_args[field_name].split(",")
            if field_value in base_list:
                del base_list[base_list.index(field_value)]
                is_active = True
            else:
                base_list.append(field_value)
            url_args[field_name] = ",".join(base_list)
        else:
            url_args[field_name] = field_value
        return url_args, is_active

    def prepare_facet_data(
        self, aggregations: "AggResponse", get_args: QueryDict, *, include_url_args: bool = True
    ) -> dict[str, list[dict[str, str]]]:
        return _build_facet_data(aggregations, get_args, include_url_args=include_url_args)

    @staticmethod
    def gen_es_query(request, s):
        req_dict = request.GET.dict()
        if not request.user.is_authenticated:
            s = s.filter("term", public=True)
        if not req_dict:
            return s.filter("match_all")
        q = req_dict.get("q", "")
        if q:
            s = s.query("query_string", query=q, fields=["title", "title_jpn", "tags.full"])
        else:
            s = s.query("match_all")
        for field_name, raw_val in req_dict.items():
            if field_name in ES_SKIP_FIELDS:
                continue
            filter_field_name = field_name.replace("__", ".") if "__" in field_name else field_name
            for field_value in raw_val.split(","):
                if not field_value:
                    continue
                if field_value.startswith("-"):
                    s = s.exclude("term", **{filter_field_name: field_value.replace("-", "")})
                else:
                    s = s.filter("term", **{filter_field_name: field_value})
        return s

    def gen_pagination(self, request: HttpRequest, count: int, per_page: int) -> dict[str, Any]:

        paginator: dict[str, Any] = {}

        try:
            page = int(request.GET.get("page", "1"))
            if page < 1:
                page = 1
        except ValueError:
            page = 1

        num_pages = self.num_pages(count, per_page)

        number = page
        if number > num_pages:
            number = num_pages

        paginator["count"] = count
        paginator["number"] = number
        paginator["num_pages"] = num_pages
        paginator["page_range"] = list(
            range(
                max(1, number - 3 - max(0, number - (num_pages - 3))),
                min(num_pages + 1, number + 3 + 1 - min(0, number - 3 - 1)),
            )
        )
        bottom = (number - 1) * per_page
        if bottom < 1:
            bottom = 0
        top = bottom + per_page
        if top >= count:
            top = count

        paginator["search"] = {"from": bottom, "to": top}

        return paginator


class ESHomeGalleryPageView(ESHomePageView):

    template_name = "viewer/elasticsearch_gallery.html"
    index_name = settings.ES_GALLERY_INDEX_NAME
    page_title = "Gallery Search"

    extra_view_options = ("list", "extended")

    possible_sorts = [
        "create_date",
        "posted_date",
        "size",
        "image_count",
        "provider",
    ]

    aggs_bucket_fields = [
        ("tags__full", "tags.full", 100),
        ("category", "category", 20),
        ("provider", "provider", 20),
        ("reason", "reason", 20),
    ]

    list_source_fields = [
        "title",
        "title_jpn",
        "thumbnail",
        "image_count",
        "size",
        "category",
        "posted_date",
    ]

    extended_source_fields = [
        "title",
        "title_jpn",
        "thumbnail",
        "image_count",
        "size",
        "category",
        "posted_date",
        "tags",
        "source_url",
        "provider",
        "reason",
        "uploader",
        "rating",
    ]

    public_sort_field = "posted_date"

    view_parameters_name = "gallery_parameters"
    view_cookie_name = "panda_gallery_view"
    item_url_name = "viewer:gallery"

    def get_source_fields(self, view_mode: str, is_authenticated: bool, is_recall_api: bool) -> list[str]:
        fields = super().get_source_fields(view_mode, is_authenticated, is_recall_api)
        if not is_recall_api and view_mode != "extended" and self.request.GET.get("show_url") == "1":
            fields.append("source_url")
        return fields

    @staticmethod
    def convert_hit_to_template(hit, item_url_base: str = "/gallery/"):
        hit.pk = hit.meta.id
        hit.url = f"{item_url_base}{hit.pk}/"
        hit.create_date_c, hit.create_date_formatted = _parse_es_datetime(getattr(hit, "create_date", None))
        hit.posted_date_c, hit.posted_date_formatted = _parse_es_datetime(getattr(hit, "posted_date", None))
        return hit

    def post(self, request, *args, **kwargs):
        context = self.get_context_data(**kwargs)

        if "recall_api" in request.POST and request.user.has_perm("viewer.update_metadata"):
            if "count_result" in context:
                p = request.POST

                message = "Recalling API for {} galleries".format(context["count_result"])
                logger.info(message)
                messages.success(request, message)

                if "reason" in p and p["reason"] != "":
                    reason = p["reason"]
                else:
                    reason = ""

                galleries_update_metadata(
                    context["gallery_links"],
                    context["gallery_providers"],
                    request.user,
                    reason,
                    settings.CRAWLER_SETTINGS,
                )
            else:
                context["message"] = "No galleries updated."

        return self.render_to_response(context)


# Not being used client-side
def autocomplete_view(request: HttpRequest) -> HttpResponse:
    if not settings.ES_ENABLED or not es_client:
        return JsonResponse({})

    query = request.GET.get("q", "")

    s = Search(using=es_client, index=es_index_name)

    try:
        response = s.suggest(
            "title_complete",
            query,
            completion={
                "field": "title_complete",
            },
        ).execute()
    except elasticsearch.exceptions.NotFoundError:
        return JsonResponse({})

    options = response["title_complete"][0]["options"]
    data = json.dumps([{"id": i["_id"], "title": i["text"]} for i in options])
    mime_type = "application/json; charset=utf-8"
    http_response = HttpResponse(data, mime_type)
    return http_response


def title_suggest_view(request: HttpRequest) -> HttpResponse:
    if not settings.ES_ENABLED or not es_client:
        return JsonResponse({})

    query = request.GET.get("q", "")
    s = Search(using=es_client, index=es_index_name).suggest(
        "title-suggestion",
        text=query,
        phrase={
            "field": "title_suggest",
            "size": 5,
            # "gram_size": 10,
            # "confidence": 0.0,
            "direct_generator": [
                {
                    "field": "title_suggest",
                    "suggest_mode": "always",
                    "min_word_length": 3,
                }
            ],
        },
    )
    try:
        response = s.execute()
    except elasticsearch.exceptions.NotFoundError:
        return JsonResponse({})

    suggestion_parts = [part["text"] for part in response["suggest"]["title-suggestion"][0]["options"]]

    data = json.dumps({"suggestions": suggestion_parts})
    mime_type = "application/json; charset=utf-8"
    http_response = HttpResponse(data, mime_type)
    return http_response


def title_suggest_archive_view(request: HttpRequest) -> HttpResponse:
    if not settings.ES_ENABLED or not es_client:
        return JsonResponse({})

    query = request.GET.get("q", "")
    s = (
        Search(using=es_client, index=es_index_name)
        .source(["title", "thumbnail"])
        .query("match", title_suggest={"query": query, "operator": "and", "fuzziness": "AUTO"})
    )
    try:
        response = s.execute()
    except elasticsearch.exceptions.NotFoundError:
        return JsonResponse({})

    data = json.dumps([{"id": i.meta.id, "title": i.title, "thumbnail": i.thumbnail} for i in response])
    mime_type = "application/json; charset=utf-8"
    http_response = HttpResponse(data, mime_type)
    return http_response


def title_pk_suggest_archive_view(request: HttpRequest) -> HttpResponse:
    if not settings.ES_ENABLED or not es_client:
        return JsonResponse({})

    query = request.GET.get("q", "")
    s = (
        Search(using=es_client, index=es_index_name)
        .source(["title", "thumbnail"])
        .query(
            "bool",
            should=[
                Q("term", _id={"value": query, "boost": 999}),
                Q("match", title_suggest={"query": query, "operator": "and", "fuzziness": "AUTO"}),
            ],
        )
    )
    try:
        response = s.execute()
    except elasticsearch.exceptions.NotFoundError:
        return JsonResponse({})

    data = json.dumps([{"id": i.meta.id, "title": i.title, "thumbnail": i.thumbnail} for i in response])
    mime_type = "application/json; charset=utf-8"
    http_response = HttpResponse(data, mime_type)
    return http_response


def archive_simple(request: HttpRequest) -> HttpResponse:
    if not settings.ES_ENABLED or not es_client:
        return JsonResponse({})

    query = request.GET.get("q", "")
    s = Search(using=es_client, index=es_index_name).source(["title", "thumbnail"]).query("term", _id=query)
    try:
        response = s.execute()
    except elasticsearch.exceptions.NotFoundError:
        return JsonResponse({})

    if len(response) > 0:
        i = response[0]
        data = json.dumps({"id": i.meta.id, "title": i.title, "thumbnail": i.thumbnail})
    else:
        data = json.dumps({})
    mime_type = "application/json; charset=utf-8"
    http_response = HttpResponse(data, mime_type)
    return http_response


def archives_simple(request: HttpRequest) -> HttpResponse:
    if not settings.ES_ENABLED or not es_client:
        return JsonResponse({})

    query = request.GET.getlist("q", "")
    s = Search(using=es_client, index=es_index_name).source(["title", "thumbnail"]).query("terms", _id=query)
    try:
        response = s.execute()
    except elasticsearch.exceptions.NotFoundError:
        return JsonResponse({})

    data = json.dumps([{"id": i.meta.id, "title": i.title, "thumbnail": i.thumbnail} for i in response])
    mime_type = "application/json; charset=utf-8"
    http_response = HttpResponse(data, mime_type)
    return http_response


class ESArchiveJSONView(View):

    index_name = es_index_name

    possible_sorts = [
        "public_date",
        "create_date",
        "original_date",
        "size",
        "image_count",
    ]

    aggs_bucket_fields = [("tags__full", "tags.full", 50), ("source_type", "source_type", 20), ("reason", "reason", 20)]

    accepted_per_page = [24, 48, 100, 200, 300]

    public_sort_field = "public_date"

    def get(self, request, *args, **kwargs):
        data = self.get_context_data()
        return HttpResponse(json.dumps(data), content_type="application/json; charset=utf-8")

    def _resolve_sort(self) -> str:
        sort = self.request.GET.get("sort", "")
        order = self.request.GET.get("order", "desc")
        if not sort and (sort not in self.possible_sorts):
            if not self.request.user.is_authenticated:
                sort = self.public_sort_field
            else:
                sort = "create_date"
        if order == "desc":
            sort = "-" + sort
        return sort

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:

        if not settings.ES_ENABLED or not es_client:
            return {"message": "Elasticsearch is disabled for this instance."}

        if "clear" in self.request.GET:
            self.request.GET = QueryDict("")

        excludes = ["doc", "title_suggest", "title_complete"]
        if not self.request.user.is_authenticated:
            excludes.append("create_date")

        include_aggs = "no_agg" not in self.request.GET
        include_metrics = "metrics" in self.request.GET

        s = Search(using=es_client, index=self.index_name).extra(track_total_hits=True).source(excludes=excludes)
        s = self.gen_es_query(self.request, s)
        s = s.sort(self._resolve_sort())

        if include_aggs:
            for bucket_name, field, size in self.aggs_bucket_fields:
                s.aggs.bucket(bucket_name, "terms", field=field, size=size)

        if include_metrics:
            s.aggs.metric("avg_size", "avg", field="size")
            s.aggs.metric("sum_size", "sum", field="size")
            s.aggs.metric("max_size", "max", field="size")
            s.aggs.metric("min_size", "min", field="size")
            s.aggs.metric("avg_count", "avg", field="image_count")
            s.aggs.metric("sum_count", "sum", field="image_count")
            s.aggs.metric("max_count", "max", field="image_count")
            s.aggs.metric("min_count", "min", field="image_count")

        page, per_page = _parse_page_and_per_page(self.request.GET, self.accepted_per_page)
        search_from = (page - 1) * per_page
        search_to = search_from + per_page
        if search_to > max_result_window:
            search_from = max(0, max_result_window - per_page)
            search_to = max_result_window

        message = None
        context: dict[str, Any] = {}

        try:
            search_result = s[search_from:search_to].execute()
        except elasticsearch.exceptions.NotFoundError:
            return {"message": "Expected index does not exist."}
        except elasticsearch.exceptions.RequestError:
            message = ES_QUERY_PARSE_ERROR_MSG
            search_result = None

        if search_result is not None:
            count_result = _extract_total_hits(search_result)
            if count_result > 0:
                es_pagination = self.gen_pagination(self.request, count_result, per_page)

                if es_pagination["search"]["to"] > max_result_window:
                    es_pagination["search"]["from"] = max(0, max_result_window - per_page)
                    es_pagination["search"]["to"] = max_result_window
                    message = "Refine your search, can't go that far back (limit: {}).".format(max_result_window)

                if es_pagination["search"]["from"] != search_from:
                    search_result = s[es_pagination["search"]["from"] : es_pagination["search"]["to"]].execute()

                context["hits"] = [self.convert_hit_to_template(c, self.request) for c in search_result]

                context["results"] = {
                    "from": es_pagination["search"]["from"] + 1,
                    "to": es_pagination["search"]["from"] + len(context["hits"]),
                }

                if (include_aggs or include_metrics) and hasattr(search_result, "aggregations"):
                    context["aggregations"] = self.prepare_facet_data(search_result.aggregations, self.request.GET)
                context["paginator"] = es_pagination

        context["q"] = self.request.GET.get("q", "")
        context["sort"] = self.request.GET.get("sort", "")
        context["order"] = self.request.GET.get("order", "desc")
        context["message"] = message

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
        hit_as_dict.pop("doc", None)
        hit_as_dict.pop("title_suggest", None)
        hit_as_dict.pop("title_complete", None)
        if not request.user.is_authenticated:
            hit_as_dict.pop("create_date", None)
        return hit_as_dict

    @staticmethod
    def facet_url_args(url_args, field_name, field_value):
        is_active = False
        if url_args.get(field_name):
            base_list = url_args[field_name].split(",")
            if field_value in base_list:
                del base_list[base_list.index(field_value)]
                is_active = True
            else:
                base_list.append(field_value)
            url_args[field_name] = ",".join(base_list)
        else:
            url_args[field_name] = field_value
        return url_args, is_active

    def prepare_facet_data(
        self, aggregations: "AggResponse", get_args: QueryDict, *, include_url_args: bool = True
    ) -> dict[str, list[dict[str, str]]]:
        return _build_facet_data(aggregations, get_args, include_url_args=include_url_args)

    @staticmethod
    def gen_es_query(request, s):
        req_dict = request.GET.dict()
        if not request.user.is_authenticated:
            s = s.filter("term", public=True)
        if not req_dict:
            return s.filter("match_all")
        q = req_dict.get("q", "")
        if q:
            s = s.query("query_string", query=q, fields=["title", "title_jpn", "tags.full"])
        else:
            s = s.query("match_all")
        for field_name, raw_val in req_dict.items():
            if field_name in ("page", "q", "order", "sort", "metrics", "show_url", "count", "no_agg"):
                continue
            filter_field_name = field_name.replace("__", ".") if "__" in field_name else field_name
            for field_value in raw_val.split(","):
                if not field_value:
                    continue
                if field_value.startswith("-"):
                    s = s.exclude("term", **{filter_field_name: field_value.replace("-", "")})
                else:
                    s = s.filter("term", **{filter_field_name: field_value})
        return s

    def gen_pagination(self, request: HttpRequest, count: int, per_page: int) -> dict[str, Any]:

        paginator: dict[str, Any] = {}

        try:
            page = int(request.GET.get("page", "1"))
            if page < 1:
                page = 1
        except ValueError:
            page = 1

        num_pages = self.num_pages(count, per_page)

        number = page
        if number > num_pages:
            number = num_pages

        paginator["count"] = count
        paginator["number"] = number
        paginator["num_pages"] = num_pages
        paginator["page_range"] = list(
            range(
                max(1, number - 3 - max(0, number - (num_pages - 3))),
                min(num_pages + 1, number + 3 + 1 - min(0, number - 3 - 1)),
            )
        )
        bottom = (number - 1) * per_page
        if bottom < 1:
            bottom = 0
        top = bottom + per_page
        if top >= count:
            top = count

        paginator["search"] = {"from": bottom, "to": top}

        return paginator


class ESGalleryJSONView(ESArchiveJSONView):

    index_name = settings.ES_GALLERY_INDEX_NAME

    possible_sorts = [
        "create_date",
        "posted_date",
        "size",
        "image_count",
        "provider",
    ]

    aggs_bucket_fields = [
        ("tags__full", "tags.full", 50),
        ("category", "category", 20),
        ("provider", "provider", 20),
        ("reason", "reason", 20),
    ]

    public_sort_field = "posted_date"
