import logging
import typing

from django.conf import settings
from django.core.mail import BadHeaderError
from django.db.models import Q
from django.dispatch import receiver
from django.utils.html import urlize

from viewer.models import Gallery, users_with_perm, WantedGallery
from viewer.signals import wanted_gallery_found
from viewer.utils.functions import send_mass_html_mail

frontend_logger = logging.getLogger('viewer.frontend')
crawler_settings = settings.CRAWLER_SETTINGS


@receiver(wanted_gallery_found, sender=Gallery)
def wanted_gallery_found_handler(sender: typing.Any, **kwargs: typing.Any) -> None:
    gallery: Gallery = kwargs['gallery']
    wanted_gallery_list: typing.List[WantedGallery] = kwargs['wanted_gallery_list']

    notify_wanted_filters = [x.title or 'not set' for x in wanted_gallery_list if x.notify_when_found]

    if not notify_wanted_filters:
        return

    message = "Title: {}, source link: {}, link: {}, filters titles: {}".format(
        gallery.title,
        gallery.get_link(),
        gallery.get_absolute_url(),
        ', '.join(notify_wanted_filters)
    )

    # Mail users
    users_to_mail = users_with_perm(
        'viewer',
        'wanted_gallery_found',
        Q(email__isnull=False) | ~Q(email__exact=''),
        profile__notify_wanted_gallery_found=True
    )

    mails = users_to_mail.values_list('email', flat=True)

    try:
        frontend_logger.info('Wanted Gallery found: sending emails to enabled users.')
        # (subject, message, from_email, recipient_list)
        datatuples = tuple([(
            "Wanted Gallery match found",
            message,
            urlize(message),
            crawler_settings.mail_logging.from_,
            (mail,)
        ) for mail in mails])
        send_mass_html_mail(datatuples, fail_silently=True)
    except BadHeaderError:
        frontend_logger.error('Failed sending emails: Invalid header found.')
