from typing import Union

from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.paginator import Paginator

from viewer.models import Archive, Gallery

crawler_settings = settings.CRAWLER_SETTINGS


class Command(BaseCommand):
    help = "Recreate index and push data in bulk."

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from elasticsearch import Elasticsearch, RequestsHttpConnection

        # TODO: Timeout as option.
        self.es_client = Elasticsearch(
            [crawler_settings.elasticsearch.url],
            connection_class=RequestsHttpConnection,
            timeout=crawler_settings.elasticsearch.timeout,
        )

    def add_arguments(self, parser):
        parser.add_argument('-r', '--recreate_index',
                            required=False,
                            action='store_true',
                            default=False,
                            help='Creates index and puts mapping in it.')
        parser.add_argument('-p', '--push_to_index',
                            required=False,
                            action='store_true',
                            default=False,
                            help='Calls converter and uploads data to the index.')
        parser.add_argument('-rg', '--recreate_index_gallery',
                            required=False,
                            action='store_true',
                            default=False,
                            help='Creates index and puts mapping in it (Gallery).')
        parser.add_argument('-pg', '--push_to_index_gallery',
                            required=False,
                            action='store_true',
                            default=False,
                            help='Calls converter and uploads data to the index (Gallery).')
        parser.add_argument('-bs', '--bulk_size',
                            required=False,
                            action='store',
                            type=int,
                            help='Specify bulk size when adding to index.')

    def handle(self, *args, **options):

        if options['recreate_index']:
            self.recreate_index_model(Archive)
        if options['recreate_index_gallery']:
            self.recreate_index_model(Gallery)
        if options['push_to_index']:
            self.push_db_to_index_model(Archive, options['bulk_size'])
        if options['push_to_index_gallery']:
            self.push_db_to_index_model(Gallery, options['bulk_size'])

    def recreate_index_model(self, model: Union[type[Gallery], type[Archive]]):

        from elasticsearch.client.indices import IndicesClient

        indices_client = IndicesClient(client=self.es_client)
        index_name = model._meta.es_index_name  # type: ignore
        if indices_client.exists(index=index_name):
            indices_client.delete(index=index_name)
        indices_client.create(index=index_name)
        indices_client.close(index=index_name)
        indices_client.put_settings(
            index=index_name,
            body={
                "index": {"max_result_window": settings.MAX_RESULT_WINDOW},
                "analysis": {
                    "filter": {
                        "edge_ngram_filter": {
                            "type": "edge_ngram",
                            "min_gram": 2,
                            "max_gram": 20
                        }
                    },
                    "analyzer": {
                        "edge_ngram_analyzer": {
                            "type": "custom",
                            "tokenizer": "standard",
                            "filter": [
                                "lowercase",
                                "edge_ngram_filter"
                            ]
                        }
                    }
                }
            }
        )
        indices_client.put_mapping(
            body=model._meta.es_mapping,  # type: ignore
            index=index_name,
        )
        indices_client.open(index=index_name)

    def push_db_to_index_model(self, model: Union[type[Gallery], type[Archive]], bulk_size: int = 0):

        from elasticsearch.helpers import bulk

        if settings.ES_ONLY_INDEX_PUBLIC:
            query = model.objects.filter(public=True).prefetch_related('tags').order_by('-pk')
        else:
            query = model.objects.all().prefetch_related('tags').order_by('-pk')

        if not bulk_size:
            data = [
                self.convert_for_bulk(s, 'create') for s in query
            ]
            bulk(client=self.es_client, actions=data, stats_only=True, raise_on_error=False, request_timeout=30)
        else:
            paginator = Paginator(query, bulk_size)

            for page_number in paginator.page_range:
                self.stdout.write(
                    "Bulk sending page {} of {}.".format(
                        page_number,
                        paginator.num_pages
                    )
                )
                page = paginator.page(page_number)
                data = [
                    self.convert_for_bulk(s, 'create') for s in page
                ]

                bulk(client=self.es_client, actions=data, stats_only=True, raise_on_error=False, request_timeout=30)

    def convert_for_bulk(self, django_object, action=None):
        data = django_object.es_repr()
        metadata = {
            '_op_type': action,
            "_index": django_object._meta.es_index_name,
        }
        data.update(**metadata)
        return data
