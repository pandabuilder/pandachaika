from collections.abc import Iterable
from typing import Any, Optional

from django.core.mail import get_connection, EmailMultiAlternatives
from django.http import HttpRequest
from django.urls import reverse

from core.base.setup import Settings
from core.base.utilities import timestamp_or_zero, timestamp_or_null

from viewer.models import Gallery, Archive, Image, ArchiveGroupEntry, WantedGallery, FoundGallery
from viewer.utils.actions import event_log


def send_mass_html_mail(datatuple, fail_silently=False, user=None, password=None, connection=None):
    """
    Given a datatuple of (subject, text_content, html_content, from_email,
    recipient_list), sends each message to each recipient list. Returns the
    number of emails sent.

    If from_email is None, the DEFAULT_FROM_EMAIL setting is used.
    If auth_user and auth_password are set, they're used to log in.
    If auth_user is None, the EMAIL_HOST_USER setting is used.
    If auth_password is None, the EMAIL_HOST_PASSWORD setting is used.

    """
    connection = connection or get_connection(username=user, password=password, fail_silently=fail_silently)
    messages = []
    for subject, text, html, from_email, recipient in datatuple:
        message = EmailMultiAlternatives(
            subject, text, from_email, recipient, headers={"Content-Transfer-Encoding": "quoted-printable"}
        )
        message.attach_alternative(html, "text/html")
        messages.append(message)
    return connection.send_messages(messages)


def archive_search_result_to_json(
    request: HttpRequest, archives: Iterable[Archive], user_is_authenticated: bool
) -> list[dict[str, Any]]:
    response = [
        {
            "id": archive.pk,
            "title": archive.title,
            "title_jpn": archive.title_jpn,
            "filecount": archive.filecount,
            "filesize": archive.filesize,
            "posted": timestamp_or_null(archive.gallery.posted) if archive.gallery else None,
            "public_date": timestamp_or_null(archive.public_date),
            "create_date": timestamp_or_null(archive.create_date) if user_is_authenticated else None,
            "source": archive.source_type,
            "reason": archive.reason,
            "category": archive.gallery.category if archive.gallery else None,
            "uploader": archive.gallery.uploader if archive.gallery else None,
            "rating": archive.gallery.rating if archive.gallery else None,
            "link": archive.gallery.get_link() if archive.gallery else None,
            "download": request.build_absolute_uri(reverse("viewer:archive-download", args=(archive.pk,))),
            "url": request.build_absolute_uri(reverse("viewer:archive", args=(archive.pk,))),
            "thumbnail": request.build_absolute_uri(archive.thumbnail.url) if archive.thumbnail else None,
            "tags": archive.tag_list_sorted(),
        }
        for archive in archives
    ]
    return response


def archive_manage_results_to_json(
    request: HttpRequest, archives: Iterable[Archive], user_is_authenticated: bool
) -> list[dict[str, Any]]:
    response = [
        {
            "id": archive.pk,
            "title": archive.title,
            "title_jpn": archive.title_jpn,
            "filecount": archive.filecount,
            "filesize": archive.filesize,
            "public_date": timestamp_or_null(archive.public_date),
            "create_date": timestamp_or_null(archive.create_date) if user_is_authenticated else None,
            "last_modified": timestamp_or_null(archive.last_modified) if user_is_authenticated else None,
            "source_type": archive.source_type,
            "reason": archive.reason,
            "download": request.build_absolute_uri(reverse("viewer:archive-download", args=(archive.pk,))),
            "url": request.build_absolute_uri(reverse("viewer:archive", args=(archive.pk,))),
            "thumbnail": request.build_absolute_uri(archive.thumbnail.url) if archive.thumbnail else None,
            "tags": archive.tag_list_sorted(),
            "manage_entries": [
                {
                    "mark_user": x.mark_user.username if x.mark_user else None,
                    "mark_reason": x.mark_reason,
                    "mark_comment": x.mark_comment,
                    "mark_priority": x.mark_priority,
                    "mark_date": timestamp_or_null(x.mark_date),
                } for x in archive.manage_entries.all()
            ],
            "gallery": (
                {
                    "id": archive.gallery.pk,
                    "posted": int(timestamp_or_zero(archive.gallery.posted)),
                    "category": archive.gallery.category,
                    "uploader": archive.gallery.uploader,
                    "rating": archive.gallery.rating,
                    "link": archive.gallery.get_link(),
                    "hidden": archive.gallery.hidden,
                    "url": request.build_absolute_uri(reverse("viewer:gallery", args=(archive.gallery.pk,))),
                }
                if archive.gallery
                else None
            ),
        }
        for archive in archives
    ]
    return response


def gallery_search_results_to_json(request: HttpRequest, galleries: Iterable[Gallery]) -> list[dict[str, Any]]:
    return [
        {
            "id": gallery.pk,
            "gid": gallery.gid,
            "token": gallery.token,
            "title": gallery.title,
            "title_jpn": gallery.title_jpn,
            "category": gallery.category,
            "uploader": gallery.uploader,
            "comment": gallery.comment,
            "posted": int(timestamp_or_zero(gallery.posted)),
            "filecount": gallery.filecount,
            "filesize": gallery.filesize,
            "expunged": gallery.expunged,
            "disowned": gallery.disowned,
            "provider": gallery.provider,
            "rating": gallery.rating,
            "fjord": gallery.fjord,
            "tags": gallery.tag_list(),
            "link": gallery.get_link(),
            "url": request.build_absolute_uri(reverse("viewer:gallery", args=(gallery.pk,))),
            "thumbnail": (
                request.build_absolute_uri(reverse("viewer:gallery-thumb", args=(gallery.pk,)))
                if gallery.thumbnail
                else ""
            ),
            "thumbnail_url": gallery.thumbnail_url,
            "archives": [
                {
                    "link": request.build_absolute_uri(reverse("viewer:archive-download", args=(archive.pk,))),
                    "source": archive.source_type,
                    "reason": archive.reason,
                }
                for archive in gallery.available_archives  # type: ignore
            ],
        }
        for gallery in galleries
    ]


def gallery_search_dict_to_json(request: HttpRequest, galleries: dict[Gallery, list[Archive]]) -> list[dict[str, Any]]:
    return [
        {
            "id": gallery.pk,
            "gid": gallery.gid,
            "token": gallery.token,
            "title": gallery.title,
            "title_jpn": gallery.title_jpn,
            "category": gallery.category,
            "uploader": gallery.uploader,
            "comment": gallery.comment,
            "posted": int(timestamp_or_zero(gallery.posted)),
            "filecount": gallery.filecount,
            "filesize": gallery.filesize,
            "expunged": gallery.expunged,
            "disowned": gallery.disowned,
            "provider": gallery.provider,
            "rating": gallery.rating,
            "fjord": gallery.fjord,
            "tags": gallery.tag_list(),
            "link": gallery.get_link(),
            "thumbnail": (
                request.build_absolute_uri(reverse("viewer:gallery-thumb", args=(gallery.pk,)))
                if gallery.thumbnail
                else ""
            ),
            "thumbnail_url": gallery.thumbnail_url,
            "archives": [
                {
                    "link": request.build_absolute_uri(reverse("viewer:archive-download", args=(archive.pk,))),
                    "source": archive.source_type,
                    "reason": archive.reason,
                }
                for archive in archives
            ],
        }
        for gallery, archives in galleries.items()
    ]


def galleries_update_metadata(gallery_links, gallery_providers, user, reason, cs):
    current_settings = Settings(load_from_config=cs.config)
    if current_settings.workers.web_queue:
        current_settings.set_update_metadata_options(providers=gallery_providers)

        def gallery_callback(x: Optional["Gallery"], crawled_url: Optional[str], result: str) -> None:
            event_log(user, "UPDATE_METADATA", reason=reason, content_object=x, result=result, data=crawled_url)

        current_settings.workers.web_queue.enqueue_args_list(
            gallery_links, override_options=current_settings, gallery_callback=gallery_callback
        )


def images_data_to_json(images: Iterable[Image]) -> dict[int, dict[str, Any]]:
    return {
        image.archive_position: {
            "id": image.pk,
            "position": image.position,
            "archive_position": image.archive_position,
            "filename": image.image_name,
            "size": image.image_size,
            "sha1": image.sha1,
            "height": image.original_height,
            "width": image.original_width,
            "format": image.image_format,
            "mode": image.image_mode,
        }
        for image in images
    }


def archive_group_entry_to_json(
    archive_group_entry: ArchiveGroupEntry, user_is_authenticated: bool, request: HttpRequest
) -> dict[str, Any]:
    archive = archive_group_entry.archive
    return {
        "id": archive_group_entry.id,
        "title": archive_group_entry.title,
        "position": archive_group_entry.position,
        "archive": {
            "id": archive.pk,
            "title": archive.title,
            "title_jpn": archive.title_jpn,
            "filecount": archive.filecount,
            "filesize": archive.filesize,
            "posted": timestamp_or_null(archive.gallery.posted) if archive.gallery else None,
            "public_date": timestamp_or_null(archive.public_date),
            "create_date": timestamp_or_null(archive.create_date) if user_is_authenticated else None,
            "source": archive.source_type,
            "reason": archive.reason,
            "category": archive.gallery.category if archive.gallery else None,
            "uploader": archive.gallery.uploader if archive.gallery else None,
            "rating": archive.gallery.rating if archive.gallery else None,
            "link": archive.gallery.get_link() if archive.gallery else None,
            "download": request.build_absolute_uri(reverse("viewer:archive-download", args=(archive.pk,))),
            "url": request.build_absolute_uri(reverse("viewer:archive", args=(archive.pk,))),
            "thumbnail": request.build_absolute_uri(archive.thumbnail.url) if archive.thumbnail else None,
            "tags": archive.tag_list_sorted(),
        },
    }


def archive_entry_archive_to_json(
    archive: Archive, user_is_authenticated: bool, request: HttpRequest
) -> dict[str, Any]:
    return {
        "id": archive.pk,
        "title": archive.title,
        "title_jpn": archive.title_jpn,
        "filecount": archive.filecount,
        "filesize": archive.filesize,
        "posted": timestamp_or_null(archive.gallery.posted) if archive.gallery else None,
        "public_date": timestamp_or_null(archive.public_date),
        "create_date": timestamp_or_null(archive.create_date) if user_is_authenticated else None,
        "source": archive.source_type,
        "reason": archive.reason,
        "category": archive.gallery.category if archive.gallery else None,
        "uploader": archive.gallery.uploader if archive.gallery else None,
        "rating": archive.gallery.rating if archive.gallery else None,
        "link": archive.gallery.get_link() if archive.gallery else None,
        "download": request.build_absolute_uri(reverse("viewer:archive-download", args=(archive.pk,))),
        "url": request.build_absolute_uri(reverse("viewer:archive", args=(archive.pk,))),
        "thumbnail": request.build_absolute_uri(archive.thumbnail.url) if archive.thumbnail else None,
        "tags": archive.tag_list_sorted(),
    }


def found_gallery_to_json(found_gallery: FoundGallery, user_is_authenticated: bool) -> dict[str, Any]:
    gallery = found_gallery.gallery
    return {
        "id": gallery.pk,
        "gid": gallery.gid,
        "token": gallery.token,
        "title": gallery.title,
        "title_jpn": gallery.title_jpn,
        "category": gallery.category,
        "uploader": gallery.uploader,
        "posted": int(timestamp_or_zero(gallery.posted)),
        "filecount": gallery.filecount,
        "filesize": gallery.filesize,
        "expunged": gallery.expunged,
        "disowned": gallery.disowned,
        "provider": gallery.provider,
        "rating": gallery.rating,
        "fjord": gallery.fjord,
        "public": gallery.public,
        "tags": gallery.tag_list(),
        "link": gallery.get_link(),
        "match_accuracy": found_gallery.match_accuracy,
        "source": found_gallery.source,
        "found_create_date": timestamp_or_null(found_gallery.create_date),
    }


def wanted_gallery_to_json(
    wanted_gallery: WantedGallery,
    *,
    request: Optional[HttpRequest] = None,
    user_is_authenticated: bool = False,
    include_found_galleries: bool = False,
) -> dict[str, Any]:
    result = {
        "id": wanted_gallery.pk,
        "title": wanted_gallery.title,
        "title_jpn": wanted_gallery.title_jpn,
        "search_title": wanted_gallery.search_title,
        "regexp_search_title": wanted_gallery.regexp_search_title,
        "regexp_search_title_icase": wanted_gallery.regexp_search_title_icase,
        "unwanted_title": wanted_gallery.unwanted_title,
        "regexp_unwanted_title": wanted_gallery.regexp_unwanted_title,
        "regexp_unwanted_title_icase": wanted_gallery.regexp_unwanted_title_icase,
        "wanted_page_count_lower": wanted_gallery.wanted_page_count_lower,
        "wanted_page_count_upper": wanted_gallery.wanted_page_count_upper,
        "match_expression": wanted_gallery.match_expression,
        "wanted_tags_exclusive_scope": wanted_gallery.wanted_tags_exclusive_scope,
        "exclusive_scope_name": wanted_gallery.exclusive_scope_name,
        "wanted_tags_accept_if_none_scope": wanted_gallery.wanted_tags_accept_if_none_scope,
        "category": wanted_gallery.category,
        "wait_for_time": (
            wanted_gallery.wait_for_time.total_seconds() if wanted_gallery.wait_for_time is not None else None
        ),
        "should_search": wanted_gallery.should_search,
        "keep_searching": wanted_gallery.keep_searching,
        "reason": wanted_gallery.reason,
        "book_type": wanted_gallery.book_type,
        "publisher": wanted_gallery.publisher,
        "page_count": wanted_gallery.page_count,
        "restricted_to_links": wanted_gallery.restricted_to_links,
        "release_date": timestamp_or_null(wanted_gallery.release_date),
        "add_to_archive_group": (
            wanted_gallery.add_to_archive_group.pk if wanted_gallery.add_to_archive_group else None
        ),
        "wanted_tags": wanted_gallery.wanted_tags_list(),
        "unwanted_tags": wanted_gallery.unwanted_tags_list(),
        "wanted_providers": list(wanted_gallery.wanted_providers.values_list("slug", flat=True)),
        "unwanted_providers": list(wanted_gallery.unwanted_providers.values_list("slug", flat=True)),
        "categories": wanted_gallery.categories_list(),
        "public": wanted_gallery.public,
        "found": wanted_gallery.found,
        "date_found": timestamp_or_null(wanted_gallery.date_found),
        "create_date": timestamp_or_null(wanted_gallery.create_date),
        "last_modified": timestamp_or_null(wanted_gallery.last_modified),
    }
    if include_found_galleries and request is not None:
        found_gallery_entries = getattr(wanted_gallery, "foundgallery_set", None)
        if found_gallery_entries is None:
            found_gallery_entries = FoundGallery.objects.filter(wanted_gallery=wanted_gallery).select_related(
                "gallery"
            ).prefetch_related("gallery__tags")
        result["found_galleries"] = [
            found_gallery_to_json(found_entry, user_is_authenticated)
            for found_entry in found_gallery_entries.all()
            if found_entry.gallery.public or user_is_authenticated
        ]
    return result
