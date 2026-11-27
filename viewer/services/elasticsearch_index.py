from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
import contextvars
from datetime import datetime, timezone
import logging
import typing

from django.conf import settings
from django.db import models, transaction

from core.base.types import DataDict

logger = logging.getLogger(__name__)


class _IndexBatch:
    """Per-context accumulator for deferred bulk Elasticsearch index/delete operations."""

    __slots__ = ("operations", "has_pending_callbacks")

    def __init__(self) -> None:
        # Maps (model_class, pk) -> "index" | "delete"
        self.operations: dict[tuple[type[models.Model], typing.Any], str] = {}
        self.has_pending_callbacks: bool = False

    def record_index(self, model: type[models.Model], pk: typing.Any) -> None:
        self.operations[(model, pk)] = "index"

    def record_delete(self, model: type[models.Model], pk: typing.Any) -> None:
        self.operations[(model, pk)] = "delete"


_index_batch: contextvars.ContextVar[_IndexBatch | None] = contextvars.ContextVar(
    "viewer_es_index_batch", default=None
)


class ElasticsearchIndexService:
    """Shared Elasticsearch serialization, single/bulk indexing, and index management service."""

    DEFAULT_BULK_CHUNK_SIZE = 500

    @classmethod
    def get_client(cls) -> typing.Any:
        return getattr(settings, "ES_CLIENT", None)

    @classmethod
    def create_client(cls) -> typing.Any:
        from elasticsearch import Elasticsearch

        es_cfg = settings.CRAWLER_SETTINGS.elasticsearch
        return Elasticsearch(
            [es_cfg.url],
            request_timeout=es_cfg.timeout,
        )

    @classmethod
    def get_index_name(cls, obj_or_model: models.Model | type[models.Model]) -> str:
        return typing.cast(str, getattr(obj_or_model._meta, "es_index_name"))

    @classmethod
    def get_mapping(cls, obj_or_model: models.Model | type[models.Model]) -> DataDict:
        return typing.cast(DataDict, getattr(obj_or_model._meta, "es_mapping"))

    @classmethod
    def is_autorefresh_enabled(cls, obj_or_model: models.Model | type[models.Model]) -> bool:
        setting_name = getattr(obj_or_model, "es_autorefresh_setting", "ES_AUTOREFRESH")
        return bool(getattr(settings, setting_name, False))

    @classmethod
    def should_index(cls, instance: models.Model, *, respect_public_only: bool = True) -> bool:
        if instance.pk is None:
            return False
        if respect_public_only and getattr(settings, "ES_ONLY_INDEX_PUBLIC", False):
            return bool(getattr(instance, "public", True))
        return True

    @staticmethod
    def format_datetime(value: datetime | None) -> str | None:
        if value:
            return value.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
        return None

    @classmethod
    def serialize(cls, instance: models.Model) -> DataDict:
        data: DataDict = {"_id": instance.pk}
        mapping = cls.get_mapping(instance)
        for field_name in mapping["properties"].keys():
            data[field_name] = cls.serialize_field(instance, field_name)
        return data

    @classmethod
    def serialize_field(cls, instance: models.Model, field_name: str) -> typing.Any:
        config = cls.get_mapping(instance)["properties"][field_name]
        getter = getattr(instance, f"get_es_{field_name}", None)
        if callable(getter):
            return getter()
        if config["type"] == "object":
            related_object = getattr(instance, field_name)
            if related_object:
                field_es_value: DataDict = {"_id": related_object.pk}
                for prop in config["properties"].keys():
                    field_es_value[prop] = getattr(related_object, prop)
                return field_es_value
            return None
        return getattr(instance, field_name)

    @classmethod
    def _instance_repr(cls, instance: models.Model) -> DataDict:
        es_repr = getattr(instance, "es_repr", None)
        if callable(es_repr):
            return typing.cast(DataDict, es_repr())
        return cls.serialize(instance)

    @classmethod
    def to_bulk_action(
        cls,
        instance: models.Model,
        op_type: str = "update",
        *,
        doc_as_upsert: bool = True,
    ) -> DataDict:
        data = dict(cls._instance_repr(instance))
        index_name = cls.get_index_name(instance)
        if op_type == "update":
            doc_id = data.pop("_id")
            action: DataDict = {
                "_op_type": "update",
                "_index": index_name,
                "_id": str(doc_id),
                "doc": data,
            }
            if doc_as_upsert:
                action["doc_as_upsert"] = True
            return action
        data["_op_type"] = op_type
        data["_index"] = index_name
        return data

    @classmethod
    def to_delete_bulk_action(cls, model: type[models.Model], pk: typing.Any) -> DataDict:
        return {
            "_op_type": "delete",
            "_index": cls.get_index_name(model),
            "_id": str(pk),
        }

    @classmethod
    def build_index_settings(cls) -> DataDict:
        return {
            "index": {"max_result_window": settings.MAX_RESULT_WINDOW},
            "analysis": {
                "filter": {"edge_ngram_filter": {"type": "edge_ngram", "min_gram": 2, "max_gram": 20}},
                "analyzer": {
                    "edge_ngram_analyzer": {
                        "type": "custom",
                        "tokenizer": "standard",
                        "filter": ["lowercase", "edge_ngram_filter"],
                    }
                },
            },
        }

    @classmethod
    def recreate_index(
        cls,
        index_name: str,
        mapping: DataDict,
        *,
        client: typing.Any = None,
    ) -> None:
        es_client = client or cls.get_client() or cls.create_client()
        indices_client = es_client.indices
        if indices_client.exists(index=index_name):
            indices_client.delete(index=index_name)
        indices_client.create(index=index_name)
        indices_client.close(index=index_name)
        indices_client.put_settings(
            index=index_name,
            body=cls.build_index_settings(),
        )
        indices_client.put_mapping(
            body=mapping,
            index=index_name,
        )
        indices_client.open(index=index_name)

    @classmethod
    def recreate_model_index(
        cls,
        model: type[models.Model],
        *,
        client: typing.Any = None,
    ) -> None:
        cls.recreate_index(
            cls.get_index_name(model),
            cls.get_mapping(model),
            client=client,
        )

    @classmethod
    def get_queryset_for_indexing(
        cls,
        model: type[models.Model],
        *,
        pks: Iterable[typing.Any] | None = None,
        respect_public_only: bool = True,
        order_by_pk_desc: bool = False,
    ) -> models.QuerySet[typing.Any]:
        manager = getattr(model, "objects")
        if pks is not None:
            qs = manager.filter(pk__in=pks)
            if respect_public_only and getattr(settings, "ES_ONLY_INDEX_PUBLIC", False):
                qs = qs.filter(public=True)
        elif respect_public_only and getattr(settings, "ES_ONLY_INDEX_PUBLIC", False):
            qs = manager.filter(public=True)
        else:
            qs = manager.all()

        select_related_fields: tuple[str, ...] = getattr(model, "es_select_related", ())
        if select_related_fields:
            qs = qs.select_related(*select_related_fields)

        prefetch_related_fields: tuple[str, ...] = getattr(model, "es_prefetch_related", ())
        if prefetch_related_fields:
            qs = qs.prefetch_related(*prefetch_related_fields)

        if order_by_pk_desc:
            qs = qs.order_by("-pk")

        return qs

    @classmethod
    def index_object(
        cls,
        instance: models.Model,
        *,
        refresh: bool | str | None = None,
        client: typing.Any = None,
        force: bool = False,
    ) -> bool:
        es_client = client or cls.get_client()
        if not es_client:
            return False
        if not force and not cls.is_autorefresh_enabled(instance):
            return False
        if not cls.should_index(instance):
            return False

        payload = dict(cls._instance_repr(instance))
        doc_id = payload.pop("_id")
        es_index = cls.get_index_name(instance)
        kwargs: DataDict = {
            "index": es_index,
            "id": str(doc_id),
            "doc": payload,
            "doc_as_upsert": True,
        }
        if refresh is not None:
            kwargs["refresh"] = refresh
        es_client.update(**kwargs)
        return True

    @classmethod
    def delete_object(
        cls,
        obj_or_model: models.Model | type[models.Model],
        pk: typing.Any,
        *,
        refresh: bool | str | None = None,
        client: typing.Any = None,
        force: bool = False,
    ) -> bool:
        import elasticsearch

        es_client = client or cls.get_client()
        if not es_client or pk is None:
            return False
        if not force and not cls.is_autorefresh_enabled(obj_or_model):
            return False

        es_index = cls.get_index_name(obj_or_model)
        kwargs: DataDict = {
            "index": es_index,
            "id": str(pk),
        }
        if refresh is not None:
            kwargs["refresh"] = refresh
        try:
            es_client.delete(**kwargs)
            return True
        except elasticsearch.exceptions.NotFoundError:
            return False

    @classmethod
    def bulk_actions(
        cls,
        actions: list[DataDict],
        *,
        chunk_size: int = DEFAULT_BULK_CHUNK_SIZE,
        refresh: bool | str | None = None,
        client: typing.Any = None,
    ) -> tuple[int, int]:
        if not actions:
            return (0, 0)
        es_client = client or cls.get_client()
        if not es_client:
            return (0, 0)

        from elasticsearch.helpers import bulk

        kwargs: DataDict = {
            "client": es_client,
            "actions": actions,
            "stats_only": True,
            "raise_on_error": False,
        }
        if chunk_size:
            kwargs["chunk_size"] = chunk_size
        if refresh is not None:
            kwargs["refresh"] = refresh
        return typing.cast(tuple[int, int], bulk(**kwargs))

    @classmethod
    def bulk_index(
        cls,
        objects: Iterable[models.Model],
        *,
        op_type: str = "update",
        doc_as_upsert: bool = True,
        chunk_size: int = DEFAULT_BULK_CHUNK_SIZE,
        refresh: bool | str | None = None,
        client: typing.Any = None,
        force: bool = False,
        respect_public_only: bool = True,
    ) -> tuple[int, int]:
        es_client = client or cls.get_client()
        if not es_client:
            return (0, 0)

        actions: list[DataDict] = []
        for obj in objects:
            if not force and not cls.is_autorefresh_enabled(obj):
                continue
            if not cls.should_index(obj, respect_public_only=respect_public_only):
                continue
            actions.append(cls.to_bulk_action(obj, op_type=op_type, doc_as_upsert=doc_as_upsert))

        return cls.bulk_actions(actions, chunk_size=chunk_size, refresh=refresh, client=es_client)

    @classmethod
    def bulk_delete(
        cls,
        items: Iterable[tuple[type[models.Model], typing.Any]],
        *,
        chunk_size: int = DEFAULT_BULK_CHUNK_SIZE,
        refresh: bool | str | None = None,
        client: typing.Any = None,
        force: bool = False,
    ) -> tuple[int, int]:
        es_client = client or cls.get_client()
        if not es_client:
            return (0, 0)

        actions: list[DataDict] = []
        for model, pk in items:
            if pk is None:
                continue
            if not force and not cls.is_autorefresh_enabled(model):
                continue
            actions.append(cls.to_delete_bulk_action(model, pk))

        return cls.bulk_actions(actions, chunk_size=chunk_size, refresh=refresh, client=es_client)

    @classmethod
    def schedule_index(cls, instance: models.Model) -> None:
        if not (cls.get_client() and cls.is_autorefresh_enabled(instance)):
            return
        if not cls.should_index(instance):
            return

        batch = _index_batch.get()
        if batch is not None:
            model_cls = type(instance)
            pk = instance.pk
            batch.has_pending_callbacks = True

            def _enqueue() -> None:
                batch.record_index(model_cls, pk)

            transaction.on_commit(_enqueue)
            return

        conn = transaction.get_connection()
        key = (instance._meta.label, instance.pk)
        if conn.in_atomic_block and any(
            getattr(func, "_es_index_key", None) == key for _, func, _ in conn.run_on_commit
        ):
            return

        def _on_commit() -> None:
            cls.index_object(instance)

        setattr(_on_commit, "_es_index_key", key)
        transaction.on_commit(_on_commit)

    @classmethod
    def schedule_delete(cls, obj_or_model: models.Model | type[models.Model], pk: typing.Any | None = None) -> None:
        target_pk = obj_or_model.pk if pk is None and isinstance(obj_or_model, models.Model) else pk
        if not (cls.get_client() and cls.is_autorefresh_enabled(obj_or_model) and target_pk is not None):
            return

        model_cls = obj_or_model if isinstance(obj_or_model, type) else type(obj_or_model)

        batch = _index_batch.get()
        if batch is not None:
            batch.has_pending_callbacks = True

            def _enqueue() -> None:
                batch.record_delete(model_cls, target_pk)

            transaction.on_commit(_enqueue)
            return

        def _on_commit() -> None:
            cls.delete_object(model_cls, target_pk)

        transaction.on_commit(_on_commit)

    @classmethod
    def _execute_batch(
        cls,
        batch: _IndexBatch,
        *,
        chunk_size: int = DEFAULT_BULK_CHUNK_SIZE,
        refresh: bool | str | None = None,
    ) -> tuple[int, int]:
        if not batch.operations:
            return (0, 0)

        operations = dict(batch.operations)
        batch.operations.clear()

        es_client = cls.get_client()
        if not es_client:
            return (0, 0)

        pks_to_index: dict[type[models.Model], list[typing.Any]] = defaultdict(list)
        items_to_delete: list[tuple[type[models.Model], typing.Any]] = []

        for (model_cls, pk), op in operations.items():
            if not cls.is_autorefresh_enabled(model_cls):
                continue
            if op == "index":
                pks_to_index[model_cls].append(pk)
            elif op == "delete":
                items_to_delete.append((model_cls, pk))

        actions: list[DataDict] = []
        for model_cls, pks in pks_to_index.items():
            qs = cls.get_queryset_for_indexing(model_cls, pks=pks, respect_public_only=True)
            for obj in qs:
                if cls.should_index(obj):
                    actions.append(cls.to_bulk_action(obj, op_type="update", doc_as_upsert=True))

        for model_cls, pk in items_to_delete:
            actions.append(cls.to_delete_bulk_action(model_cls, pk))

        return cls.bulk_actions(actions, chunk_size=chunk_size, refresh=refresh, client=es_client)

    @classmethod
    @contextmanager
    def batch_indexing(
        cls,
        *,
        chunk_size: int = DEFAULT_BULK_CHUNK_SIZE,
        refresh: bool | str | None = None,
    ) -> Iterator[None]:
        """Collect ES index/delete operations inside this block and flush them via bulk API on commit.

        Nested blocks join the outermost batch. Operations inside rolled-back transactions/savepoints
        are automatically dropped because recording runs inside ``transaction.on_commit``.
        """
        if _index_batch.get() is not None:
            yield
            return

        batch = _IndexBatch()
        token = _index_batch.set(batch)
        try:
            yield
        finally:
            _index_batch.reset(token)
            if batch.has_pending_callbacks:
                transaction.on_commit(
                    lambda: cls._execute_batch(batch, chunk_size=chunk_size, refresh=refresh)
                )


class ElasticsearchIndexMixin:
    """Shared Elasticsearch serialization and indexing methods for indexed Django models."""

    es_autorefresh_setting: typing.ClassVar[str] = "ES_AUTOREFRESH"
    es_select_related: typing.ClassVar[tuple[str, ...]] = ()
    es_prefetch_related: typing.ClassVar[tuple[str, ...]] = ("tags",)

    def es_repr(self) -> DataDict:
        return ElasticsearchIndexService.serialize(typing.cast(models.Model, self))

    def field_es_repr(self, field_name: str) -> typing.Any:
        return ElasticsearchIndexService.serialize_field(typing.cast(models.Model, self), field_name)

    def update_index(self, *, refresh: bool | str | None = None) -> bool:
        return ElasticsearchIndexService.index_object(typing.cast(models.Model, self), refresh=refresh)

    def schedule_index_update(self) -> None:
        ElasticsearchIndexService.schedule_index(typing.cast(models.Model, self))

    def schedule_index_delete(self, pk: typing.Any | None = None) -> None:
        ElasticsearchIndexService.schedule_delete(typing.cast(models.Model, self), pk=pk)

    @staticmethod
    def _format_es_datetime(value: datetime | None) -> str | None:
        return ElasticsearchIndexService.format_datetime(value)

    def get_es_title_complete(self) -> DataDict:
        title_input = []
        title = getattr(self, "title", None)
        title_jpn = getattr(self, "title_jpn", None)
        if title:
            title_input.append(title)
        if title_jpn:
            title_input.append(title_jpn)
        return {
            "input": title_input,
        }

    def get_es_title_suggest(self) -> list[str]:
        title_input = []
        title = getattr(self, "title", None)
        title_jpn = getattr(self, "title_jpn", None)
        if title:
            title_input.append(title)
        if title_jpn:
            title_input.append(title_jpn)
        return title_input

    def get_es_size(self) -> int | None:
        return getattr(self, "filesize", None)

    def get_es_image_count(self) -> int | None:
        return getattr(self, "filecount", None)

    def get_es_create_date(self) -> str | None:
        return self._format_es_datetime(getattr(self, "create_date", None))

    def get_es_tags(self) -> list[DataDict]:
        tags_manager = getattr(self, "tags")
        return [{"scope": c.scope, "name": c.name, "full": str(c)} for c in tags_manager.all()]

    def get_es_tag_names(self) -> list[str]:
        tags_manager = getattr(self, "tags")
        return [c.name for c in tags_manager.all()]

    def get_es_tag_scopes(self) -> list[str]:
        tags_manager = getattr(self, "tags")
        return [c.scope for c in tags_manager.all()]

    def get_es_thumbnail(self) -> str | None:
        thumbnail = getattr(self, "thumbnail", None)
        if thumbnail:
            return thumbnail.url
        return None
