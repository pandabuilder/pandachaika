from collections.abc import Iterable
from typing import Any, Optional

from django.core.mail import get_connection, EmailMultiAlternatives
from django.http import HttpRequest
from django.urls import reverse

from core.base.setup import Settings
from core.base.utilities import timestamp_or_zero, timestamp_or_null

from viewer.models import Gallery, Archive, Image, ArchiveGroupEntry
from viewer.utils.actions import event_log


def send_mass_html_mail(datatuple, fail_silently=False, user=None, password=None,
                        connection=None):
    """
    Given a datatuple of (subject, text_content, html_content, from_email,
    recipient_list), sends each message to each recipient list. Returns the
    number of emails sent.

    If from_email is None, the DEFAULT_FROM_EMAIL setting is used.
    If auth_user and auth_password are set, they're used to log in.
    If auth_user is None, the EMAIL_HOST_USER setting is used.
    If auth_password is None, the EMAIL_HOST_PASSWORD setting is used.

    """
    connection = connection or get_connection(
        username=user, password=password, fail_silently=fail_silently)
    messages = []
    for subject, text, html, from_email, recipient in datatuple:
        message = EmailMultiAlternatives(
            subject, text, from_email, recipient, headers={'Content-Transfer-Encoding': 'quoted-printable'}
        )
        message.attach_alternative(html, 'text/html')
        messages.append(message)
    return connection.send_messages(messages)


def archive_search_result_to_json(request: HttpRequest, archives: Iterable[Archive], user_is_authenticated: bool) -> list[dict[str, Any]]:
    response = [
        {
            'id': archive.pk,
            'title': archive.title,
            'title_jpn': archive.title_jpn,
            'filecount': archive.filecount,
            'filesize': archive.filesize,
            'posted': timestamp_or_null(archive.gallery.posted) if archive.gallery else None,
            'public_date': timestamp_or_null(archive.public_date),
            'create_date': timestamp_or_null(archive.create_date) if user_is_authenticated else None,
            'source': archive.source_type,
            'reason': archive.reason,
            'category': archive.gallery.category if archive.gallery else None,
            'uploader': archive.gallery.uploader if archive.gallery else None,
            'rating': archive.gallery.rating if archive.gallery else None,
            'link': archive.gallery.get_link() if archive.gallery else None,
            'download': request.build_absolute_uri(reverse('viewer:archive-download', args=(archive.pk,))),
            'url': request.build_absolute_uri(reverse('viewer:archive', args=(archive.pk,))),
            'thumbnail': request.build_absolute_uri(archive.thumbnail.url) if archive.thumbnail else None,
            'tags': archive.tag_list_sorted()
        } for archive in archives
    ]
    return response


def archive_manage_results_to_json(request: HttpRequest, archives: Iterable[Archive], user_is_authenticated: bool) -> list[dict[str, Any]]:
    response = [
        {
            'id': archive.pk,
            'title': archive.title,
            'title_jpn': archive.title_jpn,
            'filecount': archive.filecount,
            'filesize': archive.filesize,
            'public_date': timestamp_or_null(archive.public_date),
            'create_date': timestamp_or_null(archive.create_date) if user_is_authenticated else None,
            'last_modified': timestamp_or_null(archive.last_modified) if user_is_authenticated else None,
            'source_type': archive.source_type,
            'reason': archive.reason,
            'download': request.build_absolute_uri(reverse('viewer:archive-download', args=(archive.pk,))),
            'url': request.build_absolute_uri(reverse('viewer:archive', args=(archive.pk,))),
            'thumbnail': request.build_absolute_uri(archive.thumbnail.url) if archive.thumbnail else None,
            'tags': archive.tag_list_sorted(),
            'manage_entries': [x.mark_as_json_string() for x in archive.manage_entries.all()],
            'gallery': {
                'id': archive.gallery.pk,
                'posted': int(timestamp_or_zero(archive.gallery.posted)),
                'category': archive.gallery.category,
                'uploader': archive.gallery.uploader,
                'rating': archive.gallery.rating,
                'link': archive.gallery.get_link(),
                'hidden': archive.gallery.hidden,
                'url': request.build_absolute_uri(reverse('viewer:gallery', args=(archive.gallery.pk,))),
            } if archive.gallery else None,
        } for archive in archives
    ]
    return response


def gallery_search_results_to_json(request: HttpRequest, galleries: Iterable[Gallery]) -> list[dict[str, Any]]:
    return [{
        'id': gallery.pk,
        'gid': gallery.gid,
        'token': gallery.token,
        'title': gallery.title,
        'title_jpn': gallery.title_jpn,
        'category': gallery.category,
        'uploader': gallery.uploader,
        'comment': gallery.comment,
        'posted': int(timestamp_or_zero(gallery.posted)),
        'filecount': gallery.filecount,
        'filesize': gallery.filesize,
        'expunged': gallery.expunged,
        'disowned': gallery.disowned,
        'provider': gallery.provider,
        'rating': gallery.rating,
        'fjord': gallery.fjord,
        'tags': gallery.tag_list(),
        'link': gallery.get_link(),
        'thumbnail': request.build_absolute_uri(
            reverse('viewer:gallery-thumb', args=(gallery.pk,))) if gallery.thumbnail else '',
        'thumbnail_url': gallery.thumbnail_url,
        'archives': [
            {
                'link': request.build_absolute_uri(
                    reverse('viewer:archive-download', args=(archive.pk,))
                ),
                'source': archive.source_type,
                'reason': archive.reason
            } for archive in gallery.available_archives  # type: ignore
        ],
    } for gallery in galleries
    ]


def gallery_search_dict_to_json(request: HttpRequest, galleries: dict[Gallery, list[Archive]]) -> list[dict[str, Any]]:
    return [{
        'id': gallery.pk,
        'gid': gallery.gid,
        'token': gallery.token,
        'title': gallery.title,
        'title_jpn': gallery.title_jpn,
        'category': gallery.category,
        'uploader': gallery.uploader,
        'comment': gallery.comment,
        'posted': int(timestamp_or_zero(gallery.posted)),
        'filecount': gallery.filecount,
        'filesize': gallery.filesize,
        'expunged': gallery.expunged,
        'disowned': gallery.disowned,
        'provider': gallery.provider,
        'rating': gallery.rating,
        'fjord': gallery.fjord,
        'tags': gallery.tag_list(),
        'link': gallery.get_link(),
        'thumbnail': request.build_absolute_uri(
            reverse('viewer:gallery-thumb', args=(gallery.pk,))) if gallery.thumbnail else '',
        'thumbnail_url': gallery.thumbnail_url,
        'archives': [
            {
                'link': request.build_absolute_uri(
                    reverse('viewer:archive-download', args=(archive.pk,))
                ),
                'source': archive.source_type,
                'reason': archive.reason
            } for archive in archives
        ],
    } for gallery, archives in galleries.items()
    ]


def galleries_update_metadata(gallery_links, gallery_providers, user, reason, cs):
    current_settings = Settings(load_from_config=cs.config)
    if current_settings.workers.web_queue:
        current_settings.set_update_metadata_options(providers=gallery_providers)

        def gallery_callback(x: Optional['Gallery'], crawled_url: Optional[str], result: str) -> None:
            event_log(
                user,
                'UPDATE_METADATA',
                reason=reason,
                content_object=x,
                result=result,
                data=crawled_url
            )

        current_settings.workers.web_queue.enqueue_args_list(
            gallery_links,
            override_options=current_settings,
            gallery_callback=gallery_callback
        )


def images_data_to_json(images: Iterable[Image]) -> dict[int, dict[str, Any]]:
    return {
        image.archive_position: {
            'id': image.pk,
            'position': image.position,
            'archive_position': image.archive_position,
            'filename': image.image_name,
            'size': image.image_size,
            'sha1': image.sha1,
            'height': image.original_height,
            'width': image.original_width,
            'format': image.image_format,
            'mode': image.image_mode,
        } for image in images
    }


def archive_group_entry_to_json(archive_group_entry: ArchiveGroupEntry, user_is_authenticated: bool, request: HttpRequest) -> dict[str, Any]:
    archive = archive_group_entry.archive
    return {
        'id': archive_group_entry.id,
        'title': archive_group_entry.title,
        'position': archive_group_entry.position,
        'archive': {
            'id': archive.pk,
            'title': archive.title,
            'title_jpn': archive.title_jpn,
            'filecount': archive.filecount,
            'filesize': archive.filesize,
            'posted': timestamp_or_null(archive.gallery.posted) if archive.gallery else None,
            'public_date': timestamp_or_null(archive.public_date),
            'create_date': timestamp_or_null(archive.create_date) if user_is_authenticated else None,
            'source': archive.source_type,
            'reason': archive.reason,
            'category': archive.gallery.category if archive.gallery else None,
            'uploader': archive.gallery.uploader if archive.gallery else None,
            'rating': archive.gallery.rating if archive.gallery else None,
            'link': archive.gallery.get_link() if archive.gallery else None,
            'download': request.build_absolute_uri(reverse('viewer:archive-download', args=(archive.pk,))),
            'url': request.build_absolute_uri(reverse('viewer:archive', args=(archive.pk,))),
            'thumbnail': request.build_absolute_uri(archive.thumbnail.url) if archive.thumbnail else None,
            'tags': archive.tag_list_sorted()
        }
    }


def archive_entry_archive_to_json(archive: Archive, user_is_authenticated: bool, request: HttpRequest) -> dict[str, Any]:
    return {
        'id': archive.pk,
        'title': archive.title,
        'title_jpn': archive.title_jpn,
        'filecount': archive.filecount,
        'filesize': archive.filesize,
        'posted': timestamp_or_null(archive.gallery.posted) if archive.gallery else None,
        'public_date': timestamp_or_null(archive.public_date),
        'create_date': timestamp_or_null(archive.create_date) if user_is_authenticated else None,
        'source': archive.source_type,
        'reason': archive.reason,
        'category': archive.gallery.category if archive.gallery else None,
        'uploader': archive.gallery.uploader if archive.gallery else None,
        'rating': archive.gallery.rating if archive.gallery else None,
        'link': archive.gallery.get_link() if archive.gallery else None,
        'download': request.build_absolute_uri(reverse('viewer:archive-download', args=(archive.pk,))),
        'url': request.build_absolute_uri(reverse('viewer:archive', args=(archive.pk,))),
        'thumbnail': request.build_absolute_uri(archive.thumbnail.url) if archive.thumbnail else None,
        'tags': archive.tag_list_sorted()
    }
