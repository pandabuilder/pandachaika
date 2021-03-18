from collections.abc import Iterable
from typing import Any

from django.core.mail import get_connection, EmailMultiAlternatives
from django.http import HttpRequest
from django.urls import reverse

from core.base.utilities import timestamp_or_zero

from viewer.models import Gallery


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
            } for archive in gallery.archive_set.filter_by_authenticated_status(authenticated=request.user.is_authenticated)
        ],
    } for gallery in galleries
    ]
