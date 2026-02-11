import logging
import typing
from datetime import timezone

import elasticsearch
from django.conf import settings

from core.base.types import GalleryData
if typing.TYPE_CHECKING:
    from viewer.models import Gallery


es_client = settings.ES_CLIENT
es_match_index_name = settings.ES_MATCH_INDEX_NAME

logger = logging.getLogger(__name__)

if es_client:
    from elasticsearch.dsl import Search

ES_MATCH_MAPPING = {
    "properties": {
        "gid": {"type": "keyword"},
        "title": {"type": "text"},
        "title_jpn": {"type": "text"},
        "tags": {
            "type": "object",
            "properties": {
                "scope": {"type": "keyword", "store": True},
                "name": {"type": "keyword", "store": True},
                "full": {"type": "keyword"},
            },
        },
        "size": {"type": "long"},
        "image_count": {"type": "integer"},
        "posted_date": {"type": "date"},
        "provider": {"type": "keyword"},
        "reason": {"type": "keyword"},
        "category": {"type": "keyword"},
        "rating": {"type": "float"},
        "expunged": {"type": "boolean"},
        "disowned": {"type": "boolean"},
        "uploader": {"type": "keyword"},
        "source_url": {"type": "text", "index": False}
    }
}

def gallery_data_to_es_repr(gallery_data: 'GalleryData') -> dict:
    if gallery_data.tags:
        tags_converted = [(x.split(":", maxsplit=1), x) for x in gallery_data.tags]
    else:
        tags_converted = []
    data = {
        'gid': gallery_data.gid,
        'title': gallery_data.title,
        'title_jpn': gallery_data.title_jpn,
        'tags': [{"scope": c[0][0], "name": c[0][1], "full": c[1]} if len(c[0]) > 1 else {"scope": "", "name": c[0][0], "full": c[1]} for c in tags_converted],
        'size': gallery_data.filesize,
        'image_count': gallery_data.filecount,
        'posted_date': gallery_data.posted.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z") if gallery_data.posted else None,
        'provider': gallery_data.provider,
        'reason': gallery_data.reason,
        'category': gallery_data.category,
        'rating': gallery_data.rating,
        'expunged': gallery_data.expunged,
        'disowned': gallery_data.disowned,
        'uploader': gallery_data.uploader,
        'source_url': gallery_data.link
    }
    return data

def add_gallery_data_to_match_index(gallery: 'Gallery | GalleryData') -> bool:
    if not settings.ES_ENABLED or not es_client:
        return False
    if not es_client.indices.exists(index=es_match_index_name):
        return False

    if not isinstance(gallery, GalleryData):
        gallery_data = gallery.as_gallery_data()
    else:
        gallery_data = gallery

    es_data = gallery_data_to_es_repr(gallery_data)

    es_client.update(
        index=es_match_index_name,
        id=f"{gallery.provider}|{gallery.gid}",
        refresh=True,
        doc=es_data,
        doc_as_upsert=True
    )

    return True


def remove_gallery_from_match_index(gallery: 'Gallery | GalleryData') -> bool:
    if not settings.ES_ENABLED or not es_client:
        return False
    if not es_client.indices.exists(index=es_match_index_name):
        return False

    prev_id = f"{gallery.provider}|{gallery.gid}"

    try:
        es_client.delete(
            index=es_match_index_name, id=prev_id, refresh=True, request_timeout=30  # type: ignore
        )
        return True
    except elasticsearch.exceptions.NotFoundError:
        pass

    return False


def match_expression_to_wanted_index(q_string: str, gallery: 'Gallery | GalleryData') -> list[dict[str, typing.Any]] | None:
    if not settings.ES_ENABLED or not es_client:
        return None
    if not es_client.indices.exists(index=es_match_index_name):
        return None

    prev_id = f"{gallery.provider}|{gallery.gid}"

    s = (
        Search(using=es_client, index=es_match_index_name)
            .source(["gid", "provider"])
            .filter("term", _id=prev_id)
            .query("query_string", query=q_string, fields=["title", "title_jpn", "tags.full"])
     )

    response = s.execute()

    return [{"gid": i.gid, "provider": i.provider} for i in response]