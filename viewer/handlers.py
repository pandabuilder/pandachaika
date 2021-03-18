import logging
import typing
from urllib.parse import urljoin

from django.conf import settings
from django.core.mail import BadHeaderError
from django.db.models import Q
from django.dispatch import receiver
from django.urls import reverse
from django.utils.html import urlize, linebreaks

from viewer.models import Gallery, users_with_perm, WantedGallery
from viewer.signals import wanted_gallery_found
from viewer.utils.functions import send_mass_html_mail

logger = logging.getLogger(__name__)
crawler_settings = settings.CRAWLER_SETTINGS


@receiver(wanted_gallery_found, sender=Gallery)
def wanted_gallery_found_handler(sender: typing.Any, **kwargs: typing.Any) -> None:
    gallery: Gallery = kwargs['gallery']
    wanted_gallery_list: list[WantedGallery] = kwargs['wanted_gallery_list']

    notify_wanted_filters = [
        "({}, {})".format((x.title or 'not set'), (x.reason or 'not set')) for x in
        wanted_gallery_list if x.notify_when_found
    ]

    if not notify_wanted_filters:
        return

    # Mail users
    users_to_mail = users_with_perm(
        'viewer',
        'wanted_gallery_found',
        Q(email__isnull=False) | ~Q(email__exact=''),
        profile__notify_wanted_gallery_found=True
    )

    if not users_to_mail.count():
        return

    main_url = crawler_settings.urls.main_webserver_url

    message = "Title: {}, source link: {}, link: {}\nFilters title, reason: {}\nGallery tags: {}".format(
        gallery.title,
        gallery.get_link(),
        urljoin(main_url, gallery.get_absolute_url()),
        ', '.join(notify_wanted_filters),
        '\n'.join(gallery.tag_list_sorted())
    )

    message += '\nYou can manage the archives in: {}'.format(
        urljoin(main_url, reverse('viewer:manage-archives'))
    )

    mails = users_to_mail.values_list('email', flat=True)

    try:
        logger.info('Wanted Gallery found: sending emails to enabled users.')
        # (subject, message, from_email, recipient_list)
        datatuples = tuple([(
            "Wanted Gallery match found",
            message,
            urlize(linebreaks(message)),
            crawler_settings.mail_logging.from_,
            (mail,)
        ) for mail in mails])
        send_mass_html_mail(datatuples, fail_silently=True)
    except BadHeaderError:
        logger.error('Failed sending emails: Invalid header found.')
