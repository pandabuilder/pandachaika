from typing import Union, Type

from django.conf import settings
from django.core.management.base import BaseCommand
from viewer.models import Archive, Gallery

crawler_settings = settings.CRAWLER_SETTINGS


class Command(BaseCommand):
    help = "Recreate index and push data in bulk."

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from elasticsearch import Elasticsearch, RequestsHttpConnection

        self.es_client = Elasticsearch(
            [crawler_settings.elasticsearch.url],
            connection_class=RequestsHttpConnection
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

    def handle(self, *args, **options):

        if options['recreate_index']:
            self.recreate_index_model(Archive)
        if options['recreate_index_gallery']:
            self.recreate_index_model(Gallery)
        if options['push_to_index']:
            self.push_db_to_index_model(Archive)
        if options['push_to_index_gallery']:
            self.push_db_to_index_model(Gallery)

    def recreate_index_model(self, model: Union[Type[Gallery], Type[Archive]]):

        from elasticsearch.client import IndicesClient

        indices_client = IndicesClient(client=self.es_client)
        index_name = model._meta.es_index_name
        if indices_client.exists(index_name):
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
            body=model._meta.es_mapping,
            index=index_name,
        )
        indices_client.open(index=index_name)

    def push_db_to_index_model(self, model: Union[Type[Gallery], Type[Archive]]):

        from elasticsearch.helpers import bulk

        if settings.ES_ONLY_INDEX_PUBLIC:
            data = [
                self.convert_for_bulk(s, 'create') for s in model.objects.filter(public=True)
            ]
        else:
            data = [
                self.convert_for_bulk(s, 'create') for s in model.objects.all()
            ]
        bulk(client=self.es_client, actions=data, stats_only=True)

    def convert_for_bulk(self, django_object, action=None):
        data = django_object.es_repr()
        metadata = {
            '_op_type': action,
            "_index": django_object._meta.es_index_name,
        }
        data.update(**metadata)
        return data
